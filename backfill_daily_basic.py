#!/usr/bin/env python3
"""daily_basic 全量 backfill → PG 表 daily_basic

tushare daily_basic: 换手率/估值/股本/市值, 全市场按日查询。
- 范围: 2015-01-01 起至今（给因子回测留 warmup）
- PG-only, 不导 CSV（可重新拉取的原始原料, 避免仓库膨胀; PG 为权威库）
- 幂等: ON CONFLICT (ts_code, trade_date) DO NOTHING
- 限流: 0.3s/次, 异常退避重试

用法:
  python3 backfill_daily_basic.py              # 全量/续传（跳过已有日期）
  python3 backfill_daily_basic.py --days 5     # 只拉最近 N 天（pipeline 增量用）
"""
import os
import sys
import time
import argparse
from datetime import date

import pandas as pd
import psycopg2
import tushare as ts

TOKEN = os.environ.get("TUSHARE_TOKEN", "72826744b6a3733e61cd602f4fd42fe56a6de0d5781ba77e0bfb929b")
PRO = ts.pro_api(TOKEN)

START = "20150101"

FIELDS = ("ts_code,trade_date,close,turnover_rate,turnover_rate_f,volume_ratio,"
          "pe,pe_ttm,pb,ps,ps_ttm,dv_ratio,dv_ttm,"
          "total_share,float_share,free_share,total_mv,circ_mv")
COLS = ["ts_code", "trade_date", "close", "turnover_rate", "turnover_rate_f",
        "volume_ratio", "pe", "pe_ttm", "pb", "ps", "ps_ttm", "dv_ratio", "dv_ttm",
        "total_share", "float_share", "free_share", "total_mv", "circ_mv"]

DDL = """
CREATE TABLE IF NOT EXISTS daily_basic (
    ts_code         varchar(12)  NOT NULL,
    trade_date      date         NOT NULL,
    close           float8,
    turnover_rate   float8,
    turnover_rate_f float8,
    volume_ratio    float8,
    pe              float8,
    pe_ttm          float8,
    pb              float8,
    ps              float8,
    ps_ttm          float8,
    dv_ratio        float8,
    dv_ttm          float8,
    total_share     float8,
    float_share     float8,
    free_share      float8,
    total_mv        float8,
    circ_mv         float8,
    PRIMARY KEY (ts_code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_daily_basic_date ON daily_basic (trade_date);
"""


def get_conn():
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "/tmp"),
        port=os.environ.get("PGPORT", "5432"),
        dbname=os.environ.get("PGDATABASE", "investassist"),
        user=os.environ.get("PGUSER", "james"),
        password=os.environ.get("PGPASSWORD", ""),
    )


def trading_days(conn, start: str, end: str) -> list[str]:
    cur = conn.cursor()
    cur.execute(
        "SELECT cal_date FROM trade_cal WHERE exchange='SSE' AND is_open='1' "
        "AND cal_date >= %s AND cal_date <= %s ORDER BY cal_date",
        (start, end),
    )
    days = [r[0].strftime("%Y%m%d") for r in cur.fetchall()]
    cur.close()
    return days


def fetch_one_day(day: str, retries: int = 3) -> pd.DataFrame:
    for attempt in range(1, retries + 1):
        try:
            return PRO.daily_basic(trade_date=day, fields=FIELDS)
        except Exception as e:
            print(f"  [{day}] 第{attempt}次失败: {str(e)[:120]}", flush=True)
            if attempt == retries:
                print(f"  [{day}] 放弃", flush=True)
                return pd.DataFrame()
            time.sleep(60 * attempt)
    return pd.DataFrame()


def upsert(conn, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    df = df[COLS].copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    # 数值列清洗: 空串/None → NULL
    for c in COLS[2:]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    tuples = [tuple(x) for x in df.itertuples(index=False, name=None)]
    cur = conn.cursor()
    cur.executemany(
        """INSERT INTO daily_basic (ts_code, trade_date, close, turnover_rate, turnover_rate_f,
           volume_ratio, pe, pe_ttm, pb, ps, ps_ttm, dv_ratio, dv_ttm,
           total_share, float_share, free_share, total_mv, circ_mv)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (ts_code, trade_date) DO NOTHING""",
        tuples,
    )
    conn.commit()
    cur.close()
    return len(tuples)


def done_days(conn) -> set:
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT trade_date FROM daily_basic")
    d = {r[0].strftime("%Y%m%d") for r in cur.fetchall()}
    cur.close()
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0, help="只拉最近 N 个交易日")
    args = ap.parse_args()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(DDL)
    conn.commit()
    cur.close()

    end = date.today().strftime("%Y%m%d")
    start = START
    if args.days:
        all_days = trading_days(conn, "20150101", end)
        start = all_days[-args.days] if len(all_days) >= args.days else "20150101"

    days = trading_days(conn, start, end)
    have = done_days(conn) if not args.days else set()
    todo = [d for d in days if d not in have]
    print(f"daily_basic backfill: {len(days)} 交易日, 已有 {len(days)-len(todo)}, 待拉 {len(todo)}", flush=True)

    total_rows = 0
    t0 = time.time()
    for i, day in enumerate(todo, 1):
        df = fetch_one_day(day)
        n = upsert(conn, df)
        total_rows += n
        if i % 50 == 0 or i == len(todo):
            rate = i / (time.time() - t0 + 1e-9)
            eta = (len(todo) - i) / max(rate, 0.01) / 60
            print(f"  [{i}/{len(todo)}] {day} +{n} 行 | 累计 {total_rows:,} | {rate:.1f} 天/秒 | ETA {eta:.0f} 分钟", flush=True)
        time.sleep(0.3)

    print(f"完成: +{total_rows:,} 行, 耗时 {(time.time()-t0)/60:.1f} 分钟", flush=True)
    conn.close()


if __name__ == "__main__":
    main()

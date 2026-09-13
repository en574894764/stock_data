# -*- coding: utf-8 -*-
"""回补 daily_quote 2010-2015 A股日线 (按 trade_date 全市场拉取, 跳过已有 >=1900 行的日期)

用法: python scripts/backfill_daily_history.py [--start 20100101] [--end 20151231] [--dry-run]
"""
import argparse
import os
import sys
import time

import psycopg2
import psycopg2.extras
import tushare as ts

TOKEN = os.environ.get("TUSHARE_TOKEN", "72826744b6a3733e61cd602f4fd42fe56a6de0d5781ba77e0bfb929b")
DB = dict(host="/tmp", dbname="investassist", user="james", connect_timeout=5)
pro = ts.pro_api(TOKEN)

INS = """
INSERT INTO daily_quote (ts_code, trade_year, trade_date, pre_close, open, high, low,
                         close, change, pct_chg, vol, amount)
VALUES %s
ON CONFLICT (ts_code, trade_year, trade_date) DO NOTHING
"""


def trading_days(start, end):
    cal = pro.trade_cal(exchange="SSE", start_date=start, end_date=end)
    return sorted(cal[cal["is_open"].astype(str) == "1"]["cal_date"].tolist())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20100101")
    ap.add_argument("--end", default="20151231")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    days = trading_days(args.start, args.end)
    print(f"trading days: {len(days)} ({days[0]} -> {days[-1]})", flush=True)

    conn = psycopg2.connect(**DB)
    conn.autocommit = True
    cur = conn.cursor()

    # 应上市数参考: 当日 list_date<=d 且未退市
    cur.execute("""
        SELECT list_date, delist_date FROM stocks
        WHERE exchange IN ('SSE','SZSE')
    """)
    from datetime import date as _d
    ranges = cur.fetchall()

    def expected(dstr):
        dd = _d(int(dstr[:4]), int(dstr[4:6]), int(dstr[6:8]))
        return sum(1 for ld, dl in ranges
                   if ld and ld <= dd and (dl is None or dl > dd))

    total_new = 0
    t0 = time.time()
    for i, d in enumerate(days):
        # 已有 A 股行数 >= 当日应上市数*0.95 视为完整, 跳过
        cur.execute(
            "SELECT count(*) FROM daily_quote WHERE trade_date=%s AND ts_code ~ '\\.(SH|SZ)$'", (d,))
        have = cur.fetchone()[0]
        if have >= expected(d) * 0.95:
            continue
        for attempt in range(5):
            try:
                df = pro.daily(trade_date=d)
                break
            except Exception as e:
                if attempt == 4:
                    print(f"[FAIL] {d}: {e}", flush=True)
                    df = None
                else:
                    time.sleep(3 * (attempt + 1))
        if df is None or df.empty:
            continue
        df = df[df["ts_code"].str.contains(r"\.(SH|SZ)$", regex=True)]
        rows = []
        for _, r in df.iterrows():
            td = str(r["trade_date"])
            rows.append((r["ts_code"], int(td[:4]), td,
                         r.get("pre_close"), r.get("open"), r.get("high"), r.get("low"),
                         r.get("close"), r.get("change"), r.get("pct_chg"),
                         r.get("vol"), r.get("amount")))
        if args.dry_run:
            print(f"[DRY] {d}: would insert {len(rows)} (have {have})", flush=True)
            continue
        if rows:
            psycopg2.extras.execute_values(cur, INS, rows, page_size=1000)
            total_new += len(rows)
        if (i + 1) % 60 == 0:
            print(f"[{i+1}/{len(days)}] {d} +{len(rows)} (have {have}) total_new={total_new} "
                  f"elapsed={time.time()-t0:.0f}s", flush=True)

    print(f"DONE total_new={total_new} elapsed={time.time()-t0:.0f}s", flush=True)
    cur.close()
    conn.close()


if __name__ == "__main__":
    sys.exit(main())

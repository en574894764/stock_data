#!/usr/bin/env python3
"""L3 跨源交叉验证 — 腾讯行情快照 vs PG 最新收盘价（手动/月度）

质检方案 2026-08-30 F5。用法:
    python scripts/cross_validate.py            # 默认抽 20 只 A股 + 5 ETF
    python scripts/cross_validate.py --n 30

注意:
  - 须在交易日收盘后（15:00+）运行，否则腾讯返回的是盘中价，全部会"偏差"
  - 仅覆盖沪深标的（sh/sz 前缀）；港股/北交所腾讯接口格式不同，本脚本不覆盖
  - 偏差阈值 0.5%（不同源的撮合口径/尾数处理有微小差异）
"""
from __future__ import annotations

import argparse
import os
import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

OK, WARN, ERR = "✅", "⚠️", "❌"


def get_conn():
    import psycopg2
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "/tmp"),
        dbname=os.environ.get("PGDATABASE", "investassist"),
        user=os.environ.get("PGUSER", "james"),
        password=os.environ.get("PGPASSWORD", ""),
        connect_timeout=5,
    )


def tx_code_for(ts_code: str) -> str | None:
    """ts_code → 腾讯行情代码 (sh600000 / sz000001)。仅沪深。"""
    sym, _, mkt = ts_code.partition(".")
    if mkt == "SH":
        return f"sh{sym}"
    if mkt == "SZ":
        return f"sz{sym}"
    return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--n", type=int, default=25, help="A股抽样数 (默认25)")
    args = p.parse_args()

    import requests

    conn = get_conn()
    cur = conn.cursor()

    # 最近有数据的交易日
    cur.execute("SELECT MAX(trade_date) FROM daily_quote WHERE ts_code NOT LIKE '%.HK'")
    latest = cur.fetchone()[0]

    # 分层抽样: A股 15/25 只（主板/创业/科创/北交所）+ ETF 5 只
    # 注意: sql_suffix 内的 LIKE 模式 % 需写成 %%（psycopg2 参数化转义）
    def sample(sql_suffix: str, n: int) -> list[str]:
        cur.execute(f"""
            SELECT ts_code FROM daily_quote d
            WHERE d.trade_date = %s AND d.ts_code {sql_suffix}
            ORDER BY random() LIMIT %s""", (latest, n))
        return [r[0] for r in cur.fetchall()]

    a_codes = (sample("NOT LIKE '%%.HK' AND ts_code NOT LIKE '30%%' AND ts_code NOT LIKE '68%%' AND ts_code NOT LIKE '%%.BJ'", args.n * 2 // 5)
               + sample("LIKE '30%%'", args.n // 5)
               + sample("LIKE '68%%'", args.n // 5)
               + sample("LIKE '%%.BJ'", max(1, args.n // 5)))
    etf_codes = []
    cur.execute("""SELECT code FROM etf_quote WHERE trade_date = %s ORDER BY random() LIMIT 5""", (latest,))
    etf_codes = [r[0] for r in cur.fetchall()]

    # PG 最新收盘
    pg_close = {}
    for ts in a_codes:
        cur.execute("SELECT close FROM daily_quote WHERE ts_code=%s AND trade_date=%s", (ts, latest))
        r = cur.fetchone()
        if r:
            pg_close[ts] = float(r[0])
    for code in etf_codes:
        cur.execute("SELECT close FROM etf_quote WHERE code=%s AND trade_date=%s", (code, latest))
        r = cur.fetchone()
        if r:
            pg_close[code] = float(r[0])
    conn.close()

    # 腾讯快照（北交所代码 tx 不支持会直接不返回）
    code_map = {}
    for ts in list(pg_close):
        tc = tx_code_for(ts)
        if tc:
            code_map[tc] = ts
    url = "https://web.sqt.gtimg.cn/q=" + ",".join(code_map.keys())
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30,
                        proxies={"http": None, "https": None})
    resp.encoding = "gbk"

    bad, checked, skipped = [], 0, 0
    for line in resp.text.strip().split(";"):
        if '="' not in line:
            continue
        parts = line.split('="')
        tc = parts[0].replace("v_", "").strip()
        data = parts[1].rstrip('"').split("~")
        if len(data) < 8 or tc not in code_map:
            continue
        ts = code_map[tc]
        try:
            tx_price = float(data[3])
        except (ValueError, IndexError):
            continue
        if tx_price <= 0:
            skipped += 1
            continue
        pg = pg_close[ts]
        if pg <= 0:
            continue
        diff_pct = abs(tx_price - pg) / pg * 100
        checked += 1
        if diff_pct > 0.5:
            bad.append((ts, pg, tx_price, round(diff_pct, 2)))

    print(f"{'='*60}")
    print(f"L3 跨源交叉验证 | PG({latest} close) vs 腾讯快照")
    print(f"{'='*60}")
    print(f"  抽样: A股 {len(a_codes)} + ETF {len(etf_codes)} | 腾讯可比 {checked} | 跳过 {skipped}")
    if bad:
        print(f"  {ERR} 偏差>0.5%: {len(bad)} 只")
        for ts, pg, tx, d in bad[:10]:
            print(f"     {ts}: PG={pg} 腾讯={tx} 偏差={d}%")
    else:
        print(f"  {OK} 全部 {checked} 只收盘价偏差 ≤ 0.5%")
    print(f"  （注: 港股/无腾讯代码标的不在覆盖范围）")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

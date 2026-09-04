#!/usr/bin/env python3
"""一次性回补 etf_quote 2026-05-01 ~ 2026-06-04 缺口 (pipeline v2 重写空窗期)."""
import os, sys, time
import psycopg2
import pandas as pd
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))
import tushare as ts

pro = ts.pro_api(os.environ["TUSHARE_TOKEN"])
conn = psycopg2.connect(host="/tmp", dbname="investassist", user="james")
cur = conn.cursor()

# 从 index_daily 拿 A 股交易日历 (缺口区间)
cur.execute("SELECT DISTINCT trade_date FROM index_daily WHERE symbol='000001.SH' "
            "AND trade_date >= '2026-05-01' AND trade_date <= '2026-06-04' ORDER BY 1")
dates = [r[0].strftime("%Y%m%d") for r in cur.fetchall()]
print(f"待补交易日: {len(dates)} 天: {dates[0]} ~ {dates[-1]}")

insert_sql = """
    INSERT INTO etf_quote (code, trade_date, trade_year, pre_close, open, high, low, close, change, pct_chg, vol, amount)
    VALUES %s
    ON CONFLICT (code, trade_date) DO NOTHING
"""

def cf(v):
    try:
        f = float(v)
        return None if pd.isna(f) else f
    except (TypeError, ValueError):
        return None

total = 0
for i, d in enumerate(dates):
    df = pro.fund_daily(trade_date=d)
    if df is None or df.empty:
        print(f"  {d}: 无行情!")
        continue
    rows = []
    for r in df.to_dict("records"):
        tc = str(r["ts_code"])
        if not tc.endswith((".SH", ".SZ")):
            continue
        o, h, l, c = (cf(r.get(x)) for x in ("open", "high", "low", "close"))
        if not (o and o > 0 and h and h > 0 and l and l > 0 and c and c > 0):
            continue
        rows.append((tc.split(".")[0], f"{d[:4]}-{d[4:6]}-{d[6:8]}", int(d[:4]),
                     cf(r.get("pre_close")), o, h, l, c,
                     cf(r.get("change")), cf(r.get("pct_chg")),
                     cf(r.get("vol")), cf(r.get("amount"))))
    if rows:
        from psycopg2.extras import execute_values
        execute_values(cur, insert_sql, rows, page_size=1000)
        conn.commit()
        total += len(rows)
    print(f"  [{i+1}/{len(dates)}] {d}: +{len(rows)}")
    time.sleep(0.35)

print(f"\n完成, 共回补 {total} 行")
cur.execute("SELECT code, MIN(trade_date), MAX(trade_date), COUNT(*) FROM etf_quote WHERE code='510300' GROUP BY 1")
print("510300 (回补后):", cur.fetchone())
conn.close()

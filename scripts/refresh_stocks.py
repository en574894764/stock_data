#!/usr/bin/env python
"""刷新 stocks 股票列表表（tushare stock_basic 全量 upsert）。

背景：stocks 表停在 2026-04-17，4/17 后新上市/退市状态未更新。
validate.py check_daily_per_stock 依赖 stocks.list_status='L' 判定
「上市中但无日线」，列表过期会导致误报/漏报。

用法：python scripts/refresh_stocks.py [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

import psycopg2

UPSERT_SQL = """
    INSERT INTO stocks (ts_code, symbol, name, area, industry, market, exchange,
                        list_status, list_date, delist_date, is_hs, updated_at)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
    ON CONFLICT (ts_code) DO UPDATE SET
        symbol = EXCLUDED.symbol,
        name = EXCLUDED.name,
        area = EXCLUDED.area,
        industry = EXCLUDED.industry,
        market = EXCLUDED.market,
        exchange = EXCLUDED.exchange,
        list_status = EXCLUDED.list_status,
        list_date = EXCLUDED.list_date,
        delist_date = EXCLUDED.delist_date,
        is_hs = EXCLUDED.is_hs,
        updated_at = NOW()
"""


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    import tushare as ts

    token = os.environ["TUSHARE_TOKEN"]
    pro = ts.pro_api(token)

    conn = psycopg2.connect(
        host=os.environ.get("PGHOST", "/tmp"),
        dbname=os.environ.get("PGDATABASE", "investassist"),
        user=os.environ.get("PGUSER", "james"),
    )

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*), MAX(list_date) FROM stocks")
        before_cnt, before_max = cur.fetchone()
    print(f"刷新前: {before_cnt} 只, MAX(list_date)={before_max}")

    # 全量拉取（含退市 D/P，list_status 三态）
    df = pro.stock_basic(exchange="", list_status="L",
                         fields="ts_code,symbol,name,area,industry,market,exchange,list_status,list_date,delist_date,is_hs")
    print(f"tushare L 在市: {len(df)} 只")

    # 分页拉退市（tushare list_status=D 单独拉）
    delisted = pro.stock_basic(exchange="", list_status="D",
                               fields="ts_code,symbol,name,area,industry,market,exchange,list_status,list_date,delist_date,is_hs")
    print(f"tushare D 退市: {len(delisted)} 只")

    # 暂停上市 P（数量少）
    try:
        paused = pro.stock_basic(exchange="", list_status="P",
                                 fields="ts_code,symbol,name,area,industry,market,exchange,list_status,list_date,delist_date,is_hs")
    except Exception:
        paused = df.iloc[0:0]
    print(f"tushare P 暂停: {len(paused)} 只")

    full = __import__("pandas").concat([df, delisted, paused], ignore_index=True)
    full = full.drop_duplicates(subset="ts_code")
    # 过滤变体代码：T 前缀退市变体（如 T600018.SH，symbol 8 字符超 varchar(6)，主代码正常存续）
    full = full[~full["ts_code"].str.startswith("T")]

    if args.dry_run:
        print(f"[dry-run] 将 upsert {len(full)} 只（现库 A股+北交所 {before_cnt - 2728}）")
        return

    rows = []
    for r in full.to_dict("records"):
        rows.append((
            r["ts_code"], r.get("symbol"), r.get("name"), r.get("area"),
            r.get("industry"), r.get("market"), r.get("exchange"),
            r.get("list_status"), r.get("list_date") or None,
            r.get("delist_date") or None, r.get("is_hs"),
        ))

    with conn.cursor() as cur:
        cur.executemany(UPSERT_SQL, rows)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*), MAX(list_date), COUNT(*) FILTER (WHERE list_status='L') FROM stocks")
        after_cnt, after_max, listed = cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM stocks WHERE ts_code LIKE '%%.HK'")
        hk = cur.fetchone()[0]
    print(f"刷新后: {after_cnt} 只 (A股+北交所 {after_cnt - hk}), 在市 {listed}, MAX(list_date)={after_max}")

    # 新增标的统计
    new_codes = set(full["ts_code"]) - set()
    print("✅ 完成")


if __name__ == "__main__":
    main()

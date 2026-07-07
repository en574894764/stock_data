#!/usr/bin/env python3
"""用腾讯历史 K 线接口补全 InvestAssist PostgreSQL daily_quote 表。

用法：
  python backfill_investassist_tx.py                    # 补全最近 30 天
  python backfill_investassist_tx.py --dates 20260703,20260706  # 指定日期
  python backfill_investassist_tx.py --dates 20260703 --dry-run # 预览
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path

import requests
import yaml
from sqlalchemy import create_engine, text

PROJECT_ROOT = Path("/Users/james/WorkBuddy/InvestAssist")
sys.path.insert(0, str(PROJECT_ROOT))

HEADERS = {"User-Agent": "Mozilla/5.0"}
BATCH_SIZE = 100
REQUEST_DELAY = 0.15

KLINE_URL = "http://web.ifzq.gtimg.cn/appstock/app/fqkline/get"


def load_engine():
    config_path = PROJECT_ROOT / "config" / "config.yaml"
    with open(config_path) as f:
        cfg = yaml.safe_load(f)
    db = cfg.get("database", {})
    url = f"postgresql://{db.get('user','james')}:{db.get('password','')}@{db.get('host','localhost')}:{db.get('port',5432)}/{db.get('name','investassist')}"
    return create_engine(url)


def get_stock_list(engine) -> list[tuple]:
    with engine.connect() as conn:
        r = conn.execute(text("SELECT ts_code, symbol FROM stocks WHERE list_status='L' AND exchange IN ('SSE','SZSE') ORDER BY ts_code"))
        return [(row[0], row[1]) for row in r]


def get_existing_dates(engine, ts_codes: list[str], target_dates: list[str]) -> dict:
    """返回每只标的已存在的日期集合。"""
    result = {}
    if not target_dates:
        return result
    placeholders = ",".join([f"'{d}'" for d in target_dates])
    with engine.connect() as conn:
        for ts_code in ts_codes:
            r = conn.execute(
                text(f"SELECT trade_date FROM daily_quote WHERE ts_code = :code AND trade_date IN ({placeholders})"),
                {"code": ts_code}
            )
            result[ts_code] = {str(row[0]) for row in r}
    return result


def fetch_batch(tx_codes_map: dict, start: str, end: str) -> list[dict]:
    """批量拉取历史 K 线。tx_codes_map: {tx_code: ts_code, ...}"""
    results = []
    for tx_code, ts_code in tx_codes_map.items():
        param = f"{tx_code},day,{start},{end},30,qfq"
        try:
            resp = requests.get(KLINE_URL, params={"param": param}, headers=HEADERS, timeout=30,
                                proxies={"http": None, "https": None})
            resp.encoding = "gb2312"
            data = json.loads(resp.text)
        except Exception:
            continue

        if not isinstance(data, dict):
            continue

        stock_data = data.get("data", {})
        if isinstance(stock_data, list):
            continue
        stock_data = stock_data.get(tx_code, {})
        if not isinstance(stock_data, dict):
            continue

        days = stock_data.get("qfqday") or stock_data.get("day") or []

        for d in days:
            if len(d) < 6:
                continue
            if not (start <= d[0] <= end):
                continue
            try:
                results.append({
                    "ts_code": ts_code,
                    "trade_date": d[0],
                    "open": float(d[1]) if d[1] else None,
                    "close": float(d[2]) if d[2] else None,
                    "high": float(d[3]) if d[3] else None,
                    "low": float(d[4]) if d[4] else None,
                    "vol": float(d[5]) * 100 if d[5] else 0,
                })
            except (ValueError, IndexError):
                continue

    return results


def main():
    parser = argparse.ArgumentParser(description="InvestAssist PostgreSQL 日线数据补全")
    parser.add_argument("--dates", help="指定日期，逗号分隔 YYYYMMDD,YYYYMMDD")
    parser.add_argument("--start", help="起始日期 YYYYMMDD")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    engine = load_engine()
    stocks = get_stock_list(engine)
    print(f"待处理标的: {len(stocks)} 只")

    if args.dates:
        target_dates = sorted(set(args.dates.split(",")))
        start_date = target_dates[0]
        end_date = target_dates[-1]
    elif args.start:
        start_date = args.start
        end_date = date.today().strftime("%Y%m%d")
        target_dates = []  # 不进行去重检查
    else:
        # 默认最近30天
        start_date = (date.today().replace(day=1)).strftime("%Y%m%d")
        end_date = date.today().strftime("%Y%m%d")
        target_dates = []

    print(f"日期范围: {start_date} ~ {end_date}")
    if target_dates:
        print(f"指定日期: {', '.join(target_dates)}")
    if args.dry_run:
        print("[DRY RUN] 不实际写入")

    # 获取已有数据
    ts_codes = [s[0] for s in stocks]
    if target_dates:
        existing = get_existing_dates(engine, ts_codes, target_dates)
        # 统计
        missing_count = sum(1 for c in ts_codes if len(existing.get(c, set())) < len(target_dates))
        print(f"有缺失的标的: {missing_count} 只")

    total_inserts = 0
    total_batches = (len(stocks) + BATCH_SIZE - 1) // BATCH_SIZE
    start_time = time.time()

    for batch_idx in range(0, len(stocks), BATCH_SIZE):
        batch_stocks = stocks[batch_idx:batch_idx + BATCH_SIZE]

        # 构建 tx_code 映射
        tx_code_map = {}
        for ts_code, symbol in batch_stocks:
            if ts_code.endswith(".SH"):
                tx_code_map[f"sh{symbol}"] = ts_code
            else:
                tx_code_map[f"sz{symbol}"] = ts_code

        if not args.dry_run:
            rows = fetch_batch(tx_code_map, start_date, end_date)
        else:
            rows = fetch_batch(tx_code_map, start_date, end_date)

        # 过滤已存在日期
        if target_dates and not args.dry_run:
            rows = [r for r in rows
                    if r["trade_date"] in target_dates
                    and r["trade_date"] not in existing.get(r["ts_code"], set())]

        if rows and not args.dry_run:
            with engine.connect() as conn:
                for r in rows:
                    try:
                        conn.execute(text("""
                            INSERT INTO daily_quote (ts_code, trade_date, trade_year, open, high, low, close, pre_close, vol, amount, pct_chg, total_mv)
                            VALUES (:ts_code, :td, :ty, :open, :high, :low, :close, :pre_close, :vol, :amount, :pct_chg, :mv)
                            ON CONFLICT (ts_code, trade_date, trade_year) DO UPDATE SET
                                open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
                                close = EXCLUDED.close, vol = EXCLUDED.vol
                        """), {
                            "ts_code": r["ts_code"],
                            "td": r["trade_date"],
                            "ty": int(r["trade_date"][:4]),
                            "open": r["open"],
                            "high": r["high"],
                            "low": r["low"],
                            "close": r["close"],
                            "pre_close": r["open"],  # 腾讯历史接口无昨收
                            "vol": r["vol"],
                            "amount": 0,
                            "pct_chg": 0,
                            "mv": 0,
                        })
                    except Exception:
                        continue
                conn.commit()

        batch_inserts = len(rows) if rows else 0
        total_inserts += batch_inserts
        elapsed = time.time() - start_time
        eta = (elapsed / max(batch_idx // BATCH_SIZE + 1, 1)) * (total_batches - batch_idx // BATCH_SIZE - 1)
        print(f"[{batch_idx // BATCH_SIZE + 1}/{total_batches}] "
              f"本批 {batch_inserts} 行 | 累计 {total_inserts} 行 | ETA {eta:.0f}s")

        time.sleep(REQUEST_DELAY)

    print(f"\n{'[DRY RUN] ' if args.dry_run else ''}完成: {total_inserts} 行写入 PostgreSQL")


if __name__ == "__main__":
    main()

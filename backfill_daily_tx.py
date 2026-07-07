#!/usr/bin/env python3
"""用腾讯历史 K 线接口补全缺失的日线数据。

用法：
  python backfill_daily_tx.py                    # 补全最近 7 天
  python backfill_daily_tx.py --start 20260701   # 从指定日期开始
  python backfill_daily_tx.py --dates 20260703,20260706  # 指定日期列表
  python backfill_daily_tx.py --dry-run          # 预览不写入
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path

import requests

REPO = Path(__file__).parent
DAILY_DIR = REPO / "daily"

# 腾讯历史 K 线接口配置
KLINE_URL = "http://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
HEADERS = {"User-Agent": "Mozilla/5.0"}
BATCH_SIZE = 30  # 每批请求的标的数
REQUEST_DELAY = 0.1  # 批次间延迟


def get_ts_codes() -> list[str]:
    """从 daily/ 目录获取所有 A 股标的代码。"""
    codes = []
    for f in sorted(DAILY_DIR.glob("*.csv")):
        stem = f.stem
        if stem.endswith((".SH", ".SZ")) and not stem.endswith(".BJ"):
            codes.append(stem)
    return codes


def ts_to_tx(ts_code: str) -> str:
    """转换 ts_code 到腾讯格式，如 600519.SH → sh600519。"""
    symbol, exchange = ts_code.split(".")
    prefix = "sh" if exchange == "SH" else "sz"
    return f"{prefix}{symbol}"


def fetch_historical(tx_code: str, start: str, end: str) -> list[dict] | None:
    """拉取单个标的的历史 K 线数据。"""
    param = f"{tx_code},day,{start},{end},30,qfq"
    try:
        resp = requests.get(KLINE_URL, params={"param": param}, headers=HEADERS, timeout=30,
                            proxies={"http": None, "https": None})
        resp.encoding = "gb2312"
        data = json.loads(resp.text)
    except Exception as e:
        print(f"  {tx_code}: 请求失败 - {e}")
        return None

    if not isinstance(data, dict):
        return None

    stock_data = data.get("data", {})
    if isinstance(stock_data, list):
        return None
    stock_data = stock_data.get(tx_code, {})
    if not isinstance(stock_data, dict):
        return None

    days = stock_data.get("qfqday") or stock_data.get("day") or []

    if not days:
        return None

    results = []
    for d in days:
        if len(d) < 6:
            continue
        results.append({
            "date": d[0],
            "open": float(d[1]) if d[1] else None,
            "close": float(d[2]) if d[2] else None,
            "high": float(d[3]) if d[3] else None,
            "low": float(d[4]) if d[4] else None,
            "volume": float(d[5]) * 100 if d[5] else 0,  # 手 → 股
        })
    return results


def write_to_csv(ts_code: str, rows: list[dict]) -> int:
    """将数据写入 CSV，跳过已有日期。返回新增行数。"""
    csv_path = DAILY_DIR / f"{ts_code}.csv"

    # 读已有日期
    existing_dates = set()
    if csv_path.exists():
        try:
            with open(csv_path) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    existing_dates.add(row.get("datetime", ""))
        except Exception:
            pass

    new_rows = [r for r in rows if r["date"] not in existing_dates]
    if not new_rows:
        return 0

    existed = csv_path.exists()
    with open(csv_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"])
        if not existed:
            writer.writeheader()
        for r in new_rows:
            writer.writerow({
                "symbol": ts_code,
                "datetime": r["date"],
                "open": r["open"] if r["open"] is not None else "",
                "high": r["high"] if r["high"] is not None else "",
                "low": r["low"] if r["low"] is not None else "",
                "close": r["close"] if r["close"] is not None else "",
                "volume": r["volume"],
                "amount": "",  # 腾讯历史接口无成交额
            })

    return len(new_rows)


def main():
    parser = argparse.ArgumentParser(description="腾讯历史 K 线补全日线数据")
    parser.add_argument("--start", help="起始日期 YYYYMMDD")
    parser.add_argument("--dates", help="指定日期列表，逗号分隔 YYYYMMDD,YYYYMMDD")
    parser.add_argument("--end", help="结束日期 YYYYMMDD，默认今天")
    parser.add_argument("--dry-run", action="store_true", help="预览不写入")
    parser.add_argument("--limit", type=int, default=0, help="限制处理的标的数（调试用）")
    args = parser.parse_args()

    ts_codes = get_ts_codes()
    print(f"共 {len(ts_codes)} 只 A 股")

    if args.limit > 0:
        ts_codes = ts_codes[: args.limit]
        print(f"限制: 仅处理前 {args.limit} 只")

    end_date = (args.end or date.today().strftime("%Y%m%d")).replace("-", "")
    if args.dates:
        dates = args.dates.split(",")
        start_date = dates[0]
        end_date = dates[-1]
    else:
        start_date = (args.start or (date.today() - timedelta(days=7)).strftime("%Y%m%d")).replace("-", "")
    print(f"日期范围: {start_date} ~ {end_date}")
    if args.dry_run:
        print("[DRY RUN] 不实际写入")

    total_new = 0
    total_stocks = 0
    start_time = time.time()

    for i in range(0, len(ts_codes), BATCH_SIZE):
        batch = ts_codes[i : i + BATCH_SIZE]
        batch_new = 0

        for ts_code in batch:
            tx_code = ts_to_tx(ts_code)
            rows = fetch_historical(tx_code, start_date, end_date)

            if rows is None:
                continue

            if not args.dry_run:
                new = write_to_csv(ts_code, rows)
                batch_new += new
            else:
                batch_new += len(rows)

            total_stocks += 1

        total_new += batch_new
        elapsed = time.time() - start_time
        eta = (elapsed / max(total_stocks, 1)) * (len(ts_codes) - total_stocks) if total_stocks > 0 else 0
        print(f"[{min(i + BATCH_SIZE, len(ts_codes))}/{len(ts_codes)}] "
              f"本批 +{batch_new} 行 | 累计 {total_stocks} 只 {total_new} 行 | "
              f"ETA {eta:.0f}s" if not args.dry_run else
              f"[{min(i + BATCH_SIZE, len(ts_codes))}/{len(ts_codes)}] "
              f"预览 {total_stocks} 只 {total_new} 行 | ETA {eta:.0f}s")

        time.sleep(REQUEST_DELAY)

    print(f"\n{'[DRY RUN] ' if args.dry_run else ''}完成: {total_stocks} 只标的, 新增 {total_new} 行")


if __name__ == "__main__":
    main()

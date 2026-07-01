#!/usr/bin/env python3
"""填补 stock_data 数据缺口。

从 AKShare 拉取缺失的日线数据，写入 daily/*.csv 和 PostgreSQL daily_quote 表。
用法：
  python scripts/gap_fill.py           # 增量填补
  python scripts/gap_fill.py --dry-run  # 预览
  python scripts/gap_fill.py --max-stocks 100  # 限制数量（测试用）
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

import akshare as ak
import pandas as pd
import psycopg2

REPO = Path(__file__).parent.parent
DAILY_DIR = REPO / "daily"
DB_HOST = os.environ.get("STOCK_DB_HOST", "/tmp")
DB_PORT = os.environ.get("STOCK_DB_PORT", "5432")
DB_NAME = os.environ.get("STOCK_DB_NAME", "investassist")
DB_USER = os.environ.get("STOCK_DB_USER", "james")

# 并行度
MAX_WORKERS = 8
# 单 stock 最大重试次数
MAX_RETRIES = 2


def get_db_conn():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER,
        connect_timeout=5,
    )


def get_trade_dates(start: date, end: date) -> list[str]:
    """获取 start~end 之间的交易日（用 AKShare 交易日历）。"""
    try:
        df = ak.tool_trade_date_hist_sina()
        if df is None or df.empty:
            return []
        trade_dates = set(df["trade_date"].astype(str).tolist())
        result = []
        d = start
        while d <= end:
            ds = d.strftime("%Y-%m-%d")
            if ds in trade_dates:
                result.append(ds)
            d += timedelta(days=1)
        return result
    except Exception as e:
        print(f"交易日历获取失败: {e}, 回退到周一到周五")
        result = []
        d = start
        while d <= end:
            if d.weekday() < 5:
                result.append(d.strftime("%Y-%m-%d"))
            d += timedelta(days=1)
        return result


def get_last_date(csv_path: Path) -> str | None:
    """读取 CSV 文件最后一行的日期。"""
    if not csv_path.exists():
        return None
    try:
        with open(csv_path) as f:
            # 快速读取最后一行
            reader = csv.DictReader(f)
            last = None
            for row in reader:
                last = row
            if last:
                return last.get("datetime", last.get("trade_date", ""))[:10]
    except Exception:
        pass
    return None


def write_to_csv(csv_path: Path, records: list[dict]):
    """追加写入 CSV（去重）。"""
    # 读取已有日期集合
    existing_dates = set()
    if csv_path.exists():
        try:
            with open(csv_path) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    dt = row.get("datetime", row.get("trade_date", ""))[:10]
                    if dt:
                        existing_dates.add(dt)
        except Exception:
            pass

    new_records = [r for r in records if r["trade_date"][:10] not in existing_dates]
    if not new_records:
        return 0

    fields = ["ts_code", "trade_date", "open", "high", "low", "close", "pct_chg", "vol", "amount"]
    existed = csv_path.exists()
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if not existed:
            writer.writeheader()
        for r in new_records:
            writer.writerow({k: r.get(k, "") for k in fields})
    return len(new_records)


def write_to_db(conn, records: list[dict]):
    """批量写入 PostgreSQL daily_quote 表。"""
    if not records:
        return 0
    sql = """
        INSERT INTO daily_quote (ts_code, trade_year, trade_date, open, high, low, close, pct_chg, vol, amount)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (ts_code, trade_year, trade_date) DO UPDATE SET
            open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
            close = EXCLUDED.close, pct_chg = EXCLUDED.pct_chg,
            vol = EXCLUDED.vol, amount = EXCLUDED.amount
    """
    cur = conn.cursor()
    written = 0
    for r in records:
        try:
            td = r["trade_date"]
            year = int(td[:4])
            cur.execute(sql, (
                r["ts_code"], year, td,
                safe_float(r.get("open")), safe_float(r.get("high")),
                safe_float(r.get("low")), safe_float(r.get("close")),
                safe_float(r.get("pct_chg")), safe_float(r.get("vol")),
                safe_float(r.get("amount")),
            ))
            written += 1
        except Exception as e:
            # 可能是分区不存在，忽略
            pass
    conn.commit()
    cur.close()
    return written


def safe_float(v) -> float | None:
    if v is None or v == "" or v == "-":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def fetch_stock_history(stock_info: dict, end_date: str) -> list[dict]:
    """从 AKShare 拉取单个股票的日线历史。返回标准化记录列表。"""
    ts_code = stock_info["ts_code"]
    code = ts_code.split(".")[0]

    # 确定 exchange 后缀
    suffix = ts_code.split(".")[-1] if "." in ts_code else ""
    if suffix == "SH":
        symbol_ak = f"sh{code}"
    elif suffix == "SZ":
        symbol_ak = f"sz{code}"
    else:
        symbol_ak = f"sh{code}" if code.startswith("6") else f"sz{code}"

    for attempt in range(MAX_RETRIES):
        try:
            df = ak.stock_zh_a_hist(
                symbol=code, period="daily",
                start_date="20260601", end_date=end_date.replace("-", ""),
                adjust="qfq",
            )
            if df is None or df.empty:
                return []

            records = []
            for _, row in df.iterrows():
                td = str(row.get("日期", ""))
                if not td or td < "2026-06-19":
                    continue
                records.append({
                    "ts_code": ts_code,
                    "trade_date": td,
                    "open": safe_float(row.get("开盘")),
                    "high": safe_float(row.get("最高")),
                    "low": safe_float(row.get("最低")),
                    "close": safe_float(row.get("收盘")),
                    "pct_chg": safe_float(row.get("涨跌幅")),
                    "vol": safe_float(row.get("成交量")),
                    "amount": safe_float(row.get("成交额")),
                })
            return records
        except Exception as e:
            if attempt == MAX_RETRIES - 1:
                pass  # 最终失败，静默
            else:
                import time
                time.sleep(0.5)
    return []


def main():
    parser = argparse.ArgumentParser(description="填补 stock_data 数据缺口")
    parser.add_argument("--dry-run", action="store_true", help="仅预览")
    parser.add_argument("--max-stocks", type=int, default=0, help="限制拉取数量（0=全部）")
    parser.add_argument("--csv-only", action="store_true", help="仅写 CSV，不写 DB")
    parser.add_argument("--threads", type=int, default=MAX_WORKERS)
    args = parser.parse_args()

    today = date.today()
    print(f"📅 当前日期: {today}")

    # 1. 找出需要填补的交易日
    gap_start = date(2026, 6, 19)
    trade_dates = get_trade_dates(gap_start, today)
    if not trade_dates:
        print("无交易日需要填补")
        return
    print(f"📊 需要填补的交易日: {trade_dates}")
    end_trade_date = max(trade_dates)

    # 2. 获取股票列表
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("SELECT ts_code, name FROM stocks WHERE list_status = 'L' ORDER BY ts_code")
        stocks = [{"ts_code": r[0], "name": r[1]} for r in cur.fetchall()]
        cur.close()
        conn.close()
    except Exception as e:
        print(f"DB 连接失败: {e}, 从 CSV 文件列表获取")
        stocks = []
        for f in sorted(DAILY_DIR.glob("*.csv")):
            if f.stem.endswith(".SH") or f.stem.endswith(".SZ"):
                stocks.append({"ts_code": f.stem, "name": ""})

    print(f"📋 待拉取股票数: {len(stocks)}")
    if args.max_stocks > 0:
        stocks = stocks[:args.max_stocks]
        print(f"  (限制为 {args.max_stocks} 只)")

    if args.dry_run:
        print(f"[DRY RUN] 将拉取 {len(stocks)} 只股票的 {len(trade_dates)} 天数据")
        return

    # 3. 并行拉取
    print(f"\n🔄 开始拉取 (并行度: {args.threads})...")
    total_csv_writes = 0
    total_db_writes = 0
    total_stocks_done = 0
    lock = threading.Lock()

    def fetch_one(info):
        nonlocal total_csv_writes, total_db_writes, total_stocks_done
        ts_code = info["ts_code"]
        records = fetch_stock_history(info, end_trade_date)
        if not records:
            with lock:
                total_stocks_done += 1
            return 0

        # 写 CSV
        csv_path = DAILY_DIR / f"{ts_code}.csv"
        n = write_to_csv(csv_path, records)

        # 写 DB
        db_n = 0
        if not args.csv_only:
            try:
                conn = get_db_conn()
                db_n = write_to_db(conn, records)
                conn.close()
            except Exception:
                pass

        with lock:
            total_csv_writes += n
            total_db_writes += db_n
            total_stocks_done += 1
            if total_stocks_done % 200 == 0:
                print(f"  进度: {total_stocks_done}/{len(stocks)} (CSV +{total_csv_writes}, DB +{total_db_writes})")
        return n

    with ThreadPoolExecutor(max_workers=args.threads) as executor:
        futures = {executor.submit(fetch_one, s): s for s in stocks}
        for f in as_completed(futures):
            try:
                f.result()
            except Exception as e:
                pass

    print(f"\n✅ 完成!")
    print(f"  CSV 写入: {total_csv_writes} 行")
    print(f"  DB 写入:  {total_db_writes} 行")
    print(f"  处理股票: {total_stocks_done} 只")


if __name__ == "__main__":
    main()

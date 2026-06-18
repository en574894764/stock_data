#!/usr/bin/env python3
"""一次性迁移：旧格式 → 新格式（按标的 CSV）。

旧格式（来源）：
  data/daily/a_shares/YYYY-QN.csv  — 全市场按季度
  data/daily/hk/YYYY-QN.csv
  1d/SYMBOL.parquet                — Argus 增量 parquet

新格式（目标）：
  daily/SYMBOL.csv                 — 每标的一个 CSV，append-only，git 友好

列格式（新）：
  symbol, datetime, open, high, low, close, volume, amount

运行：
  python3 migrate.py               # 预览（dry-run）
  python3 migrate.py --execute     # 真实迁移
  python3 migrate.py --execute --clean  # 迁移后删除旧目录
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).parent
OLD_A   = REPO / "data" / "daily" / "a_shares"
OLD_HK  = REPO / "data" / "daily" / "hk"
OLD_ETF = REPO / "data" / "daily" / "etf"
OLD_1D  = REPO / "1d"     # Argus parquet 缓存
NEW_DIR = REPO / "daily"

# 旧季度 CSV 的列名 → 标准列名
_COL_MAP = {
    "ts_code": "symbol", "code": "symbol",
    "trade_date": "datetime", "date": "datetime",
    "open": "open", "high": "high", "low": "low", "close": "close",
    "vol": "volume", "volume": "volume",
    "amount": "amount",
}
_FINAL_COLS = ["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"]


def _read_quarterly_csvs(directory: Path) -> pd.DataFrame | None:
    """读一个市场目录下所有季度 CSV，合并为 DataFrame。"""
    files = sorted(directory.glob("*-Q*.csv"))
    if not files:
        return None
    frames = []
    for f in files:
        try:
            df = pd.read_csv(f, low_memory=False)
            frames.append(df)
        except Exception as e:
            print(f"  ⚠️  跳过 {f.name}: {e}", file=sys.stderr)
    if not frames:
        return None
    return pd.concat(frames, ignore_index=True)


def _standardize(df: pd.DataFrame) -> pd.DataFrame:
    df = df.rename(columns={c: _COL_MAP.get(str(c).lower(), c) for c in df.columns})
    df = df.rename(columns={c: _COL_MAP.get(c, c) for c in df.columns})
    if "datetime" not in df.columns:
        raise ValueError(f"找不到 datetime 列，实际列: {list(df.columns)}")
    df["datetime"] = pd.to_datetime(df["datetime"].astype(str), errors="coerce")
    df = df.dropna(subset=["datetime"])
    for col in _FINAL_COLS:
        if col not in df.columns:
            df[col] = float("nan")
    return df[_FINAL_COLS]


def _write_per_stock(df: pd.DataFrame, execute: bool) -> dict[str, int]:
    """按 symbol 分组写入 daily/SYMBOL.csv（追加去重）。返回 {symbol: rows_written}。"""
    result: dict[str, int] = {}
    for symbol, group in df.groupby("symbol"):
        symbol = str(symbol)
        out_path = NEW_DIR / f"{symbol}.csv"
        group = group.sort_values("datetime").drop_duplicates(subset=["datetime"], keep="last")

        if out_path.exists():
            existing = pd.read_csv(out_path, parse_dates=["datetime"])
            last_dt = existing["datetime"].max()
            new_rows = group[group["datetime"] > last_dt]
        else:
            existing = None
            new_rows = group

        if new_rows.empty:
            continue

        if execute:
            if existing is not None:
                new_rows.to_csv(out_path, mode="a", header=False, index=False,
                                date_format="%Y-%m-%d")
            else:
                new_rows.to_csv(out_path, index=False, date_format="%Y-%m-%d")

        result[symbol] = len(new_rows)
    return result


def migrate_market(label: str, src_dir: Path, execute: bool) -> int:
    print(f"\n[{label}] 读取 {src_dir} ...", flush=True)
    raw = _read_quarterly_csvs(src_dir)
    if raw is None:
        print(f"  无数据，跳过")
        return 0
    print(f"  原始行数: {len(raw):,}，标的数: {raw.iloc[:, 0].nunique()}")
    df = _standardize(raw)
    written = _write_per_stock(df, execute)
    total = sum(written.values())
    print(f"  → {'写入' if execute else '预计写入'} {len(written)} 个标的，{total:,} 行")
    return total


def migrate_argus_parquet(execute: bool) -> int:
    """把 Argus 的 1d/*.parquet 合并进 daily/*.csv。"""
    files = list(OLD_1D.glob("*.parquet"))
    if not files:
        print("\n[Argus 1d parquet] 无文件，跳过")
        return 0
    print(f"\n[Argus 1d parquet] {len(files)} 个文件", flush=True)
    frames = []
    for f in files:
        df = pd.read_parquet(f)
        frames.append(df)
    raw = pd.concat(frames, ignore_index=True)
    # parquet 列已是标准格式
    if "datetime" in raw.columns:
        raw["datetime"] = pd.to_datetime(raw["datetime"])
    for col in _FINAL_COLS:
        if col not in raw.columns:
            raw[col] = float("nan")
    df = raw[_FINAL_COLS]
    written = _write_per_stock(df, execute)
    total = sum(written.values())
    print(f"  → {'写入' if execute else '预计写入'} {len(written)} 个标的，{total:,} 行")
    return total


def verify():
    """验证 daily/ 目录内容。"""
    files = sorted(NEW_DIR.glob("*.csv"))
    print(f"\n=== 验证 ===")
    print(f"daily/ 标的数: {len(files)}")
    total_rows = 0
    date_min, date_max = "9999", "0000"
    for f in files[:5]:
        df = pd.read_csv(f, parse_dates=["datetime"])
        rows = len(df)
        total_rows += rows
        mn = str(df["datetime"].min())[:10]
        mx = str(df["datetime"].max())[:10]
        if mn < date_min: date_min = mn
        if mx > date_max: date_max = mx
        print(f"  {f.name}: {rows} 行  {mn} ~ {mx}")
    if len(files) > 5:
        for f in files[5:]:
            df = pd.read_csv(f, parse_dates=["datetime"])
            total_rows += len(df)
    print(f"合计约 {total_rows:,} 行（仅统计部分），日期跨度 {date_min} ~ {date_max}")


def clean_old(dry_run: bool):
    """删除旧目录（迁移完成后）。"""
    import shutil
    old_dirs = [
        REPO / "data" / "daily",
        REPO / "data" / "fundamental",
        REPO / "data" / "meta",
        REPO / "data",
    ]
    for d in old_dirs:
        if d.exists():
            print(f"  {'[dry-run] 将删除' if dry_run else '删除'}: {d.relative_to(REPO)}")
            if not dry_run:
                shutil.rmtree(d)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="真实写入（否则只预览）")
    parser.add_argument("--clean", action="store_true", help="迁移后清理旧目录")
    args = parser.parse_args()

    if args.execute:
        NEW_DIR.mkdir(parents=True, exist_ok=True)
        print("=== 开始迁移（--execute 模式）===")
    else:
        print("=== 预览模式（加 --execute 才真实写入）===")

    total = 0
    for label, src in [("A股", OLD_A), ("港股", OLD_HK), ("ETF", OLD_ETF)]:
        if src.exists():
            total += migrate_market(label, src, args.execute)

    total += migrate_argus_parquet(args.execute)

    print(f"\n合计: {'写入' if args.execute else '预计写入'} {total:,} 行")

    if args.execute:
        verify()
        if args.clean:
            print("\n=== 清理旧目录 ===")
            clean_old(dry_run=False)
        else:
            print("\n提示: 加 --clean 可删除旧目录（迁移验证无误后再清理）")
    else:
        print("\n运行 python3 migrate.py --execute 开始迁移")

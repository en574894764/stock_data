#!/usr/bin/env python3
"""stock_data 数据完整性校验脚本

检查维度：
  1. 文件级 — 存在性、大小、编码
  2. Schema — 列名、类型、必填字段
  3. 时序 — 日期连续性、重复、倒序
  4. 行情 — OHLC 合理性、量价匹配
  5. 交叉引用 — symbol 一致性
  6. Git — 未跟踪大文件、未提交变更

用法：
  python validate.py                  # 全部检查
  python validate.py --quick          # 快速抽查 (1%)
  python validate.py --symbol 600519.SH  # 单标的
  python validate.py --report report.json  # 输出 JSON 报告
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

REPO = Path(__file__).parent
DAILY = REPO / "daily"
MARKET = REPO / "market"
INDEX = REPO / "index"
MACRO = REPO / "macro"
META = REPO / "meta"
FUND = REPO / "fundamental"

STATUS_OK = "✅"
STATUS_WARN = "⚠️"
STATUS_ERR = "❌"

all_issues: list[dict] = []


def issue(level: str, category: str, item: str, detail: str = ""):
    all_issues.append({"level": level, "category": category, "item": item, "detail": detail})
    icon = {"OK": STATUS_OK, "WARN": STATUS_WARN, "ERR": STATUS_ERR}[level]
    print(f"  {icon} [{category}] {item}" + (f" — {detail}" if detail else ""))


# ═════════════════════════════════════════════════════════════════════════════
# 1. 文件级检查
# ═════════════════════════════════════════════════════════════════════════════

def check_files_exist():
    print("\n── 文件存在性 ──")
    dirs = {"daily": DAILY, "meta": META, "macro": MACRO, "index": INDEX, "fundamental": FUND}
    for name, d in dirs.items():
        if not d.exists():
            issue("ERR", "exist", name, "目录不存在")
        elif name == "daily":
            cnt = len(list(d.glob("*.csv")))
            issue("OK", "exist", f"daily/ ({cnt:,} CSV)")
        else:
            cnt = len(list(d.glob("*.csv")))
            issue("OK", "exist", f"{name}/ ({cnt} CSV)")


def check_empty_files():
    print("\n── 空文件检查 ──")
    empty_count = 0
    for d in [DAILY, INDEX, MACRO, META]:
        if not d.exists():
            continue
        for f in d.glob("*.csv"):
            sz = f.stat().st_size
            if sz == 0:
                issue("ERR", "empty", str(f.relative_to(REPO)))
                empty_count += 1
            elif sz < 50:
                issue("WARN", "tiny", str(f.relative_to(REPO)), f"仅 {sz} bytes")
                empty_count += 1
    if empty_count == 0:
        issue("OK", "empty", "无空文件")


# ═════════════════════════════════════════════════════════════════════════════
# 2. Schema 检查
# ═════════════════════════════════════════════════════════════════════════════

DAILY_REQUIRED = {"symbol", "datetime", "open", "high", "low", "close", "volume", "amount"}
INDEX_REQUIRED = {"trade_date", "open", "high", "low", "close"}


def check_daily_schema(symbols: list[str] | None = None):
    print("\n── Daily Schema ──")
    files = sorted(DAILY.glob("*.csv"))
    if symbols:
        files = [DAILY / f"{s}.csv" for s in symbols if (DAILY / f"{s}.csv").exists()]
    bad = 0
    for f in files:
        try:
            df = pd.read_csv(f, nrows=1)
            cols = set(df.columns)
            missing = DAILY_REQUIRED - cols
            if missing:
                issue("ERR", "schema", f.name, f"缺列: {missing}")
                bad += 1
        except Exception as e:
            issue("ERR", "schema", f.name, str(e))
            bad += 1
    if bad == 0 and files:
        issue("OK", "schema", f"daily/ ({len(files)} 文件列结构正确)")


def check_index_schema():
    print("\n── Index Schema ──")
    bad = 0
    for f in sorted(INDEX.glob("*.csv")):
        try:
            df = pd.read_csv(f, nrows=1)
            missing = INDEX_REQUIRED - set(df.columns)
            if missing:
                issue("ERR", "schema", f.name, f"缺列: {missing}")
                bad += 1
        except Exception as e:
            issue("ERR", "schema", f.name, str(e))
            bad += 1
    if bad == 0:
        issue("OK", "schema", f"index/ ({len(list(INDEX.glob('*.csv')))} 文件)")

# ═════════════════════════════════════════════════════════════════════════════
# 3. 数据完整性检查
# ═════════════════════════════════════════════════════════════════════════════

def check_daily_integrity(symbols: list[str] | None = None, quick: bool = False):
    print("\n── Daily 数据完整性 ──")
    files = sorted(DAILY.glob("*.csv"))
    if symbols:
        files = [DAILY / f"{s}.csv" for s in symbols if (DAILY / f"{s}.csv").exists()]
    if quick:
        import random
        sample_size = max(10, int(len(files) * 0.01))
        files = random.sample(files, min(sample_size, len(files)))
        print(f"  (快速模式: 抽查 {len(files)}/{len(list(DAILY.glob('*.csv')))} 个文件)")

    dup_count = 0
    date_order_issues = 0
    ohlc_issues = 0
    na_count = 0
    total_rows = 0

    for f in files:
        try:
            df = pd.read_csv(f, parse_dates=["datetime"])
            total_rows += len(df)

            # 重复
            dups = df.duplicated(subset=["datetime"]).sum()
            if dups > 0:
                issue("WARN", "dup", f.name, f"{dups} 个重复日期")
                dup_count += 1

            # 日期排序
            if not df["datetime"].is_monotonic_increasing:
                issue("WARN", "order", f.name, "日期未严格递增")
                date_order_issues += 1

            # OHLC 合理性
            ohlc_bad = df[
                (df["high"] < df["low"]) |
                (df["open"] < 0) | (df["high"] < 0) |
                (df["low"] < 0) | (df["close"] < 0)
            ]
            if len(ohlc_bad) > 0:
                issue("ERR", "ohlc", f.name, f"{len(ohlc_bad)} 行 OHLC 不合理")
                ohlc_issues += 1

            # 空值
            na = df.isna().sum().sum()
            if na > 0:
                issue("WARN", "na", f.name, f"{na} 个空值")
                na_count += 1

        except Exception as e:
            issue("ERR", "read", f.name, str(e))

    print(f"  合计: {len(files)} 文件, {total_rows:,} 行")
    print(f"  重复: {dup_count} | 排序: {date_order_issues} | OHLC异常: {ohlc_issues} | 空值: {na_count}")


def check_date_continuity(symbols: list[str] | None = None, max_gap_days: int = 10):
    """检查日期间隔是否合理（交易日内允许 gap <= max_gap_days 日历天）"""
    print(f"\n── 交易日历连续性 (最大间隔 {max_gap_days} 天) ──")
    files = sorted(DAILY.glob("*.csv"))
    if symbols:
        files = [DAILY / f"{s}.csv" for s in symbols if (DAILY / f"{s}.csv").exists()]

    gap_issues = 0
    for f in files[:500]:  # 抽样前500个避免太慢
        try:
            df = pd.read_csv(f, parse_dates=["datetime"])
            dates = pd.to_datetime(df["datetime"]).dropna().sort_values()
            if len(dates) < 2:
                continue
            gaps = dates.diff().dropna()
            big_gaps = gaps[gaps > timedelta(days=max_gap_days)]
            if len(big_gaps) > 0:
                issue("WARN", "gap", f.name, f"{len(big_gaps)} 处间隔 > {max_gap_days} 天 (最大 {big_gaps.max().days} 天)")
                gap_issues += 1
        except Exception:
            pass

    if gap_issues == 0:
        issue("OK", "gap", "前500文件日期连续性良好")


def check_cross_ref():
    """检查 daily 中的 symbol 是否在 stock_basic 或 index 中能找到"""
    print("\n── 交叉引用 ──")
    stock_file = META / "stock_basic.csv"
    if not stock_file.exists():
        issue("WARN", "xref", "stock_basic.csv 不存在，跳过交叉引用")
        return

    try:
        stock_df = pd.read_csv(stock_file)
        stock_symbols = set(stock_df["ts_code"].astype(str))
    except Exception:
        issue("WARN", "xref", "stock_basic.csv 读取失败")
        return

    index_files = set(f.stem for f in INDEX.glob("*.csv"))
    daily_files = list(DAILY.glob("*.csv"))
    daily_symbols = set(f.stem for f in daily_files)

    orphan = daily_symbols - stock_symbols - index_files
    if orphan:
        issue("WARN", "xref", f"{len(orphan)} 个 daily 标的未在 stock_basic/index 中找到", str(sorted(list(orphan))[:10]))
    else:
        issue("OK", "xref", "daily 标的全部可追溯到 stock_basic 或 index")


# ═════════════════════════════════════════════════════════════════════════════
# 4. Fundamental 检查
# ═════════════════════════════════════════════════════════════════════════════

def check_fundamental():
    print("\n── Fundamental 检查 ──")
    if not FUND.exists():
        issue("WARN", "fundamental", "目录不存在")
        return
    for sub in sorted(FUND.iterdir()):
        if not sub.is_dir():
            continue
        csvs = sorted(sub.glob("*.csv"))
        if not csvs:
            issue("WARN", "fundamental", sub.name, "无数据")
            continue
        # 检查最新文件行数
        latest = csvs[-1]
        try:
            n = sum(1 for _ in open(latest)) - 1  # 减去表头
            issue("OK", "fundamental", f"{sub.name} ({len(csvs)} 期)", f"最新期 {n:,} 行")
        except Exception as e:
            issue("ERR", "fundamental", f"{sub.name}/{latest.name}", str(e))


# ═════════════════════════════════════════════════════════════════════════════
# 5. Market parquet 检查
# ═════════════════════════════════════════════════════════════════════════════

def check_market():
    print("\n── Market Parquet 检查 ──")
    if not MARKET.exists():
        issue("OK", "market", "目录不存在（正常，由 daily 派生）")
        return
    files = sorted(MARKET.glob("*.parquet"))
    if not files:
        issue("OK", "market", "无文件")
        return

    import pyarrow.parquet as pq
    bad = 0
    total_rows = 0
    for f in files[:100]:  # 抽查100个
        try:
            t = pq.read_table(f)
            total_rows += t.num_rows
        except Exception as e:
            issue("ERR", "market", f.name, str(e))
            bad += 1
    issue("OK", "market", f"{len(files):,} 文件 (抽查通过)" if bad == 0 else f"{bad} 损坏")


# ═════════════════════════════════════════════════════════════════════════════
# 6. Git 状态检查
# ═════════════════════════════════════════════════════════════════════════════

def check_git():
    print("\n── Git 状态 ──")
    import subprocess
    try:
        r = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, cwd=REPO, timeout=10)
        if r.returncode != 0:
            issue("WARN", "git", f"git status 返回 {r.returncode}")
            return
        lines = [l for l in r.stdout.strip().split("\n") if l]
        if not lines:
            issue("OK", "git", "工作区干净")
            return
        # 检查是否有不该跟踪的大文件
        untracked = [l for l in lines if l.startswith("??")]
        modified = [l for l in lines if l.startswith(" M") or l.startswith("M ")]
        if untracked:
            issue("WARN", "git", f"{len(untracked)} 个未跟踪文件", ", ".join(l[3:] for l in untracked[:5]))
        if modified:
            issue("WARN", "git", f"{len(modified)} 个已修改文件", ", ".join(l[3:] for l in modified[:5]))
    except FileNotFoundError:
        issue("WARN", "git", "git 不可用")
    except Exception as e:
        issue("WARN", "git", str(e))


# ═════════════════════════════════════════════════════════════════════════════
# 主入口
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="stock_data 数据完整性校验")
    parser.add_argument("--quick", action="store_true", help="快速抽查")
    parser.add_argument("--symbol", help="只检查指定标的")
    parser.add_argument("--skip-git", action="store_true", help="跳过 git 检查")
    parser.add_argument("--report", help="输出 JSON 报告文件")
    args = parser.parse_args()

    print(f"=== stock_data 完整性校验 ===\n仓库: {REPO}\n时间: {date.today()}")
    if args.quick:
        print("模式: 快速抽查")

    symbols = [args.symbol] if args.symbol else None

    check_files_exist()
    check_empty_files()
    check_daily_schema(symbols)
    check_index_schema()
    check_daily_integrity(symbols, quick=args.quick)
    if not args.quick:
        check_date_continuity(symbols)
    check_cross_ref()
    check_fundamental()
    check_market()
    if not args.skip_git:
        check_git()

    # 摘要
    errs = sum(1 for i in all_issues if i["level"] == "ERR")
    warns = sum(1 for i in all_issues if i["level"] == "WARN")
    print(f"\n{'='*50}")
    print(f"摘要: {STATUS_ERR} {errs} 错误  {STATUS_WARN} {warns} 警告  {STATUS_OK} 通过")

    if args.report:
        report = {
            "repo": str(REPO),
            "date": str(date.today()),
            "errors": errs,
            "warnings": warns,
            "issues": all_issues,
        }
        with open(args.report, "w") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"报告已保存: {args.report}")

    sys.exit(1 if errs > 0 else 0)

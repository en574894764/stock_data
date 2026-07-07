#!/usr/bin/env python3
"""stock_data 数据自动拉取 & GitHub 备份脚本

功能：
  1. 从数据源拉取增量数据 → 写入 stock_data CSV
  2. 校验新数据完整性
  3. Git commit + push 到 GitHub

数据源优先级：
  A. PostgreSQL (quant_sys investassist 数据库) — 通过 scripts/export.py
  B. Tushare Pro API — 直接拉取
  C. AKShare — 开源免费数据

用法：
  python fetch_and_backup.py                 # 增量拉取 + 备份
  python fetch_and_backup.py --full          # 全量重建
  python fetch_and_backup.py --dry-run       # 预览，不实际写入
  python fetch_and_backup.py --source pg     # 指定数据源 (pg|tushare|akshare)
  python fetch_and_backup.py --no-push       # 拉取但不推送
  python fetch_and_backup.py --cron          # 定时任务模式（静默输出）

定时任务配置 (crontab):
  # 每个交易日 18:00 执行
  0 18 * * 1-5 cd ~/workspace/stock_data && python fetch_and_backup.py --cron

环境变量:
  PG_EXPORT_DSN          PostgreSQL 连接串
  TUSHARE_TOKEN          Tushare Pro token
  GITHUB_TOKEN           可选，用于 API push
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

REPO = Path(__file__).parent
DATA_DIR = REPO / "data"
DAILY_DIR = REPO / "daily"
LOG_FILE = REPO / "logs" / "fetch.log"

# ── 配置 ────────────────────────────────────────────────────────────────────

GITHUB_REMOTE = os.environ.get("GITHUB_REMOTE", "origin")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
MAX_RETRIES = 3
CHUNK_SIZE = 500  # 每批提交的标的数


def log(msg: str, level: str = "INFO", cron: bool = False):
    """记录日志。cron 模式下不输出到终端。"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")
    if not cron or level in ("ERROR", "WARN"):
        print(line)


# ═════════════════════════════════════════════════════════════════════════════
# 数据源: PostgreSQL (quant_sys)
# ═════════════════════════════════════════════════════════════════════════════

def fetch_from_pg(dry_run: bool = False, full: bool = False) -> int:
    """从 PG 导出数据。复用 scripts/export.py。"""
    export_script = REPO / "scripts" / "export.py"
    if not export_script.exists():
        log("scripts/export.py 不存在，无法从 PG 拉取", "ERROR")
        return 0

    cmd = [sys.executable, str(export_script)]
    if full:
        cmd.append("--full")

    log(f"执行: {' '.join(cmd)}")
    if dry_run:
        log("[dry-run] 跳过实际执行", "WARN")
        return 0

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO, timeout=600)
        log(r.stdout, "INFO")
        if r.stderr:
            log(r.stderr, "WARN")
        if r.returncode != 0:
            log(f"export.py 退出码: {r.returncode}", "ERROR")
            return 0
    except subprocess.TimeoutExpired:
        log("export.py 超时 (10分钟)", "ERROR")
        return 0
    except FileNotFoundError:
        log(f"Python 不可用: {sys.executable}", "ERROR")
        return 0

    return 1  # 表示成功


# ═════════════════════════════════════════════════════════════════════════════
# 数据源: Tushare Pro
# ═════════════════════════════════════════════════════════════════════════════

def fetch_from_tushare(dry_run: bool = False) -> int:
    """通过 Tushare Pro API 拉取最新日线。"""
    token = os.environ.get("TUSHARE_TOKEN", "")
    if not token:
        log("未设置 TUSHARE_TOKEN，跳过 Tushare", "WARN")
        return 0

    try:
        import tushare as ts
    except ImportError:
        log("tushare 未安装，跳过", "WARN")
        return 0

    log("通过 Tushare 拉取最新行情...")
    if dry_run:
        log("[dry-run] 跳过", "WARN")
        return 0

    pro = ts.pro_api(token)

    # 获取最新交易日
    try:
        cal_df = pro.trade_cal(exchange="SSE", start_date="20260101", end_date=date.today().strftime("%Y%m%d"))
        latest_trade_dates = cal_df[cal_df["is_open"] == 1]["cal_date"].tail(3).tolist()
    except Exception as e:
        log(f"交易日历获取失败: {e}", "ERROR")
        return 0

    if not latest_trade_dates:
        log("无最近交易日", "WARN")
        return 0

    trade_date = latest_trade_dates[-1]
    log(f"拉取日期: {trade_date}")

    try:
        df = pro.daily(trade_date=trade_date)
        if df is None or df.empty:
            log(f"{trade_date} 无行情数据", "WARN")
            return 0
        log(f"获取 {len(df)} 条行情")

        # 写入 daily/SYMBOL.csv
        new_rows = 0
        for _, row in df.iterrows():
            ts_code = row["ts_code"]
            csv_path = DAILY_DIR / f"{ts_code}.csv"

            new_line = {
                "symbol": ts_code,
                "datetime": f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}",
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "volume": row["vol"],
                "amount": row.get("amount", ""),
            }

            # 追加模式
            csv_path.parent.mkdir(parents=True, exist_ok=True)
            existed = csv_path.exists()
            with open(csv_path, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=list(new_line.keys()))
                if not existed:
                    writer.writeheader()
                writer.writerow(new_line)
            new_rows += 1

        log(f"写入 {new_rows} 行到 daily/")
        return new_rows

    except Exception as e:
        log(f"Tushare 拉取失败: {e}", "ERROR")
        return 0


# ═════════════════════════════════════════════════════════════════════════════
# 数据源: AKShare (免费开源)
# ═════════════════════════════════════════════════════════════════════════════

def fetch_from_akshare(dry_run: bool = False) -> int:
    """通过 AKShare 拉取最新行情（免费，无需 token）。"""
    try:
        import akshare as ak
    except ImportError:
        log("akshare 未安装，跳过", "WARN")
        return 0

    log("通过 AKShare 拉取最新行情...")
    if dry_run:
        log("[dry-run] 跳过", "WARN")
        return 0

    new_rows = 0
    today = date.today().strftime("%Y-%m-%d")

    try:
        # 沪深A股日行情
        df = ak.stock_zh_a_spot_em()
        if df is None or df.empty:
            log("AKShare 返回空数据", "WARN")
            return 0

        # 列映射
        col_map = {"代码": "symbol", "今开": "open", "最高": "high",
                    "最低": "low", "最新价": "close", "成交量": "volume", "成交额": "amount"}

        for _, row in df.iterrows():
            code = str(row.get("代码", ""))
            if not code:
                continue

            # 补全 ts_code 格式
            if "." not in code:
                if code.startswith("6"):
                    ts_code = f"{code}.SH"
                elif code.startswith(("0", "3")):
                    ts_code = f"{code}.SZ"
                else:
                    ts_code = code
            else:
                ts_code = code

            csv_path = DAILY_DIR / f"{ts_code}.csv"
            new_line = {
                "symbol": ts_code,
                "datetime": today,
                "open": row.get("今开", ""),
                "high": row.get("最高", ""),
                "low": row.get("最低", ""),
                "close": row.get("最新价", ""),
                "volume": row.get("成交量", ""),
                "amount": row.get("成交额", ""),
            }

            existed = csv_path.exists()
            with open(csv_path, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=list(new_line.keys()))
                if not existed:
                    writer.writeheader()
                writer.writerow(new_line)
            new_rows += 1

        log(f"AKShare: 写入 {new_rows} 行")
        return new_rows

    except Exception as e:
        log(f"AKShare 拉取失败: {e}", "ERROR")
        return 0


# ═════════════════════════════════════════════════════════════════════════════
# 定向补全: 根据 validate.py 缺失报告拉取
# ═════════════════════════════════════════════════════════════════════════════

def fetch_from_report(report_path: str, dry_run: bool = False) -> int:
    """根据 validate.py --missing-report 输出的 JSON，定向补全缺失数据。

    支持：
    - daily_gaps: 用 AKShare 历史行情补全日线缺口
    - 跳过 no_data_stocks（港股为主，AKShare 无法覆盖）
    - 跳过 financial_gaps（需额外接口）
    """
    try:
        with open(report_path) as f:
            report = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        log(f"无法读取缺失报告: {e}", "ERROR")
        return 0

    daily_gaps = report.get("daily_gaps", [])
    table_gaps = report.get("table_gaps", [])

    # 过滤：只处理 A 股 + 最近 30 天内有数据的（不处理港股和疑似退市的）
    actionable = [g for g in daily_gaps
                  if not g["ts_code"].endswith(".HK")
                  and g.get("days_behind", 999) < 365
                  and g.get("reason") != "已退市(未标记)"]

    if not actionable:
        log("缺失报告中无待补全的 A 股日线数据")
    else:
        log(f"从缺失报告发现 {len(actionable)} 只 A 股需补全日线 (共 {len(daily_gaps)} 只缺失)")
        if dry_run:
            for g in actionable[:10]:
                log(f"  [dry-run] {g['ts_code']} {g['name']} 落后 {g['days_behind']} 天")
            if len(actionable) > 10:
                log(f"  ... 等 {len(actionable)} 只")
        else:
            _fill_daily_gaps(actionable)

    if table_gaps:
        log(f"表级缺失 {len(table_gaps)} 个 (需单独处理):")
        for g in table_gaps:
            log(f"  {g['table']} ({g['name']}): {g['max_date']} → 落后 {g['days_behind']} 天")

    return len(actionable)


def _fill_daily_gaps(stocks: list[dict]) -> int:
    """用 AKShare 历史行情补全指定股票的日线缺口。

    对每只股票，拉取最近 60 天的日线数据，写入 CSV。
    """
    try:
        import akshare as ak
    except ImportError:
        log("akshare 未安装，无法定向补全", "ERROR")
        return 0

    total_new = 0
    for i, s in enumerate(stocks):
        ts_code = s["ts_code"]
        name = s["name"]
        # 去掉后缀获取纯代码
        code = ts_code.split(".")[0]

        try:
            # 拉取历史日线（最近60天足够覆盖缺口）
            df = ak.stock_zh_a_hist(symbol=code, period="daily", adjust="qfq")
            if df is None or df.empty:
                log(f"  [{i+1}/{len(stocks)}] {ts_code} {name}: AKShare 无数据", "WARN")
                continue

            csv_path = DAILY_DIR / f"{ts_code}.csv"

            # 读已有数据，找最新日期
            existing_dates = set()
            if csv_path.exists():
                try:
                    import pandas as pd
                    edf = pd.read_csv(csv_path, usecols=["datetime"])
                    existing_dates = set(str(d) for d in edf["datetime"])
                except Exception:
                    pass

            # 追加新行
            new_for_stock = 0
            with open(csv_path, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"])
                if not csv_path.exists() or csv_path.stat().st_size == 0:
                    writer.writeheader()

                for _, row in df.iterrows():
                    date_str = str(row.get("日期", ""))
                    if not date_str or date_str in existing_dates:
                        continue

                    writer.writerow({
                        "symbol": ts_code,
                        "datetime": date_str,
                        "open": row.get("开盘", ""),
                        "high": row.get("最高", ""),
                        "low": row.get("最低", ""),
                        "close": row.get("收盘", ""),
                        "volume": row.get("成交量", ""),
                        "amount": row.get("成交额", ""),
                    })
                    new_for_stock += 1

            total_new += new_for_stock
            if new_for_stock > 0:
                log(f"  [{i+1}/{len(stocks)}] {ts_code} {name}: +{new_for_stock} 行")

        except Exception as e:
            log(f"  [{i+1}/{len(stocks)}] {ts_code} {name}: 失败 - {e}", "WARN")
            continue

    log(f"定向补全完成: {total_new} 行")
    return total_new


# ═════════════════════════════════════════════════════════════════════════════
# 数据源: 腾讯股票接口 (web.sqt.gtimg.cn)
# ═════════════════════════════════════════════════════════════════════════════

def fetch_from_tencent(dry_run: bool = False) -> int:
    """通过腾讯接口拉取最新行情（免费，无需 token，通常比东财更稳定）。"""
    import requests

    log("通过腾讯接口拉取最新行情...")
    if dry_run:
        log("[dry-run] 跳过", "WARN")
        return 0

    # 收集所有需要更新的标的
    if not DAILY_DIR.exists():
        log("daily/ 目录不存在", "ERROR")
        return 0

    # 从 daily/ 目录读取已有 ts_code 列表（只拉 A 股的沪深标的）
    ts_codes = []
    for f in sorted(DAILY_DIR.glob("*.csv")):
        code = f.stem  # 如 000001.SZ
        if code.endswith((".SH", ".SZ")) and not code.endswith(".BJ"):
            ts_codes.append(code)

    if not ts_codes:
        log("daily/ 目录中无 A 股 CSV", "WARN")
        return 0

    log(f"共 {len(ts_codes)} 只 A 股待拉取")

    # 构建 Tencent 代码映射
    code_map = {}
    for ts_code in ts_codes:
        symbol = ts_code.split(".")[0]
        if ts_code.endswith(".SH"):
            tx_code = f"sh{symbol}"
        else:
            tx_code = f"sz{symbol}"
        code_map[tx_code] = ts_code

    today = date.today().strftime("%Y-%m-%d")
    url = "https://web.sqt.gtimg.cn/q="
    headers = {"User-Agent": "Mozilla/5.0"}
    batch_size = 500
    tx_codes = list(code_map.keys())
    total = len(tx_codes)
    new_rows = 0  # 改为计数行数

    for i in range(0, total, batch_size):
        batch = tx_codes[i:i + batch_size]
        batch_url = url + ",".join(batch)

        try:
            resp = requests.get(batch_url, headers=headers, timeout=60)
            lines = resp.text.strip().split(";")

            for line in lines:
                if '="' not in line:
                    continue
                parts = line.split('="')
                tx_code = parts[0].replace("v_", "")
                data = parts[1].rstrip('"').split("~")

                if len(data) < 40:
                    continue

                ts_code = code_map.get(tx_code)
                if not ts_code:
                    continue

                try:
                    price = float(data[3]) if data[3] else 0
                    pre_close = float(data[4]) if data[4] else 0
                    high = float(data[33]) if data[33] else price
                    low = float(data[34]) if data[34] else price
                    vol = float(data[6]) * 100 if data[6] else 0  # 手 → 股
                    amount = float(data[7]) * 10000 if data[7] else 0  # 万 → 元
                    if high == 0:
                        high = price
                    if low == 0:
                        low = price
                except (ValueError, IndexError):
                    continue

                csv_path = DAILY_DIR / f"{ts_code}.csv"
                new_line = {
                    "symbol": ts_code,
                    "datetime": today,
                    "open": pre_close,  # 腾讯接口无今开字段，用昨收代替
                    "high": high,
                    "low": low,
                    "close": price,
                    "volume": vol,
                    "amount": amount,
                }

                existed = csv_path.exists()
                with open(csv_path, "a", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=list(new_line.keys()))
                    if not existed:
                        writer.writeheader()
                    writer.writerow(new_line)
                new_rows += 1

        except Exception as e:
            log(f"批次 [{i+1}-{min(i+batch_size, total)}] 失败: {e}", "WARN")
            continue

        time.sleep(0.05)

    log(f"腾讯接口: 写入 {new_rows} 行 ({len(ts_codes)} 只标的)")
    return new_rows


def fetch_latest_day(dry_run: bool = False) -> int:
    """仅拉取最新一个交易日的全量行情。

    优先级：腾讯接口 → AKShare（作为回退）。
    """
    rows = fetch_from_tencent(dry_run)
    if rows > 0:
        return rows
    log("腾讯接口返回 0 行，回退到 AKShare", "WARN")
    return fetch_from_akshare(dry_run)


# ═════════════════════════════════════════════════════════════════════════════
# Git 操作
# ═════════════════════════════════════════════════════════════════════════════

def git_status() -> tuple[bool, list[str]]:
    """返回 (has_changes, changed_files)。"""
    try:
        r = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, cwd=REPO, timeout=10)
        lines = [l[3:] for l in r.stdout.strip().split("\n") if l]
        return len(lines) > 0, lines
    except Exception as e:
        log(f"git status 失败: {e}", "ERROR")
        return False, []


def git_pull() -> bool:
    """拉取远程最新。"""
    for i in range(MAX_RETRIES):
        try:
            r = subprocess.run(
                ["git", "pull", "--rebase", GITHUB_REMOTE, GITHUB_BRANCH],
                capture_output=True, text=True, cwd=REPO, timeout=30,
            )
            if r.returncode == 0:
                log(f"git pull 成功: {r.stdout.strip()}")
                return True
            log(f"git pull 失败 (尝试 {i+1}/{MAX_RETRIES}): {r.stderr}", "WARN")
            time.sleep(2)
        except subprocess.TimeoutExpired:
            log(f"git pull 超时 (尝试 {i+1}/{MAX_RETRIES})", "WARN")
            time.sleep(2)
    return False


def git_commit_and_push(dry_run: bool = False, no_push: bool = False) -> bool:
    """提交并推送。"""
    has_changes, files = git_status()
    if not has_changes:
        log("无变更，跳过提交")
        return True

    # 显示变更统计
    stats = {}
    for f in files:
        ext = Path(f).suffix
        stats[ext] = stats.get(ext, 0) + 1
    stats_str = ", ".join(f"{v} {k}" for k, v in sorted(stats.items()))
    log(f"变更: {len(files)} 个文件 ({stats_str})")

    if dry_run:
        log("[dry-run] 跳过 git 操作")
        return True

    # Stage 文件
    today_str = date.today().strftime("%Y-%m-%d")
    try:
        subprocess.run(["git", "add", "-A", "data/", "daily/", "fundamental/", "meta/", "macro/", "index/"],
                       capture_output=True, text=True, cwd=REPO, timeout=30)

        subprocess.run(["git", "add", "scripts/", "validate.py", "mcp_server.py", "fetch_and_backup.py"],
                       capture_output=True, text=True, cwd=REPO, timeout=10)
    except Exception as e:
        log(f"git add 失败: {e}", "ERROR")
        return False

    # Commit
    commit_msg = f"data: {today_str} auto-update\n\n{len(files)} files changed"
    try:
        r = subprocess.run(["git", "commit", "-m", commit_msg],
                           capture_output=True, text=True, cwd=REPO, timeout=30)
        if r.returncode != 0:
            log(f"git commit 失败: {r.stderr}", "ERROR")
            return False
        log(f"git commit: {r.stdout.strip()}")
    except Exception as e:
        log(f"git commit 失败: {e}", "ERROR")
        return False

    # Push
    if no_push:
        log("--no-push 模式，跳过推送")
        return True

    for i in range(MAX_RETRIES):
        try:
            r = subprocess.run(
                ["git", "push", GITHUB_REMOTE, GITHUB_BRANCH],
                capture_output=True, text=True, cwd=REPO, timeout=60,
            )
            if r.returncode == 0:
                log(f"git push 成功 (尝试 {i+1})")
                return True
            log(f"git push 失败 (尝试 {i+1}/{MAX_RETRIES}): {r.stderr.strip()}", "WARN")
            time.sleep(3)
        except subprocess.TimeoutExpired:
            log(f"git push 超时 (尝试 {i+1}/{MAX_RETRIES})", "WARN")
            time.sleep(3)

    return False


# ═════════════════════════════════════════════════════════════════════════════
# 主流程
# ═════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="stock_data 数据拉取 & GitHub 备份")
    parser.add_argument("--full", action="store_true", help="全量重建")
    parser.add_argument("--dry-run", action="store_true", help="预览，不实际写入/提交")
    parser.add_argument("--source", choices=["pg", "tushare", "akshare", "tencent", "all"], default="all",
                        help="数据源 (默认: all，按 pg→tushare→akshare→tencent 优先级)")
    parser.add_argument("--no-push", action="store_true", help="拉取但不推送")
    parser.add_argument("--cron", action="store_true", help="定时任务模式（静默 + 仅增量）")
    parser.add_argument("--skip-validate", action="store_true", help="跳过数据校验")
    parser.add_argument("--latest", action="store_true", help="仅拉取最新一个交易日（快捷模式）")
    parser.add_argument("--from-report", help="根据 validate.py --missing-report 输出的 JSON 定向补全缺失数据")
    args = parser.parse_args()

    log("=== stock_data fetch & backup ===")
    if args.from_report:
        log(f"模式: 定向补全 | 报告: {args.from_report}")
    elif args.latest:
        log("模式: 仅最新日")
    else:
        log(f"模式: {'FULL' if args.full else 'INCREMENTAL'} | 源: {args.source} | dry-run: {args.dry_run}")

    # ── Step 1: Pull latest from GitHub ──
    if not args.dry_run and not args.from_report:
        log("[1/4] 同步远程...")
        git_pull()

    # ── Step 2: Fetch data ──
    if args.from_report:
        log("[2/4] 定向补全缺失数据...")
        new_rows = fetch_from_report(args.from_report, args.dry_run)
    elif args.latest:
        log("[2/4] 拉取最新交易日...")
        new_rows = fetch_latest_day(args.dry_run)
    else:
        log("[2/4] 拉取数据...")
        new_rows = 0

        if args.source in ("pg", "all"):
            pg_rows = fetch_from_pg(args.dry_run, full=args.full)
            if isinstance(pg_rows, int):
                new_rows += pg_rows
                if pg_rows > 0:
                    log(f"  PG 导出完成")

        if args.source in ("tushare", "all"):
            if new_rows == 0:
                ts_rows = fetch_from_tushare(args.dry_run)
                new_rows += ts_rows

        if args.source in ("akshare", "all"):
            if new_rows == 0:
                ak_rows = fetch_from_akshare(args.dry_run)
                new_rows += ak_rows

        if args.source in ("tencent", "all"):
            if new_rows == 0:
                tx_rows = fetch_from_tencent(args.dry_run)
                new_rows += tx_rows

    log(f"  数据拉取完成, 总新增: {new_rows} 行" if isinstance(new_rows, int) else f"  数据拉取完成")

    # ── Step 3: Validate ──
    if not args.skip_validate and not args.dry_run and (isinstance(new_rows, int) and new_rows > 0):
        log("[3/4] 校验数据...")
        try:
            r = subprocess.run(
                [sys.executable, str(REPO / "validate.py"), "--quick"],
                capture_output=True, text=True, cwd=REPO, timeout=300,
            )
            if r.returncode != 0:
                log(f"校验发现问题:\n{r.stdout[-500:]}", "WARN")
            else:
                log("校验通过 ✅")
        except Exception as e:
            log(f"校验失败: {e}", "WARN")
    else:
        log("[3/4] 跳过校验" if args.skip_validate else "[3/4] 跳过校验 (dry-run或无新数据)")

    # ── Step 4: Git commit & push ──
    log("[4/4] Git 提交...")
    success = git_commit_and_push(args.dry_run, args.no_push)

    if success:
        log("✅ 完成" if not args.dry_run else "✅ [dry-run] 完成")
    else:
        log("❌ 推送失败", "ERROR")
        sys.exit(1)


if __name__ == "__main__":
    main()

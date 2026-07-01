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
    parser.add_argument("--source", choices=["pg", "tushare", "akshare", "all"], default="all",
                        help="数据源 (默认: all，按 pg→tushare→akshare 优先级)")
    parser.add_argument("--no-push", action="store_true", help="拉取但不推送")
    parser.add_argument("--cron", action="store_true", help="定时任务模式（静默 + 仅增量）")
    parser.add_argument("--skip-validate", action="store_true", help="跳过数据校验")
    args = parser.parse_args()

    log("=== stock_data fetch & backup ===")
    log(f"模式: {'FULL' if args.full else 'INCREMENTAL'} | 源: {args.source} | dry-run: {args.dry_run}")

    # ── Step 1: Pull latest from GitHub ──
    if not args.dry_run:
        log("[1/4] 同步远程...")
        git_pull()

    # ── Step 2: Fetch data ──
    log("[2/4] 拉取数据...")
    new_rows = 0

    if args.source in ("pg", "all"):
        pg_rows = fetch_from_pg(args.dry_run, full=args.full)
        new_rows += pg_rows
        if isinstance(pg_rows, int) and pg_rows > 0:
            log(f"  PG 导出完成, 获取 {pg_rows} 行")

    if args.source in ("tushare", "all"):
        if new_rows == 0:  # 仅当 PG 无数据时用 Tushare
            ts_rows = fetch_from_tushare(args.dry_run)
            new_rows += ts_rows

    if args.source in ("akshare", "all"):
        if new_rows == 0:  # 最后备选
            ak_rows = fetch_from_akshare(args.dry_run)
            new_rows += ak_rows

    log(f"  数据拉取完成, 总新增: {new_rows} 行")

    # ── Step 3: Validate ──
    if not args.skip_validate and not args.dry_run and new_rows > 0:
        log("[3/4] 校验数据...")
        try:
            r = subprocess.run(
                [sys.executable, str(REPO / "validate.py"), "--quick", "--skip-git"],
                capture_output=True, text=True, cwd=REPO, timeout=300,
            )
            if r.returncode != 0:
                log(f"校验发现问题:\n{r.stdout[-500:]}", "WARN")
            else:
                log("校验通过")
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

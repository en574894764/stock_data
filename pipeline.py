#!/usr/bin/env python3
"""stock_data 数据管道编排器 — validate → fix → fetch → backup → report

用法: python pipeline.py [--cron] [--dry-run] [--force] [--max-rounds N]
"""
from __future__ import annotations

import argparse, json, os, subprocess, sys, time
from datetime import date, datetime
from pathlib import Path

from report_builder import collect_db_stats, generate_report, build_feishu_report, push_feishu_report

REPO = Path(__file__).parent
LOGS = REPO / "logs"
LOG_FILE = LOGS / "pipeline.log"
PY = sys.executable
SUCCESS, FAIL, WARN = "✅", "❌", "⚠️"


def log(msg: str, level: str = "INFO", cron: bool = False):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    LOGS.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")
    if not cron or level in ("ERROR", "WARN"):
        print(line)


def run(cmd: list[str], timeout: int = 300, cron: bool = False) -> subprocess.CompletedProcess:
    log(f"执行: {' '.join(cmd)}", "DEBUG", cron)
    try:
        return subprocess.run(cmd, capture_output=True, text=True, cwd=REPO, timeout=timeout)
    except subprocess.TimeoutExpired:
        log(f"超时: {' '.join(cmd)}", "ERROR")
        return subprocess.CompletedProcess(cmd, -1, "", f"Timeout after {timeout}s")


def is_trading_day(d: date | None = None) -> bool:
    if d is None: d = date.today()
    try:
        import psycopg2
        c = psycopg2.connect(host="/tmp", dbname="investassist", user="james", connect_timeout=3)
        cur = c.cursor()
        cur.execute("SELECT is_open::int FROM trade_cal WHERE cal_date = %s", (d.isoformat(),))
        row = cur.fetchone()
        cur.close(); c.close()
        return row is not None and row[0] == 1
    except Exception:
        return d.weekday() < 5


# ── Pipeline steps ──

def step_validate(missing_path: str, quick: bool = False, cron: bool = False) -> dict | None:
    args = [PY, str(REPO / "validate.py"), "--missing-report", missing_path]
    if quick: args.append("--quick")
    r = run(args, timeout=300, cron=cron)
    try:
        with open(missing_path) as f: return json.load(f)
    except Exception: return None


def step_fetch_gaps(missing_path: str, cron: bool = False) -> bool:
    r = run([PY, str(REPO / "fetch_and_backup.py"), "--from-report", missing_path, "--no-push"],
            timeout=600, cron=cron)
    return r.returncode == 0


def step_fetch_latest(cron: bool = False) -> bool:
    r = run([PY, str(REPO / "fetch_and_backup.py"), "--latest", "--no-push"],
            timeout=300, cron=cron)
    return r.returncode == 0


def step_backfill_financial(cron: bool = False) -> bool:
    """财报回补：补全缺失的利润表/资产负债表/现金流量表"""
    log("财报回补...")
    r = run([PY, str(REPO / "backfill_financial.py"), "--max", "100"],
            timeout=1800, cron=cron)
    return r.returncode == 0


def step_backup(cron: bool = False) -> bool:
    import subprocess as sp
    log("GitHub 备份...")
    # sync CSV → DB
    sp.run(["git", "add", "daily/", "fundamental/", "meta/", "macro/", "index/", "scripts/", "*.py"],
           capture_output=True, text=True, cwd=REPO, timeout=30)

    sr = sp.run(["git", "status", "--porcelain"], capture_output=True, text=True, cwd=REPO, timeout=10)
    changed = [l for l in sr.stdout.strip().split("\n") if l]
    if not changed: log("无变更"); return True

    today_str = date.today().strftime("%Y-%m-%d")
    sp.run(["git", "commit", "-m", f"data: {today_str} pipeline auto-update\n\n{len(changed)} files changed"],
           capture_output=True, text=True, cwd=REPO, timeout=30)
    sp.run(["git", "pull", "--rebase", "origin", "main"], capture_output=True, text=True, cwd=REPO, timeout=30)

    for i in range(3):
        pr = sp.run(["git", "push", "origin", "main"], capture_output=True, text=True, cwd=REPO, timeout=120)
        if pr.returncode == 0: log(f"push ✅"); return True
        log(f"push retry {i+1}", "WARN"); time.sleep(3)
    return False


# ── main ──

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--cron", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--max-rounds", type=int, default=3)
    p.add_argument("--skip-backup", action="store_true")
    args = p.parse_args()

    today = date.today()
    missing_path = str(LOGS / f"missing_{today.strftime('%Y%m%d')}.json")

    log(f"Pipeline 启动 | {today} | {'交易日' if is_trading_day(today) else '非交易日'} | "
        f"模式: {'DRY-RUN' if args.dry_run else 'CRON' if args.cron else 'NORMAL'}")

    if not is_trading_day(today) and not args.force:
        log("非交易日，仅校验", cron=args.cron)
        step_validate(missing_path, cron=args.cron)
        stats = collect_db_stats()
        rp = generate_report(stats, False)
        push_feishu_report(build_feishu_report(stats, False))
        return 0

    if args.dry_run:
        log("=== DRY RUN ===", cron=args.cron)
        return 0

    # ── 主流程 ──
    start = time.time()
    before_stats = collect_db_stats()

    report = step_validate(missing_path, cron=args.cron)
    if not report: return 1
    stale = report.get("summary", {}).get("stale_daily", 0)
    log(f"初始: {stale} 只缺失")

    for rnd in range(1, args.max_rounds + 1):
        log(f"--- 第 {rnd}/{args.max_rounds} 轮 ---")
        if stale > 0:
            step_fetch_gaps(missing_path, args.cron)
        step_fetch_latest(args.cron)
        # 财报回补（每轮跑一次，自动发现缺口并补全）
        step_backfill_financial(args.cron)
        report = step_validate(missing_path, cron=args.cron)
        stale = report.get("summary", {}).get("stale_daily", 0) if report else 999
        log(f"第 {rnd} 轮: {stale} 只缺失")
        if stale == 0:
            log(f"✅ 通过！"); break

    github_ok = step_backup(args.cron) if not args.skip_backup else False
    stats = collect_db_stats()

    # 增量
    inc = {}
    for tbl in ["daily_quote", "income", "balance_sheet", "cashflow"]:
        b = before_stats.get(tbl, {}).get("rows", 0)
        a = stats.get(tbl, {}).get("rows", 0)
        if b > 0: inc[tbl] = a - b

    report_path = generate_report(stats, github_ok)
    push_feishu_report(build_feishu_report(stats, github_ok))

    elapsed = time.time() - start
    log(f"\n{'='*60}\n{SUCCESS} 完成 | {elapsed:.0f}s | 日线 {stats.get('daily_ok', 0)}/{stats.get('daily_total', 0)} "
        f"| GitHub {SUCCESS if github_ok else FAIL}\n报告: {report_path}\n{'='*60}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

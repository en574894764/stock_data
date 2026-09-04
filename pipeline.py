#!/usr/bin/env python3
"""stock_data 数据管道编排器 — validate → fetch → backfill → macro → export → backup → report

M5 改造（修复方案 2026-08-30）：
  - fetch 走 fetch_and_backup.py v2（PG 单向流 + 区间自愈），不再有 --from-report 循环
  - 新增宏观步骤 fetch_macro.py
  - export 失败 → 飞书告警 + 非零退出
  - 收敛判据：轮间 stale 无下降即 break + 告警（杜绝空转）
  - 飞书报告含各数据域新鲜度 + 缺口 TOP10

用法: python pipeline.py [--cron] [--dry-run] [--force] [--max-rounds N]
"""
from __future__ import annotations

import argparse, json, os, subprocess, sys, time
from datetime import date, datetime
from pathlib import Path

from report_builder import (collect_db_stats, generate_report, build_feishu_report,
                            push_feishu_report, push_feishu_alert)

REPO = Path(__file__).parent
LOGS = REPO / "logs"
LOG_FILE = LOGS / "pipeline.log"
PY = sys.executable
SUCCESS, FAIL, WARN = "✅", "❌", "⚠️"


def load_env(env_path: Path | None = None) -> None:
    """读取 .env（TUSHARE_TOKEN 等密钥），注入环境变量供子进程继承。不覆盖已有值。"""
    path = env_path or (REPO / ".env")
    if not path.exists():
        return
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"\''))
    except Exception as e:
        log(f"读取 .env 失败: {e}", "WARN")


def log(msg: str, level: str = "INFO", cron: bool = False):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    LOGS.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")
    if not cron or level in ("ERROR", "WARN"):
        print(line)


def run(cmd: list[str], timeout: int = 300, cron: bool = False) -> subprocess.CompletedProcess:
    cmd = [c for c in cmd if c]  # 过滤空参数
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

def step_validate(missing_path: str, cron: bool = False) -> dict | None:
    """全量校验（核心 GROUP BY 查询 <10s，无需 --quick 抽查）。"""
    r = run([PY, str(REPO / "validate.py"), "--missing-report", missing_path],
            timeout=600, cron=cron)
    try:
        with open(missing_path) as f: return json.load(f)
    except Exception:
        log(f"validate 未产出缺失报告 (exit={r.returncode}): {r.stdout[-300:]}", "ERROR", cron)
        return None


def step_fetch(cron: bool = False) -> bool:
    """数据拉取（A股/港股/ETF/指数 → PG，区间自愈）。"""
    log("数据拉取 (fetch_and_backup → PG)...")
    r = run([PY, str(REPO / "fetch_and_backup.py"), "--skip-export", "--skip-git"]
            + (["--cron"] if cron else []),
            timeout=3600, cron=cron)
    ok = r.returncode == 0
    if not ok:
        log(f"fetch 失败 (exit={r.returncode}): {r.stderr[-300:]}", "ERROR", cron)
    return ok


def step_backfill_financial(cron: bool = False) -> bool:
    """财报回补（已过披露截止日的报告期自动发现缺口）。"""
    log("财报回补...")
    r = run([PY, str(REPO / "backfill_financial.py")],
            timeout=1800, cron=cron)
    ok = r.returncode == 0
    if not ok:
        log(f"财报回补失败 (exit={r.returncode}): {r.stderr[-300:]}", "WARN", cron)
    return ok


def step_fetch_macro(cron: bool = False) -> bool:
    """宏观指标 → macro/*.csv 直写。"""
    log("宏观拉取...")
    r = run([PY, str(REPO / "fetch_macro.py")], timeout=600, cron=cron)
    ok = r.returncode == 0
    if not ok:
        log(f"宏观拉取失败 (exit={r.returncode}): {r.stderr[-300:]}", "WARN", cron)
    return ok


def step_export(cron: bool = False) -> bool:
    """PG → CSV 导出。失败 → 告警 + 非零。"""
    log("导出 CSV (daily/ + index/ + data/)...")
    r = run([PY, str(REPO / "scripts" / "export.py")], timeout=3600, cron=cron)
    ok = r.returncode == 0
    if not ok:
        log(f"export 失败 (exit={r.returncode}): {r.stderr[-500:]}", "ERROR", cron)
    return ok


def step_factor(cron: bool = False) -> bool:
    """因子更新: daily_basic 增量拉取 + 因子值增量计算 + 拥挤度监控刷新 (PG-only, 不入 git)。
    失败仅告警不阻塞主链路 — 因子是可再生数据, 次日自愈。"""
    log("因子更新 (daily_basic + factor_value)...")
    r1 = run([PY, str(REPO / "backfill_daily_basic.py"), "--days", "3"], timeout=900, cron=cron)
    if r1.returncode != 0:
        log(f"daily_basic 增量失败 (exit={r1.returncode}): {r1.stderr[-300:]}", "WARN", cron)
        return False
    r2 = run([PY, str(REPO / "scripts" / "compute_factors.py")], timeout=1800, cron=cron)
    if r2.returncode != 0:
        log(f"因子计算失败 (exit={r2.returncode}): {r2.stderr[-300:]}", "WARN", cron)
        return False
    r3 = run([PY, str(REPO / "scripts" / "crowding_monitor.py")], timeout=1200, cron=cron)
    if r3.returncode != 0:
        log(f"拥挤度监控失败 (exit={r3.returncode}): {r3.stderr[-300:]}", "WARN", cron)
    return True


def step_signals(cron: bool = False) -> bool:
    """策略信号生成 (非阻塞): 到调仓期才产出信号, 平日自动跳过; 产出时飞书推送执行建议。"""
    log("策略信号生成 (generate_signals)...")
    r = run([PY, str(REPO / "scripts" / "generate_signals.py"), "--push"], timeout=600, cron=cron)
    ok = r.returncode == 0
    if not ok:
        log(f"信号生成失败 (exit={r.returncode}): {r.stderr[-300:]}", "WARN", cron)
    return ok


def step_reconcile(cron: bool = False) -> bool:
    """L2 周度对账质检（周六追加）：PG↔CSV 对账 / 生命周期 / 财报合理性 / 分布漂移。"""
    log("L2 对账质检 (reconcile)...")
    r = run([PY, str(REPO / "scripts" / "reconcile.py")], timeout=1200, cron=cron)
    ok = r.returncode == 0
    if not ok:
        log(f"L2 对账发现 ERR 级问题:\n{r.stdout[-800:]}", "ERROR", cron)
        push_feishu_alert(
            f"**{FAIL} L2 对账质检发现问题**\n"
            f"{r.stdout[-600:]}")
    return ok


def step_backup(cron: bool = False) -> bool:
    import subprocess as sp
    log("GitHub 备份...")
    # launchd Background 进程下 git 操作被调度器拖慢 10~30 倍：
    # 手动 ~13s 的 add 在 launchd 下 60s+，4s 的 commit 可 120s+（2026-09-01 实测超时）
    # → 所有 git 子进程 timeout 统一给足余量
    r = sp.run(["git", "add", "data/", "daily/", "fundamental/", "meta/", "macro/", "index/",
                "scripts/", "*.py", ".gitignore"],
               capture_output=True, text=True, cwd=REPO, timeout=600)
    if r.returncode != 0:
        log(f"git add 失败 (exit={r.returncode}): {r.stderr[-200:]}", "ERROR", cron)
        return False

    sr = sp.run(["git", "status", "--porcelain"], capture_output=True, text=True, cwd=REPO, timeout=60)
    changed = [l for l in sr.stdout.strip().split("\n") if l]
    if not changed: log("无变更"); return True

    today_str = date.today().strftime("%Y-%m-%d")
    cr = sp.run(["git", "commit", "-m", f"data: {today_str} pipeline auto-update\n\n{len(changed)} files changed"],
                capture_output=True, text=True, cwd=REPO, timeout=600)
    if cr.returncode != 0:
        log(f"git commit 失败 (exit={cr.returncode}): {cr.stderr[-200:]}", "ERROR", cron)
        return False
    sp.run(["git", "pull", "--rebase", "origin", "main"], capture_output=True, text=True, cwd=REPO, timeout=300)

    for i in range(3):
        pr = sp.run(["git", "push", "origin", "main"], capture_output=True, text=True, cwd=REPO, timeout=600)
        if pr.returncode == 0: log(f"push ✅ ({len(changed)} files)"); return True
        log(f"push retry {i+1}: {pr.stderr[-200:]}", "WARN"); time.sleep(5)
    log("push 失败 3 次，放弃（备份未完成）", "ERROR", cron)
    return False


# ── main ──

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--cron", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--max-rounds", type=int, default=2)
    p.add_argument("--skip-backup", action="store_true")
    p.add_argument("--skip-fetch", action="store_true")
    args = p.parse_args()

    load_env()

    today = date.today()
    missing_path = str(LOGS / f"missing_{today.strftime('%Y%m%d')}.json")

    log(f"Pipeline 启动 | {today} | {'交易日' if is_trading_day(today) else '非交易日'} | "
        f"模式: {'DRY-RUN' if args.dry_run else 'CRON' if args.cron else 'NORMAL'}")

    # ── 非交易日：仅校验 + 宏观 + 报告 ──
    if not is_trading_day(today) and not args.force:
        log("非交易日，仅校验 + 宏观 + 报告", cron=args.cron)
        report = step_validate(missing_path, cron=args.cron)
        step_fetch_macro(args.cron)
        stats = collect_db_stats()
        rp = generate_report(stats, False)
        ok = push_feishu_report(build_feishu_report(stats, False, report))
        log(f"飞书推送: {'✅ 成功' if ok else '❌ 失败'}", cron=args.cron)
        log(f"非交易日校验完成 | 报告: {rp}", cron=args.cron)
        return 0

    if args.dry_run:
        log("=== DRY RUN ===", cron=args.cron)
        report = step_validate(missing_path, cron=args.cron)
        if report:
            s = report.get("summary", {})
            log(f"dry-run: stale_daily={s.get('stale_daily')} no_data={s.get('no_data_stocks')} "
                f"table_gaps={s.get('table_gaps')}")
        return 0

    # ── 主流程 ──
    start = time.time()
    before_stats = collect_db_stats()
    failures = []

    report = step_validate(missing_path, cron=args.cron)
    if not report: return 1
    stale = report.get("summary", {}).get("stale_daily", 0)
    log(f"初始: {stale} 只缺失")

    for rnd in range(1, args.max_rounds + 1):
        log(f"--- 第 {rnd}/{args.max_rounds} 轮 ---")
        if not args.skip_fetch:
            if not step_fetch(args.cron):
                failures.append("fetch")
        if not step_backfill_financial(args.cron):
            failures.append("backfill_financial")
        if not step_fetch_macro(args.cron):
            failures.append("fetch_macro")

        report = step_validate(missing_path, cron=args.cron)
        new_stale = report.get("summary", {}).get("stale_daily", 999) if report else 999
        log(f"第 {rnd} 轮: {new_stale} 只缺失 (上轮 {stale})")

        # 收敛判据：无下降即停（真实停牌/退市股 stale 不会归零）
        if new_stale == 0:
            log("✅ 全部新鲜")
            break
        if new_stale >= stale:
            # 区分"源端无更新"（系统正常）与"系统异常"（需介入）
            # 源端无更新常见：akshare 港股小盘股断档、港交所休市、长假后源未刷新
            log(f"⚠️ 轮间无改善 ({stale} → {new_stale})，停止迭代。多属源端无更新（akshare 港股断档/港交所休市），"
                f"少数为真停牌/退市/无源标的。", "WARN")
            push_feishu_alert(
                f"**{WARN} pipeline 收敛告警（信息性，非系统异常）**\n"
                f"stale {stale} → {new_stale} 无改善，已停止迭代。\n"
                f"经验上多为 akshare 源端对部分港股标的近 N 天无更新（小盘股/低流动性），\n"
                f"或港交所休市日属正常，非拉取逻辑故障。\n"
                f"如需排查某只具体标的，看 `logs/fetch.log` 末尾该 ts_code 处理记录。\n"
                f"剩余缺口明细见 {missing_path}")
            break
        stale = new_stale

    # ── 导出 ──
    if not step_export(args.cron):
        failures.append("export")
        push_feishu_alert(f"**{FAIL} pipeline export 失败**\n数据已写入 PG，CSV 导出失败，"
                          f"请手动重跑 scripts/export.py")

    # ── 因子更新 (非阻塞, 失败仅告警) ──
    if not args.skip_backup and not step_factor(args.cron):
        push_feishu_alert("**{WARN} pipeline 因子更新失败 (非阻塞)**\n"
                          "daily_basic/factor_value 更新未完成, 明日自动重试补齐。")

    # ── 策略信号 (非阻塞): 依赖当日因子, 平日跳过, 调仓日产出执行建议并推送飞书 ──
    if not args.skip_backup:
        step_signals(args.cron)

    github_ok = step_backup(args.cron) if not args.skip_backup else False
    stats = collect_db_stats()

    # 周六追加 L2 对账质检
    if today.weekday() == 5:
        if not step_reconcile(args.cron):
            failures.append("reconcile")

    # 增量
    inc = {}
    for tbl in ["daily_quote", "income", "balance_sheet", "cashflow"]:
        b = before_stats.get(tbl, {}).get("rows", 0)
        a = stats.get(tbl, {}).get("rows", 0)
        if b > 0: inc[tbl] = a - b

    report_path = generate_report(stats, github_ok)
    push_feishu_report(build_feishu_report(stats, github_ok, report))

    elapsed = time.time() - start
    log(f"\n{'='*60}\n{SUCCESS if not failures else FAIL} 完成 | {elapsed:.0f}s | "
        f"A股日线 {stats.get('daily_ok', 0)}/{stats.get('daily_total', 0)} | "
        f"港股日线 {stats.get('hk_daily_ok', 0)}/{stats.get('hk_daily_total', 0)} | "
        f"GitHub {SUCCESS if github_ok else FAIL} | 失败步骤: {failures or '无'}\n"
        f"报告: {report_path}\n{'='*60}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

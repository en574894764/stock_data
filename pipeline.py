#!/usr/bin/env python3
"""stock_data 数据管道编排器 — 一键执行完整的数据校验→补全→拉取→备份流程

流程:
  1. 获取今日日期，判断是否为交易日
  2. validate.py → 生成缺失报告
  3. fetch_and_backup.py --from-report → 定向补全
  4. fetch_and_backup.py --latest → 拉取今日数据
  5. validate.py --quick → 快速校验
  6. 不通过 → 回到步骤 3（最多 3 轮）
  7. 通过 → Git commit & push 到 GitHub
  8. 生成最终报告

用法:
  python pipeline.py                   # 全流程执行
  python pipeline.py --dry-run         # 预览
  python pipeline.py --cron            # 定时模式（静默输出，仅写日志）
  python pipeline.py --force           # 非交易日也执行
  python pipeline.py --max-rounds 5    # 最大重试轮次
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests

REPO = Path(__file__).parent
SCRIPTS = REPO
LOGS = REPO / "logs"
LOG_FILE = LOGS / "pipeline.log"
REPORT_DIR = REPO / "reports"
SETTINGS_PATH = Path.home() / ".workbuddy" / "settings.json"

SUCCESS = "✅"
FAIL = "❌"
WARN = "⚠️"


def log(msg: str, level: str = "INFO", cron: bool = False):
    """统一日志：同时输出终端和日志文件"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    LOGS.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")
    if not cron or level in ("ERROR", "WARN"):
        print(line)


def run(cmd: list[str], timeout: int = 300, cron: bool = False) -> subprocess.CompletedProcess:
    """运行子进程，记录输出"""
    log(f"执行: {' '.join(cmd)}", "DEBUG", cron)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO, timeout=timeout)
        if r.returncode != 0 and r.stderr:
            log(r.stderr.strip()[-200:], "WARN", cron)
        return r
    except subprocess.TimeoutExpired:
        log(f"超时 (>{timeout}s): {' '.join(cmd)}", "ERROR", cron)
        r = subprocess.CompletedProcess(cmd, -1, "", f"Timeout after {timeout}s")
        return r


# ═══════════════════════════════════════════════════════════════════════════
# Step 0: 交易日判断
# ═══════════════════════════════════════════════════════════════════════════

def is_trading_day(check_date: date | None = None) -> bool:
    """查询 PostgreSQL 判断是否为交易日"""
    if check_date is None:
        check_date = date.today()

    try:
        import psycopg2
        conn = psycopg2.connect(
            host=os.environ.get("PGHOST", "/tmp"),
            dbname=os.environ.get("PGDATABASE", "investassist"),
            user=os.environ.get("PGUSER", "james"),
            password=os.environ.get("PGPASSWORD", ""),
            connect_timeout=3,
        )
        cur = conn.cursor()
        cur.execute("SELECT is_open::int FROM trade_cal WHERE cal_date = %s", (check_date.isoformat(),))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return row is not None and row[0] == 1
    except Exception as e:
        log(f"交易日查询失败: {e}", "WARN")
        # 简单判断：周一到周五
        return check_date.weekday() < 5


# ═══════════════════════════════════════════════════════════════════════════
# Step 1-5: 核心流程
# ═══════════════════════════════════════════════════════════════════════════

def step_validate_full(missing_path: str, cron: bool = False) -> dict | None:
    """Step 1: 全量校验 + 生成缺失报告"""
    log("Step 1/5: 全量校验...")
    r = run([sys.executable, str(SCRIPTS / "validate.py"), "--missing-report", missing_path],
            timeout=300, cron=cron)

    if r.returncode != 0:
        log(f"校验发现问题 (code={r.returncode})")

    try:
        with open(missing_path) as f:
            report = json.load(f)
        return report
    except Exception as e:
        log(f"无法读取缺失报告: {e}", "ERROR")
        return None


def step_fill_gaps(missing_path: str, cron: bool = False) -> bool:
    """Step 2: 定向补全缺失数据"""
    log("Step 2/5: 定向补全...")
    r = run([sys.executable, str(SCRIPTS / "fetch_and_backup.py"),
             "--from-report", missing_path, "--no-push"], timeout=600, cron=cron)
    return r.returncode == 0


def step_fetch_latest(cron: bool = False) -> bool:
    """Step 3: 拉取今日最新数据"""
    log("Step 3/5: 拉取今日最新...")
    r = run([sys.executable, str(SCRIPTS / "fetch_and_backup.py"),
             "--latest", "--no-push"], timeout=300, cron=cron)
    return r.returncode == 0


def step_validate_quick(cron: bool = False) -> tuple[bool, int, int]:
    """Step 4: 快速校验，返回 (通过, 错误数, 警告数)"""
    log("Step 4/5: 快速校验...")
    r = run([sys.executable, str(SCRIPTS / "validate.py"), "--quick"], timeout=120, cron=cron)

    passed = r.returncode == 0
    errors = 0
    warnings = 0
    for line in r.stdout.split("\n"):
        if "❌" in line and "错误" in line:
            parts = line.split()
            for i, p in enumerate(parts):
                if p == "❌" and i + 1 < len(parts):
                    try:
                        errors = int(parts[i + 1])
                    except ValueError:
                        pass
                if p == "⚠️" and i + 1 < len(parts):
                    try:
                        warnings = int(parts[i + 1])
                    except ValueError:
                        pass

    return passed, errors, warnings


# ═══════════════════════════════════════════════════════════════════════════
# Step 6: GitHub 备份
# ═══════════════════════════════════════════════════════════════════════════

def step_backup(cron: bool = False) -> bool:
    """Step 5: Git commit & push + 更新 PostgreSQL"""
    log("Step 5/5: GitHub 备份...")

    # 1) 更新 PostgreSQL 数据库（将 CSV 新数据同步回 DB）
    log("  同步 CSV → PostgreSQL...")
    r = run([sys.executable, str(SCRIPTS / "scripts" / "gap_fill.py"), "--max-stocks", "0"],
            timeout=600, cron=cron)
    if r.returncode != 0:
        log("  CSV→DB 同步有警告，继续...", "WARN")

    # 2) Git 操作
    import subprocess as sp

    # 获取变更统计
    try:
        sr = sp.run(["git", "status", "--porcelain"], capture_output=True, text=True, cwd=REPO, timeout=10)
        changed = [l for l in sr.stdout.strip().split("\n") if l]
        log(f"  Git 变更: {len(changed)} 个文件")
    except Exception:
        changed = []

    if not changed:
        log("  无变更，跳过 Git 提交")
        return True

    # Stage daily & meta
    for pattern in ["daily/", "fundamental/", "meta/", "macro/", "index/", "scripts/", "*.py"]:
        sp.run(["git", "add", pattern], capture_output=True, text=True, cwd=REPO, timeout=30)

    # Commit
    today_str = date.today().strftime("%Y-%m-%d")
    commit_msg = f"data: {today_str} pipeline auto-update\n\n{len(changed)} files changed"
    cr = sp.run(["git", "commit", "-m", commit_msg], capture_output=True, text=True, cwd=REPO, timeout=30)
    if cr.returncode != 0:
        log(f"  Git commit 失败: {cr.stderr.strip()}", "ERROR")
        return False
    log(f"  Git commit: {cr.stdout.strip()}")

    # Pull & Push
    sp.run(["git", "pull", "--rebase", "origin", "main"], capture_output=True, text=True, cwd=REPO, timeout=30)

    for attempt in range(3):
        pr = sp.run(["git", "push", "origin", "main"], capture_output=True, text=True, cwd=REPO, timeout=60)
        if pr.returncode == 0:
            log(f"  Git push 成功 (尝试 {attempt + 1})")
            return True
        log(f"  Git push 失败 (尝试 {attempt + 1}): {pr.stderr.strip()[-100:]}", "WARN")
        time.sleep(3)

    log("Git push 最终失败", "ERROR")
    return False


# ═══════════════════════════════════════════════════════════════════════════
# 最终报告
# ═══════════════════════════════════════════════════════════════════════════

def generate_report(db_stats: dict, github_status: bool, before_stats: dict | None = None, cron: bool = False) -> str:
    """生成最终数据报告，包含增量、分布、完整性"""
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    today_str = date.today().strftime("%Y%m%d")
    report_path = REPORT_DIR / f"pipeline_report_{today_str}.md"

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 计算增量
    increments = {}
    if before_stats:
        for tbl in ["daily_quote", "income", "balance_sheet", "cashflow"]:
            before = before_stats.get(tbl, {}).get("rows", 0)
            after = db_stats.get(tbl, {}).get("rows", 0)
            if before > 0:
                increments[tbl] = after - before

    lines = [
        f"# 📊 stock_data 数据管道报告",
        f"",
        f"**日期**: {date.today()}  ",
        f"**执行时间**: {now}  ",
        f"**GitHub 备份**: {SUCCESS if github_status else FAIL} {'成功' if github_status else '失败'}",
        f"",
    ]

    # ── 数据增量 ──
    if increments:
        lines.extend([
            f"---",
            f"## 📈 本次增量",
            f"",
            f"| 表 | 新增行数 |",
            f"|-----|----------|",
        ])
        for tbl, inc in increments.items():
            icon = "📈" if inc > 0 else "➖"
            color = "+" if inc > 0 else ""
            lines.append(f"| {tbl} | {icon} {color}{inc:,} |")
        lines.append("")

    # ── 数据分布 ──
    lines.extend([
        f"---",
        f"## 📊 数据分布",
        f"",
    ])

    # 日线新鲜度分布
    fresh = db_stats.get("daily_fresh", {})
    if fresh:
        lines.append("### 日线新鲜度")
        lines.append("| 新鲜度 | 标的数 | 占比 |")
        lines.append("|--------|--------|------|")
        total_daily = sum(fresh.values())
        for label, cnt in fresh.items():
            pct = cnt / total_daily * 100 if total_daily else 0
            lines.append(f"| {label} | {cnt:,} | {pct:.1f}% |")
        lines.append("")

    # 按市场分布
    daily_ok = db_stats.get("daily_ok", 0)
    daily_stale = db_stats.get("daily_stale", 0)
    daily_none = db_stats.get("daily_none", 0)
    total = daily_ok + daily_stale + daily_none
    lines.append("### 日线覆盖分布")
    lines.append(f"- {SUCCESS} 数据新鲜: {daily_ok:,} 只 ({daily_ok/total*100:.1f}%)" if total > 0 else f"- {SUCCESS} 数据新鲜: {daily_ok:,} 只")
    lines.append(f"- {WARN} 数据陈旧: {daily_stale} 只")
    lines.append(f"- {FAIL} 无日线数据: {daily_none} 只")
    lines.append(f"- **总计**: {total:,} 只")
    lines.append("")

    # ── 完整性报告 ──
    lines.extend([
        f"---",
        f"## 🔍 完整性报告",
        f"",
        f"### 数据表覆盖",
        f"",
        f"| 表 | 行数 | 时间范围 | 状态 |",
        f"|-----|------|----------|------|",
    ])

    tbl_names = {
        "daily_quote": "日线行情", "income": "利润表", "balance_sheet": "资产负债表",
        "cashflow": "现金流量表", "financial_indicator": "财务指标",
        "stock_valuation": "估值数据", "index_daily": "指数日线",
    }
    for tbl, name in tbl_names.items():
        info = db_stats.get(tbl, {})
        rows = info.get("rows", 0)
        rng = info.get("range", "N/A")
        status_icon = SUCCESS if info.get("ok", True) else WARN
        lines.append(f"| {name} | {rows:,} | {rng} | {status_icon} |")

    # 财报覆盖
    lines.extend([
        f"",
        f"### 财报完整性 (A股)",
        f"",
        f"| 报表 | 覆盖标的 | 完整率 |",
        f"|------|----------|--------|",
    ])
    for tbl in ["income", "balance_sheet", "cashflow"]:
        info = db_stats.get(f"fin_{tbl}", {})
        covered = info.get("total", 0)
        ok = info.get("ok", 0)
        pct = ok / covered * 100 if covered else 0
        lines.append(f"| {tbl} | {covered:,} | {ok}/{covered} ({pct:.1f}%) |")

    # GitHub
    lines.extend([
        f"",
        f"---",
        f"## 🌐 GitHub",
        f"",
        f"- 仓库: `{REPO.name}`",
        f"- 推送状态: {SUCCESS if github_status else FAIL} {'成功' if github_status else '失败'}",
        f"- 数据目录: `daily/`, `fundamental/`, `meta/`, `macro/`, `index/`",
        f"",
        f"---",
        f"*报告由 pipeline.py 自动生成于 {now}*",
    ])

    content = "\n".join(lines)
    with open(report_path, "w") as f:
        f.write(content)

    log(f"报告已生成: {report_path}")
    return str(report_path)


def collect_db_stats() -> dict:
    """收集数据库统计信息"""
    stats = {}

    try:
        import psycopg2
        conn = psycopg2.connect(
            host=os.environ.get("PGHOST", "/tmp"),
            dbname=os.environ.get("PGDATABASE", "investassist"),
            user=os.environ.get("PGUSER", "james"),
            password=os.environ.get("PGPASSWORD", ""),
            connect_timeout=5,
        )
        conn.autocommit = True
        cur = conn.cursor()

        # 各表行数和时间范围
        tables = {
            "daily_quote": ("trade_date", "日线行情"),
            "income": ("report_year", "利润表"),
            "balance_sheet": ("report_year", "资产负债表"),
            "cashflow": ("report_year", "现金流量表"),
            "financial_indicator": ("report_year", "财务指标"),
            "stock_valuation": ("valuation_year", "估值数据"),
            "index_daily": ("trade_date", "指数日线"),
        }

        for tbl, (date_col, _) in tables.items():
            try:
                cur.execute(f"SELECT COUNT(*), MIN({date_col}), MAX({date_col}) FROM {tbl}")
                row = cur.fetchone()
                stats[tbl] = {
                    "rows": row[0] or 0,
                    "range": f"{row[1]} ~ {row[2]}" if row[1] and row[2] else "N/A",
                    "ok": True,
                }
            except Exception:
                stats[tbl] = {"rows": 0, "range": "N/A", "ok": False}

        # 日线覆盖统计
        cur.execute("""
            WITH stock_dq AS (
                SELECT ts_code, MAX(trade_date) as last_date
                FROM daily_quote GROUP BY ts_code
            )
            SELECT
                SUM(CASE WHEN last_date >= CURRENT_DATE - INTERVAL '3 days' THEN 1 ELSE 0 END) as ok,
                SUM(CASE WHEN last_date < CURRENT_DATE - INTERVAL '3 days'
                          AND last_date >= '2026-06-01' THEN 1 ELSE 0 END) as stale,
                (SELECT COUNT(*) FROM stocks WHERE delist_date IS NULL
                 AND ts_code NOT IN (SELECT DISTINCT ts_code FROM daily_quote)) as none_data
            FROM stock_dq
        """)
        row = cur.fetchone()
        stats["daily_ok"] = row[0] or 0
        stats["daily_stale"] = row[1] or 0
        stats["daily_none"] = row[2] or 0

        # 日线新鲜度分布
        cur.execute("""
            WITH last_dates AS (
                SELECT ts_code, MAX(trade_date) as last_date FROM daily_quote GROUP BY ts_code
            )
            SELECT
                SUM(CASE WHEN last_date >= CURRENT_DATE - INTERVAL '3 days' THEN 1 ELSE 0 END) as fresh,
                SUM(CASE WHEN last_date >= CURRENT_DATE - INTERVAL '14 days'
                          AND last_date < CURRENT_DATE - INTERVAL '3 days' THEN 1 ELSE 0 END) as week_old,
                SUM(CASE WHEN last_date >= '2026-06-01'
                          AND last_date < CURRENT_DATE - INTERVAL '14 days' THEN 1 ELSE 0 END) as month_old,
                SUM(CASE WHEN last_date < '2026-06-01' THEN 1 ELSE 0 END) as older
            FROM last_dates
        """)
        row = cur.fetchone()
        stats["daily_fresh"] = {
            "0-2天前": row[0] or 0,
            "3-14天前": row[1] or 0,
            "15天以上(本月)": row[2] or 0,
            "更早": row[3] or 0,
        }

        # 财报覆盖
        for tbl in ["income", "balance_sheet", "cashflow"]:
            try:
                cur.execute(f"SELECT COUNT(DISTINCT ts_code) FROM {tbl}")
                covered = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM stocks WHERE exchange IN ('SSE','SZSE','BSE')")
                total = cur.fetchone()[0]
                cur.execute(f"""
                    WITH y AS (SELECT ts_code, COUNT(*) as c FROM {tbl} WHERE report_year >= 2020 GROUP BY ts_code, report_year)
                    SELECT COUNT(DISTINCT ts_code) FROM y WHERE c < 3
                """)
                incomplete = cur.fetchone()[0]
                stats[f"fin_{tbl}"] = {"ok": covered - incomplete, "total": covered, "incomplete": incomplete}
            except Exception:
                stats[f"fin_{tbl}"] = {"ok": 0, "total": 0, "incomplete": 0}

        cur.close()
        conn.close()
    except Exception as e:
        log(f"收集 DB 统计失败: {e}", "WARN")
        stats["_error"] = str(e)

    return stats


# ═══════════════════════════════════════════════════════════════════════════
# 飞书推送
# ═══════════════════════════════════════════════════════════════════════════

def _get_feishu_credentials() -> tuple[str, str] | None:
    """从 WorkBuddy settings.json 读取飞书凭证"""
    try:
        if not SETTINGS_PATH.exists():
            return None
        with open(SETTINGS_PATH) as f:
            settings = json.load(f)
        feishu = settings.get("connectorConfigs", {}).get("feishu", {})
        app_id = feishu.get("appId", "")
        app_secret = feishu.get("appSecret", "")
        if app_id and app_secret:
            return app_id, app_secret
    except Exception:
        pass
    return None


def _get_feishu_token(app_id: str, app_secret: str) -> str | None:
    """获取 tenant_access_token"""
    try:
        r = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=10,
        )
        data = r.json()
        return data.get("tenant_access_token")
    except Exception as e:
        log(f"飞书 token 获取失败: {e}", "ERROR")
        return None


def push_feishu_report(report_text: str, chat_id: str | None = None) -> bool:
    """推送报告到飞书。自动发现机器人所在群聊，无需手动配置。"""
    creds = _get_feishu_credentials()
    if not creds:
        log("未找到飞书凭证，跳过推送", "WARN")
        return False

    app_id, app_secret = creds
    token = _get_feishu_token(app_id, app_secret)
    if not token:
        return False

    # 1. 优先使用指定的 chat_id
    if not chat_id:
        chat_id = os.environ.get("FEISHU_CHAT_ID", "")

    # 2. 自动发现机器人所在的群聊
    if not chat_id:
        chat_id = _discover_chat(token)
        if chat_id:
            log(f"自动发现群聊: {chat_id}")

    # 3. 兜底 webhook
    if not chat_id:
        webhook = os.environ.get("FEISHU_WEBHOOK", "")
        if webhook:
            return _send_via_webhook(webhook, report_text)
        log("未找到群聊，请将机器人添加到飞书群。", "WARN")
        return False

    return _send_via_api(token, chat_id, report_text)


def _discover_chat(token: str) -> str | None:
    """自动发现机器人所在的群聊，返回第一个可用群聊 ID"""
    try:
        r = requests.get(
            "https://open.feishu.cn/open-apis/im/v1/chats?page_size=10",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        data = r.json()
        if data.get("code") == 0:
            for chat in data.get("data", {}).get("items", []):
                if chat.get("chat_status") == "normal":
                    return chat.get("chat_id")
    except Exception:
        pass
    return None


def _send_via_api(token: str, chat_id: str, report_text: str) -> bool:
    """通过飞书 API 发送消息卡片"""
    try:
        r = requests.post(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "receive_id": chat_id,
                "msg_type": "interactive",
                "content": json.dumps({
                    "header": {
                        "title": {"tag": "plain_text", "content": "📊 stock_data 数据报告"},
                        "template": "blue",
                    },
                    "elements": [
                        {"tag": "markdown", "content": report_text},
                        {"tag": "hr"},
                        {"tag": "note", "elements": [{"tag": "plain_text", "content": f"自动生成 · {datetime.now().strftime('%Y-%m-%d %H:%M')}"}]},
                    ],
                }),
            },
            timeout=10,
        )
        result = r.json()
        ok = result.get("code") == 0
        log(f"飞书推送: {'✅' if ok else '❌'} {result.get('msg', '')}")
        return ok
    except Exception as e:
        log(f"飞书推送失败: {e}", "ERROR")
        return False


def _send_via_webhook(webhook: str, report_text: str) -> bool:
    """通过 webhook 发送消息"""
    try:
        r = requests.post(webhook, json={
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": "📊 stock_data 数据报告"}, "template": "blue"},
                "elements": [{"tag": "markdown", "content": report_text}],
            },
        }, timeout=10)
        ok = r.json().get("code") == 0
        log(f"飞书推送: {'✅' if ok else '❌'} (webhook)")
        return ok
    except Exception as e:
        log(f"飞书 webhook 推送失败: {e}", "ERROR")
        return False


def build_feishu_report(db_stats: dict, increments: dict, github_ok: bool) -> str:
    """构建飞书消息用的 Markdown 报告文本"""
    today_str = date.today().strftime("%Y-%m-%d")
    daily_ok = db_stats.get("daily_ok", 0)
    daily_stale = db_stats.get("daily_stale", 0)
    daily_none = db_stats.get("daily_none", 0)

    lines = [
        f"**日期**: {today_str}",
        f"**GitHub**: {'✅ 已备份' if github_ok else '❌ 失败'}",
        f"",
    ]

    if increments:
        lines.append("**📈 本次增量**")
        for tbl, inc in increments.items():
            names = {"daily_quote": "日线", "income": "利润表", "balance_sheet": "资产负债表", "cashflow": "现金流"}
            lines.append(f"- {names.get(tbl, tbl)}: +{inc:,} 行")
        lines.append("")

    lines.extend([
        "**📊 数据分布**",
        f"- ✅ 新鲜: {daily_ok:,} 只",
        f"- ⚠️ 陈旧: {daily_stale} 只",
        f"- ❌ 无数据: {daily_none} 只",
        f"",
        "**🔍 数据表覆盖**",
    ])

    tbl_names = {
        "daily_quote": "日线", "income": "利润表", "balance_sheet": "资产负债表",
        "cashflow": "现金流", "financial_indicator": "财务指标", "stock_valuation": "估值",
    }
    for tbl, name in tbl_names.items():
        info = db_stats.get(tbl, {})
        if info:
            icon = SUCCESS if info.get("ok", True) else WARN
            rng = info.get("range", "N/A")
            lines.append(f"- {icon} {name}: {info.get('rows', 0):,} 行 ({rng})")

    return "\n".join(lines)

def main():
    parser = argparse.ArgumentParser(description="stock_data 数据管道编排器")
    parser.add_argument("--dry-run", action="store_true", help="预览模式")
    parser.add_argument("--cron", action="store_true", help="定时任务模式（静默）")
    parser.add_argument("--force", action="store_true", help="非交易日也执行")
    parser.add_argument("--max-rounds", type=int, default=3, help="最大补全-校验轮次 (默认3)")
    parser.add_argument("--skip-backup", action="store_true", help="跳过 GitHub 备份")
    parser.add_argument("--feishu-chat", help="飞书群聊 chat_id（优先于环境变量 FEISHU_CHAT_ID）")
    parser.add_argument("--feishu-webhook", help="飞书 webhook URL（兜底方案）")
    args = parser.parse_args()

    # 飞书 webhook 环境变量
    if args.feishu_webhook:
        os.environ["FEISHU_WEBHOOK"] = args.feishu_webhook
    if args.feishu_chat:
        os.environ["FEISHU_CHAT_ID"] = args.feishu_chat

    today = date.today()
    missing_path = str(REPO / "logs" / f"missing_{today.strftime('%Y%m%d')}.json")

    log("=" * 60)
    log(f"stock_data Pipeline 启动")
    log(f"日期: {today} | {'交易日' if is_trading_day(today) else '非交易日'} | "
        f"模式: {'DRY-RUN' if args.dry_run else 'CRON' if args.cron else 'NORMAL'} | "
        f"最大轮次: {args.max_rounds}")

    # ── 非交易日处理 ──
    if not is_trading_day(today) and not args.force:
        log("今日非交易日，跳过拉取。仅执行校验和备份。", "INFO", args.cron)
        report = step_validate_full(missing_path, args.cron)
        if report:
            db_stats = collect_db_stats()
            rp = generate_report(db_stats, False, None, args.cron)
            log(f"非交易日报告: {rp}")
        return 0

    if args.dry_run:
        log("=== [DRY RUN] 预览模式 ===", "INFO")
        report = step_validate_full(missing_path, args.cron)
        if report:
            summary = report.get("summary", {})
            log(f"  日线缺失: {summary.get('stale_daily', 0)} 只")
            log(f"  无日线: {summary.get('no_data_stocks', 0)} 只")
            log(f"  表级落后: {summary.get('table_gaps', 0)} 个")
            log(f"  财报问题: {summary.get('financial_issues', {})}")
        log("[dry-run] 流水线预览完成，未执行实际操作")
        return 0

    # ── 主流程 ──
    start_time = time.time()

    # 记录执行前状态（用于计算增量）
    before_stats = collect_db_stats()

    # Step 1: 全量校验
    report = step_validate_full(missing_path, args.cron)
    if not report:
        log("初始校验失败，中止", "ERROR", args.cron)
        return 1

    stale_count = report.get("summary", {}).get("stale_daily", 0)
    log(f"初始校验: {stale_count} 只日线缺失")

    # Step 2-4: 补全循环（最多 N 轮）
    round_num = 0
    final_pass = False
    final_errors = 0
    final_warnings = 0

    while round_num < args.max_rounds:
        round_num += 1
        log(f"--- 第 {round_num}/{args.max_rounds} 轮 ---")

        if stale_count > 0:
            step_fill_gaps(missing_path, args.cron)

        step_fetch_latest(args.cron)

        # 重新校验
        updated_report = step_validate_full(missing_path, args.cron)
        if updated_report:
            stale_count = updated_report.get("summary", {}).get("stale_daily", 0)
            table_gaps = updated_report.get("summary", {}).get("table_gaps", 0)
            log(f"  第 {round_num} 轮校验: 日线缺失 {stale_count} 只, 表落后 {table_gaps} 个")

        # 快速校验
        passed, final_errors, final_warnings = step_validate_quick(args.cron)
        if passed and stale_count == 0:
            log(f"✅ 第 {round_num} 轮校验通过！")
            final_pass = True
            break
        else:
            log(f"⚠️ 第 {round_num} 轮未通过 (err={final_errors}, warn={final_warnings}), "
                f"{'继续重试' if round_num < args.max_rounds else '达到上限'}", "WARN", args.cron)

    # ── Step 5: 备份 ──
    if args.skip_backup:
        log("跳过 GitHub 备份 (--skip-backup)")
        github_ok = False
    else:
        github_ok = step_backup(args.cron)

    # ── 最终报告 ──
    db_stats = collect_db_stats()
    report_path = generate_report(db_stats, github_ok, before_stats, args.cron)

    # ── 飞书推送 ──
    increments = {}
    for tbl in ["daily_quote", "income", "balance_sheet", "cashflow"]:
        before = before_stats.get(tbl, {}).get("rows", 0)
        after = db_stats.get(tbl, {}).get("rows", 0)
        if before > 0:
            increments[tbl] = after - before

    feishu_ok = push_feishu_report(
        build_feishu_report(db_stats, increments, github_ok),
        chat_id=os.environ.get("FEISHU_CHAT_ID"),
    )

    # ── 摘要 ──
    elapsed = time.time() - start_time
    status = SUCCESS if final_pass or stale_count == 0 else WARN
    log(f"\n{'='*60}")
    log(f"{status} Pipeline 完成 | 耗时 {elapsed:.0f}s | {args.max_rounds} 轮")
    log(f"  日线缺失: {stale_count} 只")
    log(f"  增量: {increments}")
    log(f"  GitHub: {SUCCESS if github_ok else FAIL}")
    log(f"  飞书: {SUCCESS if feishu_ok else FAIL}")
    log(f"  报告: {report_path}")
    log(f"{'='*60}")

    return 0 if final_pass else 1


if __name__ == "__main__":
    sys.exit(main())

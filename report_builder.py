#!/usr/bin/env python3
"""报告生成器 — collect_db_stats + generate_report + build_feishu_report

被 pipeline.py 调用，也可独立测试。
"""
from __future__ import annotations

import json, os, requests
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).parent
SETTINGS_PATH = Path.home() / ".workbuddy" / "settings.json"
REPORT_DIR = REPO / "reports"
SUCCESS = "✅"
FAIL = "❌"
WARN = "⚠️"


def get_db_conn():
    """获取 PostgreSQL 连接"""
    import psycopg2
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "/tmp"),
        dbname=os.environ.get("PGDATABASE", "investassist"),
        user=os.environ.get("PGUSER", "james"),
        password=os.environ.get("PGPASSWORD", ""),
        connect_timeout=5,
    )


def collect_db_stats() -> dict:
    """收集数据库统计信息 + 缺失明细"""
    stats = {}
    conn = get_db_conn()
    conn.autocommit = True
    cur = conn.cursor()

    today = date.today()

    # 各表行数和时间范围
    tables = [
        ("daily_quote", "trade_date", "日线行情"),
        ("income", "report_year", "利润表"),
        ("balance_sheet", "report_year", "资产负债表"),
        ("cashflow", "report_year", "现金流量表"),
        ("financial_indicator", "report_year", "财务指标"),
        ("stock_valuation", "valuation_year", "估值数据"),
        ("index_daily", "trade_date", "指数日线"),
    ]
    for tbl, date_col, name in tables:
        try:
            cur.execute(f"SELECT COUNT(*), MIN({date_col}), MAX({date_col}) FROM {tbl}")
            row = cur.fetchone()
            stats[tbl] = {"name": name, "rows": row[0] or 0,
                           "range": f"{row[1]} ~ {row[2]}" if row[1] and row[2] else "N/A",
                           "ok": True}
        except Exception:
            stats[tbl] = {"name": name, "rows": 0, "range": "N/A", "ok": False}

    # ── 日线覆盖 ──
    cur.execute("""
        WITH active_a AS (
            SELECT ts_code, name FROM stocks
            WHERE delist_date IS NULL AND exchange IN ('SSE','SZSE','BSE')
        ),
        stock_dq AS (
            SELECT ts_code, MAX(trade_date) as last_date
            FROM daily_quote GROUP BY ts_code
        ),
        joined AS (
            SELECT a.ts_code, a.name, COALESCE(sd.last_date, '1900-01-01'::date) as last_date
            FROM active_a a LEFT JOIN stock_dq sd ON a.ts_code = sd.ts_code
        )
        SELECT
            SUM(CASE WHEN last_date >= CURRENT_DATE - INTERVAL '3 days' THEN 1 ELSE 0 END) as ok,
            SUM(CASE WHEN last_date < CURRENT_DATE - INTERVAL '3 days'
                      AND last_date >= CURRENT_DATE - INTERVAL '14 days' THEN 1 ELSE 0 END) as to_fix,
            SUM(CASE WHEN last_date < CURRENT_DATE - INTERVAL '14 days'
                      AND last_date >= '2026-04-01' THEN 1 ELSE 0 END) as suspended,
            SUM(CASE WHEN last_date < '2026-04-01' THEN 1 ELSE 0 END) as inactive,
            COUNT(*) as total,
            ARRAY_AGG(ts_code || '|' || name || '|' || last_date::text ORDER BY last_date)
                FILTER (WHERE last_date < CURRENT_DATE - INTERVAL '3 days') as stale_list
        FROM joined
    """)
    row = cur.fetchone()
    stats["daily_total"] = row[4] or 0
    stats["daily_ok"] = row[0] or 0
    stats["daily_to_fix"] = row[1] or 0      # 可修复的（近期缺几天）
    stats["daily_suspended"] = row[2] or 0    # 停牌/ST（正常）
    stats["daily_inactive"] = row[3] or 0     # 疑似退市（正常）
    stats["daily_stale_details"] = [
        {"ts_code": x.split("|")[0], "name": x.split("|")[1], "last_date": x.split("|")[2]}
        for x in (row[5] or [])
    ]

    # ── 财报明细：查询 2020+ 每只股票的缺失情况 ──
    for tbl in ["income", "balance_sheet", "cashflow"]:
        try:
            stats[f"fin_{tbl}"] = _check_financial(cur, tbl, today.year)
        except Exception:
            stats[f"fin_{tbl}"] = {"total_stocks": 0, "fully_ok": 0, "pct": 0, "missing_details": []}

    cur.close()
    conn.close()
    return stats


def _check_financial(cur, tbl: str, current_year: int) -> dict:
    """检查某个财报表 2020+ 的完整性"""
    cur.execute(f"""
        WITH stock_report AS (
            SELECT i.ts_code, i.report_year, COUNT(*) as cnt,
                EXTRACT(YEAR FROM s.list_date)::int as list_year,
                EXTRACT(MONTH FROM s.list_date)::int as list_month,
                s.name
            FROM {tbl} i
            JOIN stocks s ON i.ts_code = s.ts_code
            WHERE i.report_year >= 2020 AND i.report_year < {current_year}
              AND s.delist_date IS NULL
              AND s.exchange IN ('SSE','SZSE','BSE')
            GROUP BY i.ts_code, i.report_year, s.list_date, s.name
        ),
        stock_years AS (
            SELECT ts_code, name, report_year, cnt, list_year, list_month,
                CASE WHEN report_year = list_year THEN
                    CASE WHEN list_month <= 3 THEN 4
                         WHEN list_month <= 6 THEN 3
                         WHEN list_month <= 9 THEN 2 ELSE 1 END
                ELSE 4 END as expected
            FROM stock_report
        )
        SELECT
            COUNT(*) as total_years,
            SUM(CASE WHEN cnt >= expected THEN 1 ELSE 0 END) as ok_years
        FROM stock_years
    """)
    row = cur.fetchone()
    total_years = row[0] or 0
    ok_years = row[1] or 0
    stock_year_pct = round(ok_years / total_years * 100, 1) if total_years else 0

    # 严重缺失明细（≥2年不完整）
    cur.execute(f"""
        WITH stock_report AS (
            SELECT i.ts_code, i.report_year, COUNT(*) as cnt,
                EXTRACT(YEAR FROM s.list_date)::int as list_year,
                EXTRACT(MONTH FROM s.list_date)::int as list_month, s.name
            FROM {tbl} i
            JOIN stocks s ON i.ts_code = s.ts_code
            WHERE i.report_year >= 2020 AND i.report_year < {current_year}
              AND s.delist_date IS NULL AND s.exchange IN ('SSE','SZSE','BSE')
            GROUP BY i.ts_code, i.report_year, s.list_date, s.name
        ),
        stock_years AS (
            SELECT ts_code, name, report_year, cnt,
                CASE WHEN report_year = list_year THEN
                    CASE WHEN list_month <= 3 THEN 4
                         WHEN list_month <= 6 THEN 3
                         WHEN list_month <= 9 THEN 2 ELSE 1 END
                ELSE 4 END as expected
            FROM stock_report
        ),
        per_stock AS (
            SELECT ts_code, name,
                COUNT(*) as total, SUM(CASE WHEN cnt >= expected THEN 1 ELSE 0 END) as ok,
                ARRAY_AGG(report_year::text || '(' || cnt::text || '/' || expected::text || ')'
                          ORDER BY report_year) FILTER (WHERE cnt < expected) as bad
            FROM stock_years GROUP BY ts_code, name
        )
        SELECT
            ARRAY_AGG(ts_code || '|' || name || '|' || ARRAY_TO_STRING(bad, ',')
                      ORDER BY total - ok DESC)
                FILTER (WHERE total - ok >= 2) as severe,
            COUNT(*) FILTER (WHERE total - ok >= 2) as severe_cnt,
            COUNT(*) FILTER (WHERE total - ok = 1) as minor_cnt,
            COUNT(*) FILTER (WHERE total = ok) as full_cnt,
            COUNT(*) as total_stocks
        FROM per_stock
    """)
    row = cur.fetchone()
    severe = []
    if row[0]:
        for x in row[0]:
            p = x.split("|", 2)
            severe.append({"ts_code": p[0], "name": p[1], "gap_years": p[2] if len(p) > 2 else ""})

    return {
        "total_stocks": row[4] or 0,
        "fully_ok": row[3] or 0,
        "stock_year_pct": stock_year_pct,    # 股票-年完整率
        "stock_pct": round((row[3] or 0) / (row[4] or 1) * 100, 1),  # 标的完整率
        "severe_cnt": row[1] or 0,
        "minor_cnt": row[2] or 0,
        "severe_gaps": severe,
    }


# ═══════════════════════════════════════════════════════════════════════════
# 报告生成
# ═══════════════════════════════════════════════════════════════════════════

def generate_report(stats: dict, github_ok: bool) -> str:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    today_str = date.today().strftime("%Y%m%d")
    report_path = REPORT_DIR / f"pipeline_report_{today_str}.md"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total = stats.get("daily_total", 0)
    ok = stats.get("daily_ok", 0)

    lines = [
        f"# 📊 stock_data 数据管道报告",
        f"**日期**: {date.today()} | **执行**: {now} | **GitHub**: {SUCCESS if github_ok else FAIL}",
        f"",
        f"---",
        f"## 📈 日线数据 ({total:,} 只 A 股活跃标的)",
        f"",
        f"| 状态 | 数量 | 占比 |",
        f"|------|------|------|",
        f"| {SUCCESS} 新鲜 (0-2天) | {ok:,} | {ok/total*100:.1f}% |" if total else "",
    ]

    to_fix = stats.get("daily_to_fix", 0)
    suspended = stats.get("daily_suspended", 0)
    inactive = stats.get("daily_inactive", 0)
    active_base = total - suspended - inactive  # 排除停牌和退市后的底数
    daily_pct = ok / active_base * 100 if active_base else 0

    lines = [
        f"# 📊 stock_data 数据管道报告",
        f"**日期**: {date.today()} | **执行**: {now} | **GitHub**: {SUCCESS if github_ok else FAIL}",
        f"",
        f"---",
        f"## 📈 日线数据",
        f"",
        f"**{SUCCESS} 完整性: {daily_pct:.1f}%** （{ok:,}/{active_base:,} 只活跃A股在0-2天内更新）",
        f"",
        f"| 状态 | 数量 |",
        f"|------|------|",
        f"| {SUCCESS} 新鲜 (0-2天) | {ok:,} |",
    ]

    if to_fix:
        lines.append(f"| {WARN} 近期缺口 (可修复) | {to_fix} |")
    if suspended:
        lines.append(f"| ➖ 停牌/ST (正常，不计入缺失) | {suspended} |")
    if inactive:
        lines.append(f"| ➖ 疑似退市 (正常，不计入缺失) | {inactive} |")
    lines.append(f"| **总计** | **{total:,}** |")
    lines.append("")

    # 缺失明细
    stale_details = stats.get("daily_stale_details", [])
    if stale_details:
        real_gaps = [d for d in stale_details if date.fromisoformat(d["last_date"]) >= date(2026, 6, 1)]
        if real_gaps:
            lines.append("### 近期缺口明细（可修复）")
            lines.append("| 代码 | 名称 | 最后日期 |")
            lines.append("|------|------|----------|")
            for d in real_gaps[:20]:
                lines.append(f"| {d['ts_code']} | {d['name']} | {d['last_date']} |")
            if len(real_gaps) > 20:
                lines.append(f"| ... | ... | 共 {len(real_gaps)} 只 |")
            lines.append("")

    # ── 数据表覆盖 ──
    lines.extend([
        f"---",
        f"## 📊 数据表覆盖",
        f"| 表 | 行数 | 时间范围 |",
        f"|-----|------|----------|",
    ])
    table_order = ["daily_quote", "income", "balance_sheet", "cashflow",
                    "financial_indicator", "stock_valuation", "index_daily"]
    for tbl in table_order:
        info = stats.get(tbl, {})
        lines.append(f"| {info.get('name', tbl)} | {info.get('rows', 0):,} | {info.get('range', 'N/A')} |")

    # ── 财报完整性 ──
    lines.extend([
        f"",
        f"---",
        f"## 🔍 财报完整性 (2020-2025, A股活跃标的)",
        f"",
        f"| 报表 | 条数完整率 | 标的完整率 | 严重缺失(≥2年) | 轻度(1年) |",
        f"|------|-----------|-----------|-----------------|-----------|",
    ])
    names = {"income": "利润表", "balance_sheet": "资产负债表", "cashflow": "现金流量表"}
    for tbl in ["income", "balance_sheet", "cashflow"]:
        info = stats.get(f"fin_{tbl}", {})
        lines.append(
            f"| {names[tbl]} | {info.get('stock_year_pct', 0)}% "
            f"| {info.get('stock_pct', 0)}% "
            f"| {info.get('severe_cnt', 0)} "
            f"| {info.get('minor_cnt', 0)} |"
        )

    # 严重缺失明细
    for tbl in ["income", "balance_sheet", "cashflow"]:
        info = stats.get(f"fin_{tbl}", {})
        severe = info.get("severe_gaps", [])
        if severe:
            lines.append(f"")
            lines.append(f"### {names[tbl]} — 严重缺失 (≥2年)")
            lines.append(f"| 代码 | 名称 | 缺失年份(实际/预期) |")
            lines.append(f"|------|------|----------------------|")
            for g in severe[:10]:
                lines.append(f"| {g['ts_code']} | {g['name']} | {g['gap_years']} |")
            if len(severe) > 10:
                lines.append(f"| ... | ... | 共 {len(severe)} 只 |")

    # ── GitHub ──
    lines.extend([
        f"",
        f"---",
        f"## 🌐 GitHub: `{REPO.name}` — {SUCCESS if github_ok else FAIL}",
        f"*报告自动生成于 {now}*",
    ])

    content = "\n".join(lines)
    with open(report_path, "w") as f:
        f.write(content)
    return str(report_path)


# ═══════════════════════════════════════════════════════════════════════════
# 飞书推送
# ═══════════════════════════════════════════════════════════════════════════

def build_feishu_report(stats: dict, github_ok: bool) -> str:
    """构建飞书消息 Markdown"""
    total = stats.get("daily_total", 0)
    ok = stats.get("daily_ok", 0)
    suspended = stats.get("daily_suspended", 0)
    inactive = stats.get("daily_inactive", 0)
    active_base = total - suspended - inactive
    daily_pct = ok / active_base * 100 if active_base else 0
    
    lines = [
        f"**📊 stock_data 数据报告** — {date.today()}",
        f"",
        f"**日线**: {SUCCESS} {daily_pct:.1f}% ({ok}/{active_base} 活跃A股)",
    ]
    to_fix = stats.get("daily_to_fix", 0)
    if to_fix:
        lines.append(f"可修复缺口: {to_fix} 只")
    if suspended:
        lines.append(f"停牌/ST: {suspended} 只（不计入缺失）")

    # 缺失明细
    stale_details = stats.get("daily_stale_details", [])
    real_gaps = [d for d in stale_details if date.fromisoformat(d["last_date"]) >= date(2026, 6, 1)]
    if real_gaps:
        lines.append("**近期缺口**:")
        for d in real_gaps[:5]:
            lines.append(f"- {d['ts_code']} {d['name']} ({d['last_date']})")
        lines.append("")

    lines.append("**财报 (2020+, 条数完整率)**:")
    for tbl in ["income", "balance_sheet", "cashflow"]:
        info = stats.get(f"fin_{tbl}", {})
        names = {"income": "利润表", "balance_sheet": "资产负债表", "cashflow": "现金流"}
        severe = info.get("severe_cnt", 0)
        lines.append(f"- {names[tbl]}: **{info.get('stock_year_pct', 0)}%**" + (f" (严重缺失 {severe} 只)" if severe else " ✅"))

    if github_ok:
        lines.append(f"\n{SUCCESS} GitHub 备份成功")
    return "\n".join(lines)


def push_feishu_report(text: str) -> bool:
    """推送飞书，优先私聊"""
    creds = _get_feishu_credentials()
    if not creds:
        return False

    app_id, app_secret = creds
    token = _get_feishu_token(app_id, app_secret)
    if not token:
        return False

    # 尝试私聊
    open_id = _discover_user(token)
    if open_id:
        return _send_message(token, "open_id", open_id, text)

    return False


def _get_feishu_credentials():
    try:
        if not SETTINGS_PATH.exists():
            return None
        with open(SETTINGS_PATH) as f:
            s = json.load(f)
        for uid, u in s.get("claw", {}).get("users", {}).items():
            feishu = u.get("channels", {}).get("feishu", {})
            app_id = feishu.get("appId", "")
            app_secret = feishu.get("appSecret", "")
            if app_id and app_secret:
                return app_id, app_secret
    except Exception:
        pass
    return None


def _get_feishu_token(app_id, app_secret):
    try:
        r = requests.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                          json={"app_id": app_id, "app_secret": app_secret}, timeout=10)
        return r.json().get("tenant_access_token")
    except Exception:
        return None


def _discover_user(token):
    try:
        r = requests.get("https://open.feishu.cn/open-apis/contact/v3/users?page_size=5",
                         headers={"Authorization": f"Bearer {token}"}, timeout=10)
        for u in r.json().get("data", {}).get("items", []):
            return u.get("open_id")
    except Exception:
        pass
    return None


def _send_message(token, receive_type, receive_id, text):
    try:
        r = requests.post(
            f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={receive_type}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"receive_id": receive_id, "msg_type": "interactive",
                  "content": json.dumps({
                      "header": {"title": {"tag": "plain_text", "content": "📊 stock_data 数据报告"}, "template": "blue"},
                      "elements": [
                          {"tag": "markdown", "content": text},
                          {"tag": "hr"},
                          {"tag": "note", "elements": [{"tag": "plain_text", "content": f"自动生成 · {datetime.now().strftime('%Y-%m-%d %H:%M')}"}]},
                      ],
                  })},
            timeout=10)
        return r.json().get("code") == 0
    except Exception:
        return False

#!/usr/bin/env python3
"""报告生成器 — collect_db_stats + generate_report + build_feishu_report

被 pipeline.py 调用，也可独立测试。
"""
from __future__ import annotations

import json, os, requests
from datetime import date, datetime, timedelta
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
            # 港股拆分: daily_quote + 财报按 ts_code LIKE '%.HK' 统计
            if tbl == "daily_quote":
                cur.execute(f"SELECT COUNT(*) FROM {tbl} WHERE ts_code LIKE '%%.HK'")
                stats[f"{tbl}_hk"] = {"name": "  ├─ 港股", "rows": cur.fetchone()[0] or 0, "range": "", "ok": True}
            elif tbl in ("income", "balance_sheet", "cashflow", "financial_indicator"):
                cur.execute(f"SELECT COUNT(*) FROM {tbl} WHERE ts_code LIKE '%%.HK'")
                stats[f"{tbl}_hk"] = {"name": "  ├─ 港股", "rows": cur.fetchone()[0] or 0, "range": "", "ok": True}
            # 指数日线拆分港股指数
            if tbl == "index_daily":
                cur.execute("SELECT COUNT(*) FROM index_daily WHERE symbol IN ('HSI','HSTECH','HSCEI')")
                stats[f"{tbl}_hk"] = {"name": "  ├─ HK指数", "rows": cur.fetchone()[0] or 0, "range": "", "ok": True}
        except Exception:
            stats[tbl] = {"name": name, "rows": 0, "range": "N/A", "ok": False}

    # ── 日线覆盖 (A股) ──
    _collect_daily_coverage(cur, stats, "SSE,SZSE,BSE", "daily", prefix="")
    # ── 日线覆盖 (港股) ──
    _collect_daily_coverage(cur, stats, "HKEX", "daily_hk", prefix="hk_")

    # ── 财报明细 (A股) ──
    for tbl in ["income", "balance_sheet", "cashflow"]:
        try:
            stats[f"fin_{tbl}"] = _check_financial(cur, tbl, today.year, exchange="SSE,SZSE,BSE")
        except Exception:
            stats[f"fin_{tbl}"] = {"total_stocks": 0, "fully_ok": 0, "pct": 0, "missing_details": []}
    # ── 财报明细 (港股) ──
    for tbl in ["income", "balance_sheet", "cashflow"]:
        try:
            stats[f"fin_hk_{tbl}"] = _check_financial(cur, tbl, today.year, exchange="HKEX")
        except Exception:
            stats[f"fin_hk_{tbl}"] = {"total_stocks": 0, "fully_ok": 0, "pct": 0, "missing_details": []}

    # ── 港股季度数据覆盖 ──
    for tbl in ["income", "balance_sheet", "cashflow"]:
        try:
            stats[f"fin_hk_q_{tbl}"] = _check_quarterly_hk(cur, tbl)
        except Exception:
            stats[f"fin_hk_q_{tbl}"] = {"annual_pct": 0, "semi_pct": 0, "q1_pct": 0, "q3_pct": 0, "total_stocks": 0}

    # ── 数据域新鲜度 (M5) ──
    _collect_freshness(stats)

    cur.close()
    conn.close()
    return stats


def _collect_freshness(stats: dict):
    """各数据域新鲜度总览（M5）：MAX(date) vs 最近 A 股交易日，落后 N 交易日。"""
    A_IDX = ("000001.SH", "000016.SH", "000300.SH", "000688.SH", "000852.SH",
             "000905.SH", "399001.SZ", "399005.SZ", "399006.SZ")
    a_in = ",".join(f"'{s}'" for s in A_IDX)
    try:
        conn = get_db_conn()
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT MAX(cal_date) FROM trade_cal WHERE is_open::int=1 AND cal_date <= CURRENT_DATE")
        row = cur.fetchone()
        last_td = row[0] if row else None
        if not last_td:
            return

        domains = [
            ("A股日线", f"SELECT MAX(trade_date), COUNT(DISTINCT ts_code) FROM daily_quote WHERE ts_code NOT LIKE '%%.HK'", 0),
            ("港股日线", "SELECT MAX(trade_date), COUNT(DISTINCT ts_code) FROM daily_quote WHERE ts_code LIKE '%%.HK'", 2),
            ("ETF日线", "SELECT MAX(trade_date), COUNT(DISTINCT code) FROM etf_quote", 0),
            ("A股指数", f"SELECT MAX(trade_date), COUNT(DISTINCT symbol) FROM index_daily WHERE symbol IN ({a_in})", 0),
            ("海外/港股指数", f"SELECT MAX(trade_date), COUNT(DISTINCT symbol) FROM index_daily WHERE symbol NOT IN ({a_in})", 1),
        ]
        freshness = []
        for name, sql, tol in domains:
            try:
                cur.execute(sql)
                m, n = cur.fetchone()
                if m is None:
                    freshness.append({"domain": name, "max_date": "无数据", "symbols": n or 0, "lag": None})
                    continue
                cur.execute("SELECT COUNT(*) FROM trade_cal WHERE is_open::int=1 AND cal_date > %s AND cal_date <= %s",
                            (m, last_td))
                lag = cur.fetchone()[0]
                freshness.append({"domain": name, "max_date": str(m), "symbols": n or 0,
                                  "lag": lag, "ok": lag <= tol})
            except Exception:
                freshness.append({"domain": name, "max_date": "?", "symbols": 0, "lag": None})

        # 财报域：最新报告年度
        expected_year = date.today().year if date.today().month >= 5 else date.today().year - 1
        for tbl, name in [("income", "利润表"), ("balance_sheet", "资产负债表"),
                          ("cashflow", "现金流量表"), ("financial_indicator", "财务指标")]:
            try:
                cur.execute(f"SELECT MAX(report_year), MAX(ann_date), COUNT(DISTINCT ts_code) FROM {tbl}")
                ry, ad, n = cur.fetchone()
                freshness.append({"domain": f"财报·{name}", "max_date": f"{ry} (ann {ad})",
                                  "symbols": n or 0,
                                  "lag": 0 if (ry and str(ry).isdigit() and int(ry) >= expected_year) else None,
                                  "ok": bool(ry and str(ry).isdigit() and int(ry) >= expected_year)})
            except Exception:
                pass

        stats["freshness"] = freshness
        stats["freshness_base"] = str(last_td)
        cur.close()
        conn.close()
    except Exception:
        pass


def _collect_daily_coverage(cur, stats: dict, exchange: str, stat_key: str, prefix: str):
    """收集日线覆盖统计数据 (A股/港股通用)"""
    exchanges = "','".join(exchange.split(","))
    cur.execute(f"""
        WITH active AS (
            SELECT ts_code, name FROM stocks
            WHERE delist_date IS NULL AND exchange IN ('{exchanges}')
        ),
        stock_dq AS (
            SELECT ts_code, MAX(trade_date) as last_date
            FROM daily_quote GROUP BY ts_code
        ),
        joined AS (
            SELECT a.ts_code, a.name, COALESCE(sd.last_date, '1900-01-01'::date) as last_date
            FROM active a LEFT JOIN stock_dq sd ON a.ts_code = sd.ts_code
        )
        SELECT
            SUM(CASE WHEN last_date >= CURRENT_DATE - INTERVAL '3 days' THEN 1 ELSE 0 END) as ok,
            SUM(CASE WHEN last_date < CURRENT_DATE - INTERVAL '3 days'
                      AND last_date >= CURRENT_DATE - INTERVAL '14 days' THEN 1 ELSE 0 END) as to_fix,
            SUM(CASE WHEN last_date < CURRENT_DATE - INTERVAL '14 days'
                      AND last_date >= CURRENT_DATE - INTERVAL '120 days' THEN 1 ELSE 0 END) as suspended,
            SUM(CASE WHEN last_date < CURRENT_DATE - INTERVAL '120 days' THEN 1 ELSE 0 END) as inactive,
            COUNT(*) as total,
            ARRAY_AGG(ts_code || '|' || name || '|' || last_date::text ORDER BY last_date)
                FILTER (WHERE last_date < CURRENT_DATE - INTERVAL '3 days') as stale_list
        FROM joined
    """)
    row = cur.fetchone()
    stats[f"{prefix}daily_total"] = row[4] or 0
    stats[f"{prefix}daily_ok"] = row[0] or 0
    stats[f"{prefix}daily_to_fix"] = row[1] or 0
    stats[f"{prefix}daily_suspended"] = row[2] or 0
    stats[f"{prefix}daily_inactive"] = row[3] or 0
    stats[f"{prefix}daily_stale_details"] = [
        {"ts_code": x.split("|")[0], "name": x.split("|")[1], "last_date": x.split("|")[2]}
        for x in (row[5] or [])
    ]


def _check_financial(cur, tbl: str, current_year: int, exchange: str = "SSE,SZSE,BSE") -> dict:
    """检查某个财报表 2020+ 的完整性"""
    exchanges = "','".join(exchange.split(","))
    expected_per_year = 1 if exchange == "HKEX" else 4  # 港股仅年报, A股4份季报
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
              AND s.exchange IN ('{exchanges}')
            GROUP BY i.ts_code, i.report_year, s.list_date, s.name
        ),
        stock_years AS (
            SELECT ts_code, name, report_year, cnt, list_year, list_month,
                CASE WHEN {expected_per_year} = 1 THEN {expected_per_year}
                ELSE CASE WHEN report_year = list_year THEN
                    CASE WHEN list_month <= 3 THEN {expected_per_year}
                         WHEN list_month <= 6 THEN {expected_per_year * 3 // 4}
                         WHEN list_month <= 9 THEN {expected_per_year // 2} ELSE {expected_per_year // 4} END
                ELSE {expected_per_year} END END as expected
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
              AND s.delist_date IS NULL AND s.exchange IN ('{exchanges}')
            GROUP BY i.ts_code, i.report_year, s.list_date, s.name
        ),
        stock_years AS (
            SELECT ts_code, name, report_year, cnt,
                CASE WHEN {expected_per_year} = 1 THEN {expected_per_year}
                ELSE CASE WHEN report_year = list_year THEN
                    CASE WHEN list_month <= 3 THEN {expected_per_year}
                         WHEN list_month <= 6 THEN {expected_per_year * 3 // 4}
                         WHEN list_month <= 9 THEN {expected_per_year // 2} ELSE {expected_per_year // 4} END
                ELSE {expected_per_year} END END as expected
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


def _check_quarterly_hk(cur, tbl: str) -> dict:
    """检查港股季度数据覆盖 (2020-2025)

    港股 report_type: 1=一季报, 2=半年报, 3=三季报, 4=年报
    检查每只股票每种报告类型的覆盖情况
    """
    cur.execute(f"""
        WITH hk_stocks AS (
            SELECT ts_code FROM stocks
            WHERE delist_date IS NULL AND exchange = 'HKEX'
        ),
        has_data AS (
            SELECT ts_code, report_type, report_year
            FROM {tbl}
            WHERE ts_code LIKE '%.HK'
              AND report_year >= 2020 AND report_year < 2026
        )
        SELECT
            (SELECT COUNT(*) FROM hk_stocks) as total,
            COUNT(DISTINCT hd.ts_code)
                FILTER (WHERE hd.report_type = '1' AND hd.report_year IS NOT NULL)
                * 100.0 / NULLIF((SELECT COUNT(*) FROM hk_stocks), 0) as q1_pct,
            COUNT(DISTINCT hd.ts_code)
                FILTER (WHERE hd.report_type = '2' AND hd.report_year IS NOT NULL)
                * 100.0 / NULLIF((SELECT COUNT(*) FROM hk_stocks), 0) as semi_pct,
            COUNT(DISTINCT hd.ts_code)
                FILTER (WHERE hd.report_type = '3' AND hd.report_year IS NOT NULL)
                * 100.0 / NULLIF((SELECT COUNT(*) FROM hk_stocks), 0) as q3_pct,
            COUNT(DISTINCT hd.ts_code)
                FILTER (WHERE hd.report_type = '4' AND hd.report_year IS NOT NULL)
                * 100.0 / NULLIF((SELECT COUNT(*) FROM hk_stocks), 0) as annual_pct
        FROM has_data hd
    """)
    row = cur.fetchone()
    return {
        "total_stocks": row[0] or 0,
        "q1_pct": round(row[1] or 0, 1),
        "semi_pct": round(row[2] or 0, 1),
        "q3_pct": round(row[3] or 0, 1),
        "annual_pct": round(row[4] or 0, 1),
    }


def _real_gaps_cutoff() -> date:
    """"真实缺口"判定 cutoff：最近 60 天内无数据才算可修复缺口（动态）。"""
    return date.today() - timedelta(days=60)


def _build_daily_section(stats: dict, prefix: str, title: str, unit: str) -> list:
    """构建日线覆盖章节 (A股/港股通用)"""
    total = stats.get(f"{prefix}daily_total", 0)
    if total == 0:
        return []
    ok = stats.get(f"{prefix}daily_ok", 0)
    to_fix = stats.get(f"{prefix}daily_to_fix", 0)
    suspended = stats.get(f"{prefix}daily_suspended", 0)
    inactive = stats.get(f"{prefix}daily_inactive", 0)
    active_base = total - suspended - inactive
    daily_pct = ok / active_base * 100 if active_base else 0

    lines = [
        f"",
        f"---",
        f"## {title}",
        f"",
        f"**{'✅' if daily_pct >= 95 else '⚠️'} 完整性: {daily_pct:.1f}%** （{ok:,}/{active_base:,} {unit}在0-2天内更新）",
        f"",
        f"| 状态 | 数量 |",
        f"|------|------|",
        f"| ✅ 新鲜 (0-2天) | {ok:,} |",
    ]
    if to_fix:
        lines.append(f"| ⚠️ 近期缺口 (可修复) | {to_fix} |")
    if suspended:
        lines.append(f"| ➖ 停牌/ST (正常) | {suspended} |")
    if inactive:
        lines.append(f"| ➖ 疑似退市 (正常) | {inactive} |")
    lines.append(f"| **总计** | **{total:,}** |")
    lines.append("")

    stale_details = stats.get(f"{prefix}daily_stale_details", [])
    if stale_details:
        real_gaps = [d for d in stale_details if date.fromisoformat(d["last_date"]) >= _real_gaps_cutoff()]
        if real_gaps:
            lines.append("### 近期缺口明细（可修复）")
            lines.append("| 代码 | 名称 | 最后日期 |")
            lines.append("|------|------|----------|")
            for d in real_gaps[:10]:
                lines.append(f"| {d['ts_code']} | {d['name']} | {d['last_date']} |")
            if len(real_gaps) > 10:
                lines.append(f"| ... | ... | 共 {len(real_gaps)} 只 |")
            lines.append("")
    return lines


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
        real_gaps = [d for d in stale_details if date.fromisoformat(d["last_date"]) >= _real_gaps_cutoff()]
        if real_gaps:
            lines.append("### 近期缺口明细（可修复）")
            lines.append("| 代码 | 名称 | 最后日期 |")
            lines.append("|------|------|----------|")
            for d in real_gaps[:20]:
                lines.append(f"| {d['ts_code']} | {d['name']} | {d['last_date']} |")
            if len(real_gaps) > 20:
                lines.append(f"| ... | ... | 共 {len(real_gaps)} 只 |")
            lines.append("")

    # ── 港股日线 ──
    hk_total = stats.get("hk_daily_total", 0)
    if hk_total:
        lines.extend(_build_daily_section(stats, "hk_", "🇭🇰 港股日线数据", "只港股"))

    # ── 数据表覆盖 (A+H 拆分) ──
    lines.extend([
        f"---",
        f"## 📊 数据表覆盖 (A+H)",
        f"| 表 | 总行数 | 港股部分 | 时间范围 |",
        f"|-----|------|----------|----------|",
    ])
    table_order = ["daily_quote", "income", "balance_sheet", "cashflow",
                    "financial_indicator", "stock_valuation", "index_daily"]
    for tbl in table_order:
        info = stats.get(tbl, {})
        hk_info = stats.get(f"{tbl}_hk", {})
        hk_rows = hk_info.get("rows", 0)
        hk_str = f"{hk_rows:,}" if hk_rows > 0 else "-"
        lines.append(
            f"| {info.get('name', tbl)} | {info.get('rows', 0):,} | {hk_str} | {info.get('range', 'N/A')} |"
        )

    # ── 财报完整性 (A股) ──
    lines.extend([
        f"",
        f"---",
        f"## 🔍 A股财报完整性 (2020-2025)",
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

    # ── 港股财报 ──
    hk_fin_has_data = any(stats.get(f"fin_hk_{tbl}", {}).get("total_stocks", 0) > 0 for tbl in ["income", "balance_sheet", "cashflow"])
    if hk_fin_has_data:
        lines.extend([
            f"",
            f"---",
            f"## 🔍 🇭🇰 港股财报",
            f"",
            f"### 季度数据覆盖 (2020-2025)",
            f"",
            f"| 报表 | 年报 | 半年报 | 一季报 | 三季报 |",
            f"|------|------|--------|--------|--------|",
        ])
        for tbl in ["income", "balance_sheet", "cashflow"]:
            q = stats.get(f"fin_hk_q_{tbl}", {})
            annual = f"{q.get('annual_pct', 0)}%" if q else "-"
            semi = f"{q.get('semi_pct', 0)}%" if q and q.get('semi_pct', 0) > 0 else "—"
            q1 = f"{q.get('q1_pct', 0)}%" if q and q.get('q1_pct', 0) > 0 else "—"
            q3 = f"{q.get('q3_pct', 0)}%" if q and q.get('q3_pct', 0) > 0 else "—"
            lines.append(
                f"| {names[tbl]} | {annual} | {semi} | {q1} | {q3} |"
            )
        lines.append(f"")
        lines.append(f"*港股当前季度数据覆盖率低（仅年报/半年报），后续管道会逐步拉取。*")
        lines.append(f"")

        # Annual completeness (honest - only shows annual report completeness)
        lines.extend([
            f"### 年报完整率 (2020-2025)",
            f"",
            f"| 报表 | 年报完整率 | 严重缺失(≥2年) | 轻度(1年) |",
            f"|------|-----------|-----------------|-----------|",
        ])
        for tbl in ["income", "balance_sheet", "cashflow"]:
            info = stats.get(f"fin_hk_{tbl}", {})
            lines.append(
                f"| {names[tbl]} | {info.get('stock_year_pct', 0)}% "
                f"| {info.get('severe_cnt', 0)} "
                f"| {info.get('minor_cnt', 0)} |"
            )

    # ── 数据域新鲜度总览 (M5) ──
    freshness = stats.get("freshness", [])
    if freshness:
        lines.extend([
            f"",
            f"---",
            f"## 🕐 数据域新鲜度（基准 {stats.get('freshness_base', 'N/A')}）",
            f"",
            f"| 数据域 | 最新日期 | 标的数 | 落后交易日 | 状态 |",
            f"|--------|----------|--------|------------|------|",
        ])
        for f_ in freshness:
            lag = f_.get("lag")
            lag_str = f"{lag}" if lag is not None else "—"
            status = SUCCESS if f_.get("ok") else WARN
            lines.append(f"| {f_['domain']} | {f_.get('max_date', '?')} | {f_.get('symbols', 0):,} | {lag_str} | {status} |")

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

def build_feishu_report(stats: dict, github_ok: bool, missing_report: dict | None = None) -> str:
    """构建飞书消息 Markdown（含数据域新鲜度 + 缺口 TOP10）"""
    total = stats.get("daily_total", 0)
    ok = stats.get("daily_ok", 0)
    suspended = stats.get("daily_suspended", 0)
    inactive = stats.get("daily_inactive", 0)
    active_base = total - suspended - inactive
    daily_pct = ok / active_base * 100 if active_base else 0
    
    lines = [
        f"**📊 stock_data 数据报告** — {date.today()}",
        f"",
        f"**A股日线**: {SUCCESS} {daily_pct:.1f}% ({ok}/{active_base})",
    ]
    to_fix = stats.get("daily_to_fix", 0)
    if to_fix:
        lines.append(f"可修复缺口: {to_fix} 只")
    if suspended:
        lines.append(f"停牌/ST: {suspended} 只（不计入缺失）")

    # HK daily
    hk_total = stats.get("hk_daily_total", 0)
    hk_ok = stats.get("hk_daily_ok", 0)
    hk_suspended = stats.get("hk_daily_suspended", 0)
    hk_inactive = stats.get("hk_daily_inactive", 0)
    hk_active = hk_total - hk_suspended - hk_inactive
    hk_pct = hk_ok / hk_active * 100 if hk_active else 0
    hk_to_fix = stats.get("hk_daily_to_fix", 0)
    if hk_total:
        lines.append(f"**🇭🇰 港股日线**: {'✅' if hk_pct >= 95 else '⚠️'} {hk_pct:.1f}% ({hk_ok}/{hk_active})")
        if hk_to_fix:
            lines.append(f"港股缺口: {hk_to_fix} 只")

    # ── 数据域新鲜度 (M5) ──
    freshness = stats.get("freshness", [])
    if freshness:
        lines.append(f"\n**🕐 数据域新鲜度**（基准 {stats.get('freshness_base', 'N/A')}）:")
        for f_ in freshness:
            lag = f_.get("lag")
            if lag is None and not f_.get("ok"):
                mark = "❓"
            else:
                mark = "✅" if f_.get("ok") else "⚠️"
            lag_str = f"落后{lag}交易日" if lag is not None else ""
            lines.append(f"- {mark} {f_['domain']}: {f_.get('max_date', '?')} {lag_str}")

    # ── 缺口 TOP10 (M5，来自 validate 缺失报告) ──
    if missing_report:
        gaps = missing_report.get("daily_gaps", [])
        if gaps:
            lines.append(f"**缺口 TOP10**（共 {len(gaps)} 只）:")
            for g in gaps[:10]:
                lines.append(f"- {g['ts_code']} {g.get('name', '')} (止于 {g['last_date']}, {g.get('reason', '')})")
            lines.append("")

    # 缺失明细（旧逻辑，保留作为兑底）
    stale_details = stats.get("daily_stale_details", [])
    real_gaps = [d for d in stale_details if date.fromisoformat(d["last_date"]) >= _real_gaps_cutoff()]
    if real_gaps and not missing_report:
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
    """推送飞书报告卡片，优先私聊"""
    return _push_feishu_card(text, title="📊 stock_data 数据报告", template="blue")


def push_feishu_alert(text: str) -> bool:
    """推送飞书告警卡片（红色模板）"""
    return _push_feishu_card(text, title="⚠️ stock_data 告警", template="red")


def _push_feishu_card(text: str, title: str, template: str) -> bool:
    creds = _get_feishu_credentials()
    if not creds:
        print("[Feishu] 未找到凭证")
        return False

    app_id, app_secret = creds
    token = _get_feishu_token(app_id, app_secret)
    if not token:
        print("[Feishu] 获取 token 失败")
        return False

    # 尝试私聊
    open_id = _discover_user(token)
    if open_id:
        ok = _send_message(token, "open_id", open_id, text, title=title, template=template)
        print(f"[Feishu] 消息发送: {'✅ 成功' if ok else '❌ 失败'}")
        return ok

    print("[Feishu] 未找到接收用户")
    return False


def _get_feishu_credentials():
    # 优先环境变量（.env 注入，launchd 环境可用）
    import os
    env_id = os.environ.get("FEISHU_APP_ID", "").strip()
    env_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
    if env_id and env_secret:
        return env_id, env_secret
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


def _send_message(token, receive_type, receive_id, text, title="📊 stock_data 数据报告", template="blue"):
    try:
        r = requests.post(
            f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={receive_type}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"receive_id": receive_id, "msg_type": "interactive",
                  "content": json.dumps({
                      "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
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

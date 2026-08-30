#!/usr/bin/env python3
"""stock_data 数据库级数据完整性校验脚本

校验维度（默认查 PostgreSQL，支持 CSV 模式）：
  1. 全局时间范围 — 各表是否覆盖 2006 ~ 今天
  2. 日线逐标连续性 — 每个标的从上市到今天的覆盖情况
  3. 退市股识别 — 上市中但数据陈旧 → 疑似退市未标记
  4. 财报连续性 — 每只股票每年应有 4 份财报
  5. 数据新鲜度 — 最后更新日期分布
  6. OHLC 合理性 — 价格边界、空值
  7. CSV 文件模式（可选）— 原有的文件级检查

用法：
  python validate.py                        # DB 全面检查（默认）
  python validate.py --source csv           # 仅检查 CSV 文件
  python validate.py --source both          # 同时检查 DB 和 CSV
  python validate.py --quick                # 快速模式（跳过财报连续性等大查询）
  python validate.py --symbol 600519.SH     # 单标的
  python validate.py --report report.json   # 输出 JSON 报告
  python validate.py --expected-start 2006  # 覆盖起点（默认2006）
  python validate.py --stale-days 7         # 陈旧判定阈值（默认7天，港股放宽2交易日）
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

# ── 路径 ────────────────────────────────────────────────────────────────
REPO = Path(__file__).parent
DAILY_DIR = REPO / "daily"
MARKET_DIR = REPO / "market"
INDEX_DIR = REPO / "index"
MACRO_DIR = REPO / "macro"
META_DIR = REPO / "meta"
FUND_DIR = REPO / "fundamental"

# ── 状态图标 ─────────────────────────────────────────────────────────────
OK = "✅"
WARN = "⚠️"
ERR = "❌"

# ── 全局 ─────────────────────────────────────────────────────────────────
all_issues: list[dict] = []
missing_data: dict = {
    "generated_at": "",
    "daily_gaps": [],      # 日线缺失明细
    "financial_gaps": {},   # 财报缺失明细 (per table)
    "table_gaps": [],       # 表级时间落后
    "no_data_stocks": [],   # 完全无日线的标的
    "summary": {},          # 汇总
}
start_time = time.time()
TODAY = date.today()
_daily_stats = {"total": 0, "ok": 0}  # 逐标检查统计，供缺失报告 summary 使用


def issue(level: str, category: str, item: str, detail: str = ""):
    """记录一个检查项"""
    all_issues.append({"level": level, "category": category, "item": item, "detail": detail})
    icon = {"OK": OK, "WARN": WARN, "ERR": ERR}[level]
    msg = f"  {icon} [{category}] {item}"
    if detail:
        msg += f" — {detail}"
    print(msg)


# ═══════════════════════════════════════════════════════════════════════════
# PostgreSQL 连接
# ═══════════════════════════════════════════════════════════════════════════

class PostgresDB:
    """PostgreSQL 连接封装"""

    def __init__(self):
        self.conn = None
        self.available = False
        self._connect()

    def _connect(self):
        try:
            import psycopg2
            import psycopg2.extras
            self.conn = psycopg2.connect(
                host=os.environ.get("PGHOST", "/tmp"),
                dbname=os.environ.get("PGDATABASE", "investassist"),
                user=os.environ.get("PGUSER", "james"),
                password=os.environ.get("PGPASSWORD", ""),
                connect_timeout=5,
            )
            self.conn.autocommit = True
            self.available = True
        except Exception as e:
            print(f"[DB] PostgreSQL 不可用: {e}")
            self.available = False

    def query(self, sql: str, params: tuple = None) -> list[dict]:
        """执行查询，返回 dict 列表"""
        import psycopg2.extras
        cur = self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cur.execute(sql, params)
            rows = cur.fetchall()
            return rows
        finally:
            cur.close()

    def query_scalar(self, sql: str, params: tuple = None):
        """执行查询，返回单个标量值"""
        cur = self.conn.cursor()
        cur.execute(sql, params or ())
        val = cur.fetchone()
        cur.close()
        return val[0] if val else None


# ═══════════════════════════════════════════════════════════════════════════
# 1. 全局时间范围
# ═══════════════════════════════════════════════════════════════════════════

def check_table_ranges(db: PostgresDB, expected_start: int, stale_days: int = 7):
    """检查各核心表的时间覆盖范围（基准动态化：日线表对比 TODAY，年度表对比应有报告年度）"""
    print("\n── 各表时间范围 ──")

    # report_year 为 varchar 的财报/估值类表，按年度口径判定
    year_tables = {"income", "balance_sheet", "cashflow", "financial_indicator", "stock_valuation"}

    tables = {
        "daily_quote": ("日线行情", "trade_date"),
        "income": ("利润表", "report_year"),
        "balance_sheet": ("资产负债表", "report_year"),
        "cashflow": ("现金流量表", "report_year"),
        "financial_indicator": ("财务指标", "report_year"),
        "stock_valuation": ("估值数据", "valuation_year"),
        "index_daily": ("指数日线", "trade_date"),
        "stocks": ("股票列表", "list_date"),
    }

    for tbl, (name, date_col) in tables.items():
        try:
            min_d = db.query_scalar(f"SELECT MIN({date_col}) FROM {tbl}")
            max_d = db.query_scalar(f"SELECT MAX({date_col}) FROM {tbl}")
            cnt = db.query_scalar(f"SELECT COUNT(*) FROM {tbl}")

            min_s, max_s = str(min_d or "N/A"), str(max_d or "N/A")

            if tbl in year_tables:
                # 年度表：当年 Q1 披露截止 4/30，5 月起应有当年数据
                expected_year = TODAY.year if TODAY.month >= 5 else TODAY.year - 1
                try:
                    max_year = int(str(max_d)) if max_d else None
                except (TypeError, ValueError):
                    max_year = None
                if max_year and max_year >= expected_year:
                    level = "OK"
                    detail = f"{min_s} ~ {max_s}  |  {cnt:,} 行"
                elif max_year:
                    behind_years = expected_year - max_year
                    level = "WARN"
                    detail = f"{min_s} ~ {max_s}  |  {cnt:,} 行  |  落后 {behind_years} 个报告年度"
                    missing_data["table_gaps"].append({
                        "table": tbl,
                        "name": name,
                        "min_date": min_s,
                        "max_date": max_s,
                        "expected_end": str(expected_year),
                        "days_behind": behind_years * 365,
                        "row_count": cnt,
                    })
                else:
                    level = "WARN"
                    detail = f"{min_s} ~ {max_s}  |  {cnt:,} 行"
            else:
                # 日期表：对比 TODAY，容差 stale_days 天
                max_val = None
                if max_d and isinstance(max_d, (date, datetime)):
                    max_val = max_d if isinstance(max_d, date) else max_d.date()

                if max_val and (TODAY - max_val).days <= stale_days:
                    level = "OK"
                    detail = f"{min_s} ~ {max_s}  |  {cnt:,} 行"
                elif max_val:
                    days_behind = (TODAY - max_val).days
                    level = "WARN"
                    detail = f"{min_s} ~ {max_s}  |  {cnt:,} 行  |  落后 {days_behind} 天"
                    missing_data["table_gaps"].append({
                        "table": tbl,
                        "name": name,
                        "min_date": min_s,
                        "max_date": max_s,
                        "expected_end": str(TODAY),
                        "days_behind": days_behind,
                        "row_count": cnt,
                    })
                else:
                    level = "WARN"
                    detail = f"{min_s} ~ {max_s}  |  {cnt:,} 行"

            issue(level, "range", f"{name} ({tbl})", detail)

        except Exception as e:
            issue("ERR", "range", tbl, str(e))


# ═══════════════════════════════════════════════════════════════════════════
# 2. 日线逐标连续性（核心检查）
# ═══════════════════════════════════════════════════════════════════════════

def check_daily_per_stock(db: PostgresDB, expected_start: int, quick: bool = False,
                          symbol: str = None, stale_days: int = 7):
    """检查每只股票的日线数据连续性

    规则（基准动态化）：
    - 上市中 + last_date >= TODAY - stale_days → 正常（港股交易日历不同，额外放宽 2 个交易日）
    - 上市中 + 数据早于 cutoff → 需排查（退市未标记 or 数据缺失）
    - 已退市 → 数据应到退市日
    """
    print("\n── 日线逐标连续性 ──")

    try:
        # 获取每只股票的元数据 + 日线覆盖
        where_clause = ""
        if symbol:
            where_clause = f"WHERE s.ts_code = '{symbol}'"

        # 找数据陈旧但标记为"上市中"的股票
        sql = f"""
        WITH stock_dq AS (
            SELECT ts_code, MIN(trade_date) as first_date, MAX(trade_date) as last_date, COUNT(*) as days
            FROM daily_quote
            GROUP BY ts_code
        )
        SELECT
            s.ts_code,
            s.name,
            s.list_date::text,
            s.delist_date::text,
            s.list_status,
            s.exchange,
            COALESCE(sd.first_date::text, '无数据') as first_date,
            COALESCE(sd.last_date::text, '无数据') as last_date,
            COALESCE(sd.days, 0) as trading_days
        FROM stocks s
        LEFT JOIN stock_dq sd ON s.ts_code = sd.ts_code
        {where_clause}
        ORDER BY sd.last_date DESC NULLS LAST
        """

        rows = db.query(sql)

        total = len(rows)
        no_data = []
        stale_active = []  # 上市中但数据陈旧
        ok_active = 0
        delisted_ok = 0
        delisted_stale = []

        # 超过 cutoff 未更新 → 视为陈旧；港股交易日历与 A 股不同，放宽 2 个交易日
        cutoff_stale = TODAY - timedelta(days=stale_days)
        try:
            cutoff_hk = db.query_scalar(
                """SELECT cal_date FROM trade_cal
                   WHERE is_open::int = 1 AND cal_date <= %s
                   ORDER BY cal_date DESC OFFSET 2 LIMIT 1""",
                (cutoff_stale,),
            ) or (cutoff_stale - timedelta(days=3))
        except Exception:
            cutoff_hk = cutoff_stale - timedelta(days=3)

        for r in rows:
            ts = r["ts_code"]
            name = r["name"]
            last_d = r["last_date"]
            list_d = r["list_date"]
            delist_d = r["delist_date"]
            status = r["list_status"]

            if last_d == "无数据":
                no_data.append((ts, name))
                continue

            last_date = date.fromisoformat(last_d)

            if delist_d and delist_d != "None":
                # 已退市
                delist_date = date.fromisoformat(delist_d)
                if (delist_date - last_date).days > 30:
                    delisted_stale.append((ts, name, last_d, delist_d))
                else:
                    delisted_ok += 1
            else:
                # 上市中（港股用放宽后的 cutoff）
                cutoff = cutoff_hk if ts.endswith(".HK") else cutoff_stale
                if last_date >= cutoff:
                    ok_active += 1
                else:
                    stale_active.append((ts, name, last_d, list_d or "N/A", r["trading_days"]))

        # ── 汇报 ──
        issue("OK", "daily", f"上市中且数据新鲜", f"{ok_active} 只")

        if delisted_ok > 0:
            issue("OK", "daily", f"已退市数据正常", f"{delisted_ok} 只")

        if stale_active:
            # 分档
            ancient = [(t, n, ld) for t, n, ld, _, _ in stale_active if ld < "2010-01-01"]
            recent_stale = [(t, n, ld) for t, n, ld, _, _ in stale_active
                            if ld >= "2010-01-01" and not t.endswith(".HK")]
            hk_stale = [(t, n, ld) for t, n, ld, _, _ in stale_active if t.endswith(".HK")]

            # 填充 missing_data
            for ts, name, last_d, list_d, days in stale_active:
                try:
                    ldate = date.fromisoformat(last_d)
                    days_behind = (TODAY - ldate).days
                except Exception:
                    days_behind = 999
                missing_data["daily_gaps"].append({
                    "ts_code": ts,
                    "name": name,
                    "last_date": last_d,
                    "list_date": list_d or "N/A",
                    "trading_days": days,
                    "days_behind": days_behind,
                    "reason": "港股滞后" if ts.endswith(".HK") else
                              ("已退市(未标记)" if last_d < "2010-01-01" else "数据滞后"),
                })

            issue(
                "ERR" if len(stale_active) > 50 else "WARN",
                "daily",
                f"上市中但数据陈旧",
                f"{len(stale_active)} 只",
            )

            if ancient:
                names = ", ".join(f"{t}({n})" for t, n, ld in ancient[:8])
                issue("WARN", "daily", f"  → 疑似退市未标记 (停更早于2010)", f"{len(ancient)} 只: {names}")
            if hk_stale:
                names = ", ".join(f"{t}({n})" for t, n, ld in hk_stale[:5])
                issue("WARN", "daily", f"  → 港股数据滞后", f"{len(hk_stale)} 只: {names}")
            if recent_stale:
                names = ", ".join(f"{t}({n})" for t, n, ld in recent_stale[:5])
                issue("WARN", "daily", f"  → A股数据滞后 (>1个月)", f"{len(recent_stale)} 只: {names}")

        if delisted_stale:
            names = ", ".join(f"{t}({n})" for t, n, ld, dd in delisted_stale[:5])
            issue("WARN", "daily", f"已退市但数据未到退市日", f"{len(delisted_stale)} 只: {names}")

        if no_data:
            names = ", ".join(f"{t}({n})" for t, n in no_data[:10])
            issue("ERR", "daily", f"无日线数据", f"{len(no_data)} 只: {names}")
            for ts, name in no_data:
                missing_data["no_data_stocks"].append({
                    "ts_code": ts,
                    "name": name,
                    "reason": "港股无数据" if ts.endswith(".HK") else "无日线记录",
                })

        print(f"\n  总计: {total} 上市中新鲜 {ok_active} | 陈旧 {len(stale_active)} | 无数据 {len(no_data)}")
        _daily_stats["total"] = total
        _daily_stats["ok"] = ok_active

    except Exception as e:
        issue("ERR", "daily", "逐标检查失败", str(e))


# ═══════════════════════════════════════════════════════════════════════════
# 3. 日线数据新鲜度分布
# ═══════════════════════════════════════════════════════════════════════════

def check_daily_freshness(db: PostgresDB, quick: bool = False):
    """按最后更新日期分组统计"""
    if quick:
        return

    print("\n── 日线新鲜度分布 ──")

    try:
        sql = """
        WITH last_dates AS (
            SELECT ts_code, MAX(trade_date) as last_date
            FROM daily_quote
            GROUP BY ts_code
        )
        SELECT
            CASE
                WHEN last_date >= CURRENT_DATE - INTERVAL '3 days' THEN '0-2天前'
                WHEN last_date >= CURRENT_DATE - INTERVAL '7 days' THEN '3-7天前'
                WHEN last_date >= CURRENT_DATE - INTERVAL '14 days' THEN '8-14天前'
                WHEN last_date >= CURRENT_DATE - INTERVAL '30 days' THEN '15-30天前'
                WHEN last_date >= CURRENT_DATE - INTERVAL '90 days' THEN '31-90天前'
                WHEN last_date >= date_trunc('year', CURRENT_DATE) THEN '今年较早'
                ELSE '往年'
            END as freshness,
            COUNT(*) as cnt
        FROM last_dates
        GROUP BY freshness
        ORDER BY MIN(last_date) DESC
        """
        rows = db.query(sql)

        total = sum(r["cnt"] for r in rows)
        for r in rows:
            pct = r["cnt"] / total * 100 if total else 0
            level = "WARN" if r["freshness"] in ("15-30天前", "31-90天前", "今年较早", "往年") else "OK"
            issue(level, "freshness", r["freshness"], f"{r['cnt']:,} 只 ({pct:.1f}%)")

    except Exception as e:
        issue("ERR", "freshness", "查询失败", str(e))


# ═══════════════════════════════════════════════════════════════════════════
# 3.5 各数据域新鲜度总览
# ═══════════════════════════════════════════════════════════════════════════

A_SHARE_INDEX_SYMBOLS = ("000001.SH", "000016.SH", "000300.SH", "000688.SH", "000852.SH",
                         "000905.SH", "399001.SZ", "399005.SZ", "399006.SZ")


def _macro_latest_date(path: Path):
    """解析宏观 CSV 的最新数据日期；无法解析返回 None（兼容正序/倒序、YYYYMM/YYYYMMDD/ISO 等格式）"""
    try:
        df = pd.read_csv(path, dtype=str)
    except Exception:
        return None
    if df.empty:
        return None
    # 候选列：命名日期列优先，其余按内容扫描
    named = [c for c in df.columns if str(c).strip().lower() in ("date", "日期", "month", "月份", "day")]
    others = [c for c in df.columns if c not in named]
    for c in named + others:
        col = df[c].dropna().astype(str).str.strip()
        if col.empty:
            continue
        # 显式格式优先（避免 dateutil 对 YYYYMM 的歧义解析）
        for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y%m"):
            parsed = pd.to_datetime(col, format=fmt, errors="coerce").dropna()
            if len(parsed) >= max(1, len(col) // 2) and parsed.dt.year.between(1990, 2030).all():
                return parsed.max().date()
        # 兜底：无固定格式（如 '2026-06-01 08:46:58'）
        parsed = pd.to_datetime(col, errors="coerce").dropna()
        parsed = parsed[parsed.dt.year.between(1990, 2030)]
        if len(parsed) >= max(1, len(col) // 2):
            return parsed.max().date()
    return None


def check_freshness_overview(db: PostgresDB):
    """各数据域新鲜度总览：MAX(date) vs 最近 A 股交易日，直接给出落后 N 交易日"""
    print("\n── 数据域新鲜度总览 ──")

    try:
        last_td = db.query_scalar(
            "SELECT MAX(cal_date) FROM trade_cal WHERE is_open::int = 1 AND cal_date <= CURRENT_DATE")
        if not last_td:
            issue("ERR", "overview", "基准交易日", "trade_cal 无数据，无法计算基准")
            return
        print(f"  基准（最近 A 股交易日）: {last_td}")

        def lag_trading_days(d):
            if d is None:
                return None
            return db.query_scalar(
                "SELECT COUNT(*) FROM trade_cal WHERE is_open::int = 1 AND cal_date > %s AND cal_date <= %s",
                (d, last_td))

        def report_date_domain(name, max_d, n, extra_tol=0):
            """日线类数据域：落后 N 交易日。extra_tol>0 表示允许的预期滞后（如海外指数）"""
            if max_d is None:
                issue("ERR", "overview", name, "无数据")
                return
            lag = lag_trading_days(max_d)
            if lag is None:
                issue("WARN", "overview", name, f"MAX={max_d}  |  {n} 标的")
                return
            if lag <= extra_tol:
                tail = "（预期滞后）" if extra_tol else ""
                issue("OK", "overview", name, f"MAX={max_d}  |  {n} 标的  |  落后 {lag} 交易日{tail}")
            else:
                issue("ERR" if lag > 5 else "WARN", "overview", name,
                      f"MAX={max_d}  |  {n} 标的  |  落后基准 {last_td} 共 {lag} 个交易日")

        a_idx_in = ",".join(f"'{s}'" for s in A_SHARE_INDEX_SYMBOLS)
        date_domains = [
            ("A股日线", f"SELECT MAX(trade_date) m, COUNT(DISTINCT ts_code) n FROM daily_quote WHERE ts_code NOT LIKE '%.HK'", 0),
            ("港股日线", f"SELECT MAX(trade_date) m, COUNT(DISTINCT ts_code) n FROM daily_quote WHERE ts_code LIKE '%.HK'", 2),
            ("ETF日线", "SELECT MAX(trade_date) m, COUNT(DISTINCT code) n FROM etf_quote", 0),
            ("A股指数", f"SELECT MAX(trade_date) m, COUNT(DISTINCT symbol) n FROM index_daily WHERE symbol IN ({a_idx_in})", 0),
            # 海外/港股指数数据要到北京时间次日才有，天然滞后 1 个交易日
            ("海外/港股指数", f"SELECT MAX(trade_date) m, COUNT(DISTINCT symbol) n FROM index_daily WHERE symbol NOT IN ({a_idx_in})", 1),
        ]
        for name, sql, extra_tol in date_domains:
            try:
                r = db.query(sql)[0]
                report_date_domain(name, r["m"], r["n"], extra_tol=extra_tol)
            except Exception as e:
                issue("ERR", "overview", name, str(e))

        # 财报类：最新报告年度 vs 应有年度（当年 Q1 披露截止 4/30）
        expected_year = TODAY.year if TODAY.month >= 5 else TODAY.year - 1
        for tbl, name in [("income", "利润表"), ("balance_sheet", "资产负债表"),
                          ("cashflow", "现金流量表"), ("financial_indicator", "财务指标")]:
            try:
                r = db.query(
                    f"SELECT MAX(report_year) ry, MAX(ann_date) ad, COUNT(DISTINCT ts_code) n FROM {tbl}")[0]
                ry, ad, n = r["ry"], r["ad"], r["n"]
                if not ry:
                    issue("ERR", "overview", f"财报·{name}", f"{tbl} 无数据")
                    continue
                try:
                    ry_i = int(str(ry))
                except (TypeError, ValueError):
                    ry_i = 0
                if ry_i >= expected_year:
                    issue("OK", "overview", f"财报·{name}", f"最新报告年度 {ry}  |  ann_date 至 {ad}  |  {n} 标的")
                else:
                    issue("WARN", "overview", f"财报·{name}",
                          f"最新报告年度 {ry}（应有 {expected_year}）|  ann_date 至 {ad}  |  {n} 标的")
                    missing_data["table_gaps"].append({
                        "table": tbl, "name": f"财报·{name}",
                        "min_date": "", "max_date": str(ry),
                        "expected_end": str(expected_year),
                        "days_behind": (expected_year - ry_i) * 365 if ry_i else 9999,
                        "row_count": 0,
                    })
            except Exception as e:
                issue("ERR", "overview", f"财报·{name}", str(e))

        # 宏观 CSV（直写文件，不走 PG）
        monthly_domains = {"cpi", "pmi", "money_supply"}  # 月度数据，容忍度放宽
        for f in sorted(MACRO_DIR.glob("*.csv")):
            try:
                latest = _macro_latest_date(f)
                if latest is None:
                    mtime = datetime.fromtimestamp(f.stat().st_mtime)
                    issue("WARN", "overview", f"宏观·{f.stem}",
                          f"无法解析数据日期，文件更新于 {mtime:%Y-%m-%d}")
                    continue
                days = (TODAY - latest).days
                tol = 45 if f.stem in monthly_domains else 10
                if days <= tol:
                    issue("OK", "overview", f"宏观·{f.stem}", f"最新数据 {latest}")
                else:
                    issue("WARN", "overview", f"宏观·{f.stem}",
                          f"最新数据 {latest}，距今 {days} 天")
            except Exception as e:
                issue("ERR", "overview", f"宏观·{f.stem}", str(e))

    except Exception as e:
        issue("ERR", "overview", "总览查询失败", str(e))


# ═══════════════════════════════════════════════════════════════════════════
# 4. OHLC 数据质量（PostgreSQL）
# ═══════════════════════════════════════════════════════════════════════════

def check_ohlc_quality(db: PostgresDB):
    """检查 PostgreSQL 中日线数据的 OHLC 合理性"""
    print("\n── OHLC 数据质量 (PostgreSQL) ──")

    try:
        # 负值 / 零值
        neg = db.query_scalar("""
            SELECT COUNT(*) FROM daily_quote
            WHERE open <= 0 OR high <= 0 OR low <= 0 OR close <= 0
        """)
        if neg:
            issue("ERR", "ohlc", "负值或零值价格", f"{neg:,} 行")
        else:
            issue("OK", "ohlc", "无负价或零价")

        # high < low
        hl = db.query_scalar("""
            SELECT COUNT(*) FROM daily_quote WHERE high < low
        """)
        if hl:
            issue("ERR", "ohlc", "high < low", f"{hl:,} 行")
        else:
            issue("OK", "ohlc", "high ≥ low 全部正确")

        # 涨跌幅异常（>20% 单日）
        extreme = db.query_scalar("""
            SELECT COUNT(*) FROM daily_quote WHERE ABS(pct_chg) > 20
        """)
        if extreme:
            issue("WARN", "ohlc", "涨跌幅 > ±20%", f"{extreme:,} 行（可能含除权或数据异常）")

        # NULL 值
        nulls = db.query_scalar("""
            SELECT COUNT(*) FROM daily_quote
            WHERE open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL
        """)
        if nulls:
            issue("WARN", "ohlc", "OHLC 含 NULL", f"{nulls:,} 行")
        else:
            issue("OK", "ohlc", "OHLC 无 NULL")

    except Exception as e:
        issue("ERR", "ohlc", "查询失败", str(e))


# ═══════════════════════════════════════════════════════════════════════════
# 5. 财报连续性
# ═══════════════════════════════════════════════════════════════════════════

def check_financial_continuity(db: PostgresDB, expected_start: int):
    """检查每只股票的财报报告期是否连续"""
    print(f"\n── 财报连续性 (利润表/资产负债表/现金流, {expected_start}～{TODAY.year}) ──")

    tables = {
        "income": "利润表",
        "balance_sheet": "资产负债表",
        "cashflow": "现金流量表",
    }

    # 当前年份预期报告期数（A股财报截止日：Q1=4/30, Q2=8/31, Q3=10/31, 年报=次年4/30）
    current_month = TODAY.month
    if current_month <= 4:
        current_year_expected = 0  # 当年尚无截止的报表
    elif current_month <= 8:
        current_year_expected = 1  # Q1 已截止
    elif current_month <= 10:
        current_year_expected = 2  # Q1+Q2 已截止
    else:
        current_year_expected = 3  # Q1+Q2+Q3 已截止（年报次年才出）

    for tbl, name in tables.items():
        try:
            # 简化：只查 A 股（沪深北），不包含港股
            sql = f"""
            WITH yearly AS (
                SELECT ts_code, report_year, COUNT(*) as reports
                FROM {tbl}
                WHERE report_year >= {expected_start}
                GROUP BY ts_code, report_year
            ),
            stock_years AS (
                SELECT
                    s.ts_code, s.name, s.exchange,
                    EXTRACT(YEAR FROM s.list_date)::int as list_year,
                    EXTRACT(MONTH FROM s.list_date)::int as list_month,
                    GENERATE_SERIES(
                        GREATEST({expected_start}, EXTRACT(YEAR FROM s.list_date)::int),
                        LEAST({TODAY.year}, COALESCE(EXTRACT(YEAR FROM s.delist_date)::int, {TODAY.year}))
                    ) as report_year
                FROM stocks s
                WHERE s.list_date IS NOT NULL
                  AND s.delist_date IS NULL
                  AND (s.exchange IN ('SSE', 'SZSE', 'BSE')
                       OR s.ts_code LIKE '%.SH' OR s.ts_code LIKE '%.SZ' OR s.ts_code LIKE '%.BJ')
            ),
            joined AS (
                SELECT sy.*, COALESCE(yr.reports, 0) as actual_reports
                FROM stock_years sy
                LEFT JOIN yearly yr ON sy.ts_code = yr.ts_code AND sy.report_year = yr.report_year
            )
            SELECT
                ts_code, name,
                report_year,
                actual_reports
            FROM joined
            ORDER BY ts_code, report_year
            """

            rows = db.query(sql)
            if not rows:
                issue("OK", "financial", name, "无数据或全部完整")
                continue

            # Python 侧聚合（含 IPO 年调整）
            stock_data = defaultdict(lambda: {"name": "", "years": {}, "missing_years": 0, "partial_years": 0})
            for r in rows:
                ts = r["ts_code"]
                sd = stock_data[ts]
                sd["name"] = r["name"]
                yr = r["report_year"]
                actual = r["actual_reports"]
                ly = r.get("list_year", yr)  # 上市年份
                lm = r.get("list_month", 1)  # 上市月份

                if yr < TODAY.year:
                    # 非当年：IPO年份按上市月份调整预期
                    if yr == ly:
                        if lm <= 3: expected = 4      # Q1 上市，全年有 4 份
                        elif lm <= 6: expected = 3    # Q2 上市，最多 3 份
                        elif lm <= 9: expected = 2    # Q3 上市，最多 2 份
                        else: expected = 1             # Q4 上市，最多 1 份
                    else:
                        expected = 4
                else:
                    # 当年按A股截止日
                    expected = current_year_expected

                sd["years"][yr] = (actual, expected)

            # 统计
            incomplete = []
            for ts_code, sd in stock_data.items():
                missing = sum(1 for yr, (act, exp) in sd["years"].items() if act == 0)
                partial = sum(1 for yr, (act, exp) in sd["years"].items() if 0 < act < exp)
                if missing > 0 or partial > 0:
                    incomplete.append({
                        "ts_code": ts_code,
                        "name": sd["name"],
                        "missing_years": missing,
                        "partial_years": partial,
                    })

            if not incomplete:
                issue("OK", "financial", name, f"{len(stock_data)} 只 A股全部完整")
                missing_data["financial_gaps"][tbl] = []
            else:
                severe = [r for r in incomplete if r["missing_years"] > 0]
                partial_only = [r for r in incomplete if r["missing_years"] == 0]

                # 填充 missing_data
                missing_data["financial_gaps"][tbl] = [
                    {
                        "ts_code": r["ts_code"],
                        "name": r["name"],
                        "missing_years": r["missing_years"],
                        "partial_years": r["partial_years"],
                        "severity": "missing" if r["missing_years"] > 0 else "partial",
                    }
                    for r in incomplete
                ]

                issue(
                    "WARN",
                    "financial",
                    name,
                    f"{len(incomplete)}/{len(stock_data)} 只不完整 ({len(severe)} 缺年份, {len(partial_only)} 缺季度)",
                )
                if severe:
                    names = ", ".join(f"{r['ts_code']}({r['name']}缺{r['missing_years']}年)" for r in severe[:5])
                    issue("WARN", "financial", f"  → 缺整年报表 ({len(severe)} 只)", names)
                if partial_only:
                    names = ", ".join(f"{r['ts_code']}({r['name']}缺{r['partial_years']}季)" for r in partial_only[:3])
                    issue("WARN", "financial", f"  → 缺季度报表 ({len(partial_only)} 只)", names)

        except Exception as e:
            issue("ERR", "financial", name, str(e))


# ═══════════════════════════════════════════════════════════════════════════
# 6. 交易日历完整性
# ═══════════════════════════════════════════════════════════════════════════

def check_trade_cal(db: PostgresDB, expected_start: int):
    """检查 trade_cal 表是否覆盖应有的交易日"""
    print("\n── 交易日历 ──")

    try:
        cnt = db.query_scalar("SELECT COUNT(*) FROM trade_cal")
        min_d = db.query_scalar("SELECT MIN(cal_date) FROM trade_cal")
        max_d = db.query_scalar("SELECT MAX(cal_date) FROM trade_cal")
        is_open = db.query_scalar("SELECT COUNT(*) FROM trade_cal WHERE is_open::int = 1")

        if cnt:
            issue("OK", "trade_cal", "交易日历", f"{cnt:,} 天 ({min_d} ~ {max_d}), {is_open:,} 交易日")
        else:
            issue("WARN", "trade_cal", "交易日历", "无数据")
    except Exception as e:
        issue("WARN", "trade_cal", "查询失败", str(e))


# ═══════════════════════════════════════════════════════════════════════════
# 7. CSV 文件模式检查（保留原有功能）
# ═══════════════════════════════════════════════════════════════════════════

DAILY_REQUIRED_COLS = {"symbol", "datetime", "open", "high", "low", "close", "volume", "amount"}


def check_csv_files(quick: bool = False, symbol_filter: str = None):
    """CSV 文件级完整性检查"""
    print("\n── [CSV模式] 文件检查 ──")

    # 目录存在性
    for name, d in [("daily", DAILY_DIR), ("meta", META_DIR), ("macro", MACRO_DIR),
                     ("index", INDEX_DIR), ("fundamental", FUND_DIR)]:
        if d.exists():
            cnt = len(list(d.glob("*.csv")))
            issue("OK", "csv-exist", f"{name}/", f"{cnt} 文件")
        else:
            issue("ERR", "csv-exist", name, "目录不存在")

    # Daily schema
    files = sorted(DAILY_DIR.glob("*.csv"))
    if symbol_filter:
        files = [f for f in files if f.stem == symbol_filter]
    if quick:
        sample = max(10, int(len(files) * 0.05))
        files = random.sample(files, min(sample, len(files)))
        print(f"  (抽查 {len(files)} 个文件)")

    bad_schema = 0
    for f in files:
        try:
            df = pd.read_csv(f, nrows=1)
            missing = DAILY_REQUIRED_COLS - set(df.columns)
            if missing:
                issue("ERR", "csv-schema", f.name, f"缺列: {missing}")
                bad_schema += 1
        except Exception as e:
            issue("ERR", "csv-schema", f.name, str(e))
            bad_schema += 1
    if bad_schema == 0 and files:
        issue("OK", "csv-schema", "daily/", f"{len(files)} 文件 Schema 正确")

    # Fundamental
    if FUND_DIR.exists():
        for sub in sorted(FUND_DIR.iterdir()):
            if not sub.is_dir():
                continue
            csvs = sorted(sub.glob("*.csv"))
            if csvs:
                n = sum(1 for _ in open(csvs[-1])) - 1
                issue("OK", "csv-fund", f"{sub.name} ({len(csvs)} 期)", f"最新 {n:,} 行")


# ═══════════════════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="stock_data 数据库级完整性校验")
    parser.add_argument("--quick", action="store_true", help="快速抽查")
    parser.add_argument("--symbol", help="只检查指定标的")
    parser.add_argument("--source", choices=["db", "csv", "both"], default="db",
                        help="校验数据源: db(默认), csv, both")
    parser.add_argument("--expected-start", type=int, default=2006,
                        help="预期数据起始年份 (默认2006)")
    parser.add_argument("--stale-days", type=int, default=7,
                        help="陈旧判定阈值：超过 N 天未更新视为陈旧 (默认7，港股额外放宽2个交易日)")
    parser.add_argument("--skip-ohlc", action="store_true", help="跳过 OHLC 检查")
    parser.add_argument("--skip-financial", action="store_true", help="跳过财报连续性检查")
    parser.add_argument("--report", help="输出 JSON 报告文件")
    parser.add_argument("--missing-report", help="输出缺失数据明细 JSON，供 fetch_and_backup.py --from-report 使用")
    args = parser.parse_args()

    print(f"{'='*60}")
    print(f"stock_data 数据完整性校验")
    print(f"时间: {TODAY}")
    print(f"模式: {args.source}" + (" (快速)" if args.quick else ""))
    print(f"预期覆盖: {args.expected_start} ~ {TODAY}")
    if args.symbol:
        print(f"标的: {args.symbol}")
    print(f"{'='*60}")

    # ── DB 模式 ──
    if args.source in ("db", "both"):
        db = PostgresDB()
        if not db.available:
            print("\n❌ PostgreSQL 不可用，无法执行数据库检查")
            if args.source == "db":
                sys.exit(1)
        else:
            check_table_ranges(db, args.expected_start, stale_days=args.stale_days)
            check_freshness_overview(db)
            check_daily_per_stock(db, args.expected_start, quick=args.quick,
                                  symbol=args.symbol, stale_days=args.stale_days)
            if not args.skip_ohlc:
                check_ohlc_quality(db)
            check_daily_freshness(db, quick=args.quick)

            if not args.skip_financial and not args.quick:
                check_financial_continuity(db, args.expected_start)

            check_trade_cal(db, args.expected_start)

    # ── CSV 模式 ──
    if args.source in ("csv", "both"):
        check_csv_files(quick=args.quick, symbol_filter=args.symbol)

    # ── 摘要 ──
    elapsed = time.time() - start_time
    errs = sum(1 for i in all_issues if i["level"] == "ERR")
    warns = sum(1 for i in all_issues if i["level"] == "WARN")
    oks = sum(1 for i in all_issues if i["level"] == "OK")

    print(f"\n{'='*60}")
    print(f"校验完成  |  耗时 {elapsed:.1f}s  |  {ERR} {errs} 错误  {WARN} {warns} 警告  {OK} {oks} 通过")
    print(f"{'='*60}")

    # ── JSON 报告 ──
    if args.report:
        report = {
            "repo": str(REPO),
            "date": str(TODAY),
            "mode": args.source,
            "expected_start": args.expected_start,
            "elapsed_seconds": round(elapsed, 1),
            "errors": errs,
            "warnings": warns,
            "passed": oks,
            "issues": all_issues,
        }
        with open(args.report, "w") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"报告已保存: {args.report}")

    # ── 缺失明细报告 ──
    if args.missing_report:
        missing_data["generated_at"] = datetime.now().isoformat()
        missing_data["summary"] = {
            "total_stocks": _daily_stats["total"],
            "ok_daily": _daily_stats["ok"],
            "stale_daily": len(missing_data["daily_gaps"]),
            "no_data_stocks": len(missing_data["no_data_stocks"]),
            "table_gaps": len(missing_data["table_gaps"]),
            "financial_issues": {
                tbl: len(gaps) for tbl, gaps in missing_data["financial_gaps"].items()
            },
            "total_errors": errs,
            "total_warnings": warns,
        }
        with open(args.missing_report, "w") as f:
            json.dump(missing_data, f, ensure_ascii=False, indent=2)
        print(f"缺失明细已保存: {args.missing_report}")
        print(f"  日线缺失: {len(missing_data['daily_gaps'])} 只")
        print(f"  无日线数据: {len(missing_data['no_data_stocks'])} 只")
        print(f"  表级落后: {len(missing_data['table_gaps'])} 个表")
        if missing_data['financial_gaps']:
            for tbl, gaps in missing_data['financial_gaps'].items():
                severe = [g for g in gaps if g.get('severity') == 'missing']
                print(f"  {tbl} 缺失: {len(gaps)} 只 (含 {len(severe)} 只缺整年)")

    sys.exit(1 if errs > 0 else 0)

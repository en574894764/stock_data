#!/usr/bin/env python3
"""财报数据回补 — AKShare 补全利润表/资产负债表/现金流量表

用法:
  python backfill_financial.py                    # 自动补全缺口（3表）
  python backfill_financial.py --table income     # 只补利润表
  python backfill_financial.py --year 2025        # 只补特定年份
  python backfill_financial.py --dry-run          # 预览
  python backfill_financial.py --symbol 600519.SH # 单只股票
"""

from __future__ import annotations

import argparse, sys, time
from datetime import date

import akshare as ak
import psycopg2

DB = dict(host="/tmp", dbname="investassist", user="james", connect_timeout=10)
MONTH_TO_TYPE = {3: 1, 6: 2, 9: 3, 12: 4}


def get_conn():
    c = psycopg2.connect(**DB)
    c.autocommit = True
    return c


def _n(val):
    if val is None or (isinstance(val, float) and val != val):
        return None
    if isinstance(val, (int, float)):
        return float(val) if val else None
    s = str(val).replace(",", "").replace("亿", "e8").replace("万", "e4")
    try: return float(s)
    except: return None


def _r(row, key):
    """解析报告期 → (report_year, report_type, report_date)"""
    rpt = str(row.get(key, ""))
    if len(rpt) < 10:
        return None, None, None
    y = int(rpt[:4])
    m = int(rpt[5:7])
    t = MONTH_TO_TYPE.get(m, 4)
    return y, t, rpt[:10]


# ═══════════════════════════════════════════════════════════════════════════
# 数据源: AKShare 东方财富
# ═══════════════════════════════════════════════════════════════════════════

def pull_income(conn, symbol: str, year: int) -> int:
    """利润表 — stock_yjbb_em"""
    code = symbol.split(".")[0]
    try:
        df = ak.stock_yjbb_em(date=f"{year}1231")
        if df is None or df.empty: return 0
        df = df[df["股票代码"] == code]
        if df.empty: return 0

        cur = conn.cursor(); new = 0
        for _, row in df.iterrows():
            yr, qt, rd = _r(row, "报告期")
            if not yr: continue
            try:
                cur.execute("""
                    INSERT INTO income (ts_code, report_year, report_type, report_date,
                        revenue, operating_cost, operating_profit, net_profit, total_profit, basic_eps)
                    VALUES (%s,%s,%s,%s, %s,%s,%s,%s,%s,%s)
                    ON CONFLICT (ts_code, report_year, report_type) DO UPDATE
                    SET revenue=EXCLUDED.revenue, operating_cost=EXCLUDED.operating_cost,
                        operating_profit=EXCLUDED.operating_profit, net_profit=EXCLUDED.net_profit
                """, (symbol, yr, qt, rd,
                      _n(row.get("营业总收入")), _n(row.get("营业总成本")) or _n(row.get("营业成本")),
                      _n(row.get("营业利润")), _n(row.get("净利润")),
                      _n(row.get("利润总额")), _n(row.get("基本每股收益"))))
                new += cur.rowcount
            except: pass
        cur.close(); return new
    except Exception as e:
        print(f"  {symbol} 利润表: {e}"); return 0


def pull_balance(conn, symbol: str, year: int) -> int:
    """资产负债表 — stock_zcfzb_em"""
    code = symbol.split(".")[0]
    try:
        df = ak.stock_zcfzb_em(date=f"{year}1231")
        if df is None or df.empty: return 0
        df = df[df["股票代码"] == code]
        if df.empty: return 0

        cur = conn.cursor(); new = 0
        for _, row in df.iterrows():
            yr, qt, rd = _r(row, "报告期")
            if not yr: continue
            try:
                cur.execute("""
                    INSERT INTO balance_sheet (ts_code, report_year, report_type, report_date,
                        total_assets, total_liab, total_hldr_eqy_exc_min_int,
                        current_assets, current_liab, cash_and_equivalents)
                    VALUES (%s,%s,%s,%s, %s,%s,%s, %s,%s,%s)
                    ON CONFLICT (ts_code, report_year, report_type) DO UPDATE
                    SET total_assets=EXCLUDED.total_assets, total_liab=EXCLUDED.total_liab,
                        total_hldr_eqy_exc_min_int=EXCLUDED.total_hldr_eqy_exc_min_int
                """, (symbol, yr, qt, rd,
                      _n(row.get("资产总计")), _n(row.get("负债合计")),
                      _n(row.get("股东权益合计")),
                      _n(row.get("流动资产合计")), _n(row.get("流动负债合计")),
                      _n(row.get("货币资金"))))
                new += cur.rowcount
            except: pass
        cur.close(); return new
    except Exception as e:
        print(f"  {symbol} 负债表: {e}"); return 0


def pull_cashflow(conn, symbol: str, year: int) -> int:
    """现金流量表 — stock_xjll_em"""
    code = symbol.split(".")[0]
    try:
        df = ak.stock_xjll_em(date=f"{year}1231")
        if df is None or df.empty: return 0
        df = df[df["股票代码"] == code]
        if df.empty: return 0

        cur = conn.cursor(); new = 0
        for _, row in df.iterrows():
            yr, qt, rd = _r(row, "报告期")
            if not yr: continue
            try:
                cur.execute("""
                    INSERT INTO cashflow (ts_code, report_year, report_type, report_date,
                        n_cashflow_act, c_fr_sale_sg, n_cashflow_inv_act, n_cashflow_fin_act)
                    VALUES (%s,%s,%s,%s, %s,%s,%s,%s)
                    ON CONFLICT (ts_code, report_year, report_type) DO UPDATE
                    SET n_cashflow_act=EXCLUDED.n_cashflow_act,
                        c_fr_sale_sg=EXCLUDED.c_fr_sale_sg
                """, (symbol, yr, qt, rd,
                      _n(row.get("经营现金流量净额")), _n(row.get("销售商品提供劳务收到的现金")),
                      _n(row.get("投资现金流量净额")), _n(row.get("筹资现金流量净额"))))
                new += cur.rowcount
            except: pass
        cur.close(); return new
    except Exception as e:
        print(f"  {symbol} 现金流: {e}"); return 0


PULLERS = {"income": pull_income, "balance_sheet": pull_balance, "cashflow": pull_cashflow}

# ═══════════════════════════════════════════════════════════════════════════
# 缺口发现
# ═══════════════════════════════════════════════════════════════════════════

def find_gaps(conn, table: str, year: int = None) -> list[tuple]:
    cur = conn.cursor()
    if year is None:
        year = date.today().year

    cur.execute(f"""
        WITH active_stocks AS (
            SELECT s.ts_code, s.name, EXTRACT(YEAR FROM s.list_date)::int as ly
            FROM stocks s
            WHERE s.exchange IN ('SSE','SZSE','BSE') AND s.delist_date IS NULL
        ),
        years AS (SELECT generate_series(2020, {year - 1}) as yr),
        expected AS (
            SELECT a.ts_code, a.name, y.yr as report_year,
                   q.qt::varchar as report_type
            FROM active_stocks a
            CROSS JOIN years y
            CROSS JOIN (SELECT 1 as qt UNION SELECT 2 UNION SELECT 3 UNION SELECT 4) q
            WHERE y.yr >= a.ly
        ),
        missing AS (
            SELECT e.* FROM expected e
            LEFT JOIN {table} t ON e.ts_code = t.ts_code
                AND e.report_year = t.report_year AND e.report_type = t.report_type
            WHERE t.ts_code IS NULL
            LIMIT 5000
        )
        SELECT DISTINCT ts_code, name FROM missing
    """)

    rows = cur.fetchall()
    cur.close()
    return rows


# ═══════════════════════════════════════════════════════════════════════════

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--year", type=int)
    p.add_argument("--symbol")
    p.add_argument("--max", type=int, default=500)
    p.add_argument("--table", choices=["income", "balance_sheet", "cashflow", "all"], default="all")
    args = p.parse_args()

    conn = get_conn()
    tables = ["income", "balance_sheet", "cashflow"] if args.table == "all" else [args.table]

    for tbl in tables:
        symbols = find_gaps(conn, tbl, args.year)
        if args.symbol:
            symbols = [s for s in symbols if s[0] == args.symbol]

        name = tbl.replace("_", " ")
        print(f"\n{name}: {len(symbols)} 只有缺口")

        if args.dry_run:
            for s in symbols[:10]:
                print(f"  {s[0]} {s[1]}")
            continue

        puller = PULLERS[tbl]
        total = 0
        for i, (sym, name) in enumerate(symbols[:args.max]):
            if i % 50 == 0:
                print(f"[{i+1}/{min(len(symbols), args.max)}] {name}...")
            new = puller(conn, sym, 2025)
            total += new
            time.sleep(0.3)

        print(f"{name}: 完成, +{total} 行")

    conn.close()


if __name__ == "__main__":
    main()

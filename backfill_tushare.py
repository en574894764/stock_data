#!/usr/bin/env python3
"""Tushare 财经数据回补 — income/balance_sheet/cashflow/financial_indicator + 估值

用法:
  python backfill_tushare.py               # 全部回补 (2025-2026)
  python backfill_tushare.py --dry-run     # 预览
  python backfill_tushare.py --year 2025   # 只补特定年份
"""

from __future__ import annotations

import argparse, os, sys, time
from datetime import date

import psycopg2
import tushare as ts

TOKEN = os.environ.get("TUSHARE_TOKEN", "72826744b6a3733e61cd602f4fd42fe56a6de0d5781ba77e0bfb929b")
DB = dict(host="/tmp", dbname="investassist", user="james", connect_timeout=5)

pro = ts.pro_api(TOKEN)
MONTH_TYPE = {3: "1", 6: "2", 9: "3", 12: "4"}


def _n(v):
    if v is None: return None
    try: return float(v)
    except: return None


def _get_stocks():
    c = psycopg2.connect(**DB)
    cur = c.cursor()
    cur.execute("SELECT ts_code FROM stocks WHERE exchange IN ('SSE','SZSE','BSE') AND delist_date IS NULL")
    rows = [r[0] for r in cur.fetchall()]
    cur.close(); c.close()
    return rows


# ═══════════════════════════════════════════════════════════════════

def pull_financial_data(stocks: list[str], year: int, dry_run: bool = False):
    """拉取 income/balance_sheet/cashflow/financial_indicator"""
    conn = psycopg2.connect(**DB)
    conn.autocommit = True
    s = f"{year}0101"
    e = f"{year+1 if year < 2026 else 2026}0630"

    # 统计当前已有数据（避免重复拉取）
    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(*) FROM income WHERE report_year >= {year} AND report_year <= 2026")
    income_before = cur.fetchone()[0]
    cur.execute(f"SELECT COUNT(*) FROM financial_indicator WHERE report_year >= {year}")
    ind_before = cur.fetchone()[0]
    cur.close()

    total = len(stocks)
    new_income = new_balance = new_cf = new_ind = 0

    for i, ts_code in enumerate(stocks):
        if i % 100 == 0:
            print(f"[{i+1}/{total}] {ts_code}... (income +{new_income}, bal +{new_balance}, cf +{new_cf}, ind +{new_ind})")
        
        if dry_run:
            continue

        try:
            # 1. income
            df_i = pro.income(ts_code=ts_code, start_date=s, end_date=e)
            if df_i is not None and not df_i.empty:
                cur = conn.cursor()
                for _, r in df_i.iterrows():
                    ed = str(r.get("end_date", ""))
                    if len(ed) < 8: continue
                    yr = int(ed[:4]); m = int(ed[4:6])
                    rt = MONTH_TYPE.get(m, "4")
                    try:
                        cur.execute("""
                            INSERT INTO income (ts_code, report_year, report_type, report_date,
                                revenue, operating_cost, net_profit, basic_eps, total_profit)
                            VALUES (%s,%s,%s,%s, %s,%s,%s,%s,%s)
                            ON CONFLICT (ts_code, report_year, report_type) DO UPDATE
                            SET revenue=EXCLUDED.revenue, net_profit=EXCLUDED.net_profit
                        """, (ts_code, yr, rt, ed[:4]+"-"+ed[4:6]+"-"+ed[6:8],
                              _n(r.get("revenue")), _n(r.get("oper_cost")),
                              _n(r.get("n_income")), _n(r.get("basic_eps")),
                              _n(r.get("total_profit"))))
                        new_income += cur.rowcount
                    except: pass
                cur.close()

            # 2. balance_sheet
            df_b = pro.balancesheet(ts_code=ts_code, start_date=s, end_date=e)
            if df_b is not None and not df_b.empty:
                cur = conn.cursor()
                for _, r in df_b.iterrows():
                    ed = str(r.get("end_date", ""))
                    if len(ed) < 8: continue
                    yr = int(ed[:4]); m = int(ed[4:6])
                    rt = MONTH_TYPE.get(m, "4")
                    try:
                        cur.execute("""
                            INSERT INTO balance_sheet (ts_code, report_year, report_type, report_date,
                                total_assets, total_liab, total_hldr_eqy_exc_min_int, current_assets, current_liab)
                            VALUES (%s,%s,%s,%s, %s,%s,%s,%s,%s)
                            ON CONFLICT (ts_code, report_year, report_type) DO UPDATE
                            SET total_assets=EXCLUDED.total_assets, total_liab=EXCLUDED.total_liab
                        """, (ts_code, yr, rt, ed[:4]+"-"+ed[4:6]+"-"+ed[6:8],
                              _n(r.get("total_assets")), _n(r.get("total_liab")),
                              _n(r.get("total_hldr_eqy_exc_min_int")),
                              _n(r.get("total_cur_assets")), _n(r.get("total_cur_liab"))))
                        new_balance += cur.rowcount
                    except: pass
                cur.close()

            # 3. cashflow
            df_c = pro.cashflow(ts_code=ts_code, start_date=s, end_date=e)
            if df_c is not None and not df_c.empty:
                cur = conn.cursor()
                for _, r in df_c.iterrows():
                    ed = str(r.get("end_date", ""))
                    if len(ed) < 8: continue
                    yr = int(ed[:4]); m = int(ed[4:6])
                    rt = MONTH_TYPE.get(m, "4")
                    try:
                        cur.execute("""
                            INSERT INTO cashflow (ts_code, report_year, report_type, report_date,
                                n_cashflow_act, c_fr_sale_sg, n_cashflow_inv_act, n_cashflow_fin_act)
                            VALUES (%s,%s,%s,%s, %s,%s,%s,%s)
                            ON CONFLICT (ts_code, report_year, report_type) DO UPDATE
                            SET n_cashflow_act=EXCLUDED.n_cashflow_act
                        """, (ts_code, yr, rt, ed[:4]+"-"+ed[4:6]+"-"+ed[6:8],
                              _n(r.get("n_cashflow_act")), _n(r.get("c_fr_sale_sg")),
                              _n(r.get("n_cashflow_inv_act")), _n(r.get("n_cashflow_fin_act"))))
                        new_cf += cur.rowcount
                    except: pass
                cur.close()

            # 4. financial_indicator
            df_f = pro.fina_indicator(ts_code=ts_code, start_date=s, end_date=e)
            if df_f is not None and not df_f.empty:
                cur = conn.cursor()
                for _, r in df_f.iterrows():
                    ed = str(r.get("end_date", ""))
                    if len(ed) < 8: continue
                    yr = int(ed[:4]); m = int(ed[4:6])
                    rt = MONTH_TYPE.get(m, "4")
                    try:
                        cur.execute("""
                            INSERT INTO financial_indicator (ts_code, report_year, report_type,
                                roe, roe_dt, roa, grossprofit_margin, netprofit_yoy,
                                debt_to_assets, current_ratio, quick_ratio, bps)
                            VALUES (%s,%s,%s, %s,%s,%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT (ts_code, report_year, report_type) DO UPDATE
                            SET roe=EXCLUDED.roe, grossprofit_margin=EXCLUDED.grossprofit_margin,
                                debt_to_assets=EXCLUDED.debt_to_assets
                        """, (ts_code, yr, rt,
                              _n(r.get("roe")), _n(r.get("roe_dt")), _n(r.get("roa")),
                              _n(r.get("grossprofit_margin")), _n(r.get("netprofit_yoy")),
                              _n(r.get("debt_to_assets")), _n(r.get("current_ratio")),
                              _n(r.get("quick_ratio")), _n(r.get("bps"))))
                        new_ind += cur.rowcount
                    except: pass
                cur.close()

        except Exception as e:
            if "每分钟最多访问" in str(e):
                print(f"  Tushare 限流，等待 60s...")
                time.sleep(60)
                continue
            # 其他错误静默跳过

        time.sleep(0.3)  # Tushare 免费版限流

    conn.close()
    return new_income, new_balance, new_cf, new_ind


def run_valuation(year: int):
    """跑 InvestAssist 估值计算"""
    print(f"计算 {year} 年估值...")
    try:
        sys.path.insert(0, "/Users/james/WorkBuddy/InvestAssist")
        from scripts.valuation.produce_valuation_data_final import main as valuation_main
        valuation_main()
        print("估值计算完成")
    except Exception as e:
        print(f"估值计算失败: {e}")
        # 降级：直接 SQL 插入 2026 估值（简化版）
        fallback_valuation(year)


def fallback_valuation(year: int):
    """简易估值：net_profit * 30 / total_shares"""
    conn = psycopg2.connect(**DB)
    conn.autocommit = True
    cur = conn.cursor()

    cur.execute(f"""
        INSERT INTO stock_valuation (ts_code, valuation_year, name,
            net_profit, net_profit_year, fair_value_3y)
        SELECT i.ts_code, {year}, s.name,
            MAX(CASE WHEN i.report_type='4' AND i.report_year={year-1} THEN i.net_profit END) as net_profit,
            {year-1},
            MAX(CASE WHEN i.report_type='4' AND i.report_year={year-1} THEN i.net_profit END) * 30 as fv
        FROM income i
        JOIN stocks s ON i.ts_code = s.ts_code
        WHERE s.exchange IN ('SSE','SZSE','BSE')
        GROUP BY i.ts_code, s.name
        HAVING MAX(CASE WHEN i.report_type='4' AND i.report_year={year-1} THEN i.net_profit END) IS NOT NULL
        ON CONFLICT (ts_code, valuation_year) DO UPDATE
        SET net_profit=EXCLUDED.net_profit, fair_value_3y=EXCLUDED.fair_value_3y
    """)

    count = cur.rowcount
    cur.close(); conn.close()
    print(f"估值: +{count} 行 (year={year})")


# ═══════════════════════════════════════════════════════════════════

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--year", type=int, default=2025)
    p.add_argument("--max-stocks", type=int, default=0, help="限制股票数 (0=全部)")
    p.add_argument("--skip-valuation", action="store_true")
    args = p.parse_args()

    stocks = _get_stocks()
    if args.max_stocks > 0:
        stocks = stocks[:args.max_stocks]

    year = args.year
    print(f"Tushare 回补 | {len(stocks)} 只 A股 | year={year}")

    if args.dry_run:
        print("[dry-run] 跳过")
        return

    ni, nb, nc, nf = pull_financial_data(stocks, year, args.dry_run)
    print(f"\n回补完成: income +{ni}, balance +{nb}, cashflow +{nc}, indicator +{nf}")

    if not args.skip_valuation:
        run_valuation(2026)

    # 更新报告
    print("\n生成报告...")
    from report_builder import collect_db_stats, generate_report, build_feishu_report, push_feishu_report
    stats = collect_db_stats()
    generate_report(stats, True)
    push_feishu_report(build_feishu_report(stats, True))
    print("✅ 完成")


if __name__ == "__main__":
    main()

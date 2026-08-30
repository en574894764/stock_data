#!/usr/bin/env python3
"""财报数据回补 — tushare VIP 接口按报告期批量补全

数据源: tushare pro income_vip / balancesheet_vip / cashflow_vip / fina_indicator_vip
       按报告期一次返回全市场，字段与 DB 表对齐，远优于旧版 AKShare 摘要方案。

v2 变更 (2026-08-30 重写):
  - 修复年份硬编码 bug (旧版 puller(conn, sym, 2025) 只能拉 2025 年报)
  - 修复当年缺口不进发现范围的 bug (旧版 generate_series(2020, year-1))
  - 修复 akshare 接口改名 stock_zcfzb_em → stock_zcfz_em 导致的崩溃
  - 批量化: 按报告期拉全市场一次, 只 INSERT 缺失行 (旧版每只股票拉一次全市场)
  - 新增 fina_indicator 回补
  - 不再 except:pass 吞错

用法:
  python backfill_financial.py                     # 补去年 + 当年已披露报告期
  python backfill_financial.py --year 2025         # 指定年份四期
  python backfill_financial.py --years 2024,2025   # 多年
  python backfill_financial.py --table income      # 只补利润表
  python backfill_financial.py --symbol 600519.SH  # 单只
  python backfill_financial.py --force             # 已存在行也更新 (财报修正场景)
  python backfill_financial.py --dry-run
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date

import psycopg2
import psycopg2.extras
import tushare as ts

DB = dict(host="/tmp", dbname="investassist", user="james", connect_timeout=10)
TUSHARE_TOKEN = os.environ.get("TUSHARE_TOKEN", "")

# 报告期月份 → DB report_type (1:Q1, 2:H1, 3:Q3, 4:FY)
MONTH_TO_TYPE = {3: 1, 6: 2, 9: 3, 12: 4}
PERIOD_MD = {1: "0331", 2: "0630", 3: "0930", 4: "1231"}


# 披露截止日 (报告期 → 最晚披露日)；用于判断"应该披露完毕"
def disclosure_deadline(year: int, q: int) -> date:
    return {1: date(year, 4, 30), 2: date(year, 8, 31),
            3: date(year, 10, 31), 4: date(year + 1, 4, 30)}[q]


# 可拉取日 (报告期结束 → 披露期内增量可拉；早于此日期接口也无数据)
def fetch_available_from(year: int, q: int) -> date:
    return {1: date(year, 4, 15), 2: date(year, 7, 15),
            3: date(year, 10, 15), 4: date(year + 1, 1, 15)}[q]


def get_conn():
    c = psycopg2.connect(**DB)
    c.autocommit = True
    return c


def _f(val):
    """安全转 float；无法解析返回 None"""
    if val is None or (isinstance(val, float) and val != val):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _pick(row: dict, *names):
    """从 tushare 行里按候选列名取第一个非空值（兼容新旧字段名）"""
    for n in names:
        v = row.get(n)
        if v is not None:
            return v
    return None


# ── 表定义: (表名, tushare VIP 接口, DB 数据列及候选 tushare 列名) ──────────

TABLES: dict[str, dict] = {
    "income": {
        "api": "income_vip",
        "cols": {
            "ann_date": ["ann_date"],
            "f_ann_date": ["f_ann_date"],
            "total_revenue": ["total_revenue"],
            "revenue": ["revenue"],
            "total_cogs": ["total_cogs"],
            "oper_cost": ["oper_cost"],
            "sell_exp": ["sell_exp"],
            "admin_exp": ["admin_exp"],
            "fin_exp": ["fin_exp"],
            "operate_profit": ["operate_profit"],
            "total_profit": ["total_profit"],
            "income_tax": ["income_tax"],
            "n_income": ["n_income"],
            "n_income_attr_p": ["n_income_attr_p"],
            "basic_eps": ["basic_eps"],
        },
    },
    "balance_sheet": {
        "api": "balancesheet_vip",
        "cols": {
            "ann_date": ["ann_date"],
            "f_ann_date": ["f_ann_date"],
            "total_assets": ["total_assets"],
            "total_cur_assets": ["total_cur_assets"],
            "total_nca": ["total_nca"],
            "money_cap": ["money_cap"],
            "accounts_receiv": ["accounts_receiv"],
            "inventories": ["inventories"],
            "fix_assets": ["fix_assets"],
            "total_liab": ["total_liab"],
            "total_cur_liab": ["total_cur_liab"],
            "total_ncl": ["total_ncl"],
            "accounts_pay": ["accounts_pay"],
            "total_hldr_eqy_exc_min_int": ["total_hldr_eqy_exc_min_int"],
            "total_hldr_eqy_inc_min_int": ["total_hldr_eqy_inc_min_int"],
        },
    },
    "cashflow": {
        "api": "cashflow_vip",
        "cols": {
            "ann_date": ["ann_date"],
            "f_ann_date": ["f_ann_date"],
            "n_cashflow_act": ["n_cashflow_act"],
            "cash_recp_sg_and_rs": ["cash_recp_sg_and_rs", "c_fr_sale_sg"],
            "n_incr_cash_cash_equ": ["n_incr_cash_cash_equ"],
            "n_cashflow_inv_act": ["n_cashflow_inv_act"],
            "cash_recp_disp_withdrw_invest": ["cash_recp_disp_withdrw_invest", "c_recp_disp_withdrw_invest"],
            "n_cash_flows_fnc_act": ["n_cash_flows_fnc_act", "c_cash_flows_fnc_act"],
            "cash_recp_cap_contrib": ["cash_recp_cap_contrib", "c_recp_cap_contrib"],
        },
    },
    "financial_indicator": {
        "api": "fina_indicator_vip",
        "cols": {
            "ann_date": ["ann_date"],
            "roe": ["roe"],
            "roe_dt": ["roe_dt"],
            "roa": ["roa"],
            "npta": ["npta"],
            "grossprofit_margin": ["grossprofit_margin"],
            "profit_dedt": ["profit_dedt"],
            "op_yoy": ["op_yoy"],
            "ebt_yoy": ["ebt_yoy"],
            "netprofit_yoy": ["netprofit_yoy"],
            "debt_to_assets": ["debt_to_assets"],
            "assets_to_eqt": ["assets_to_eqt"],
            "current_ratio": ["current_ratio"],
            "quick_ratio": ["quick_ratio"],
            "or_yoy": ["or_yoy"],
            "tr_yoy": ["tr_yoy"],
            "basic_eps": ["basic_eps", "eps"],
            "dt_eps": ["dt_eps"],
            "bps": ["bps"],
        },
    },
}


def _monthly_windows(period: str, today: date) -> list[tuple[str, str]]:
    """报告期之后按自然月切 ann_date 窗口（用于翻页绕过接口单次行数上限）"""
    y, m = int(period[:4]), int(period[4:6])
    # 窗口从报告期次月开始，到 min(今天, 披露截止)
    end_bound = disclosure_deadline(y, MONTH_TO_TYPE[m])
    if today < end_bound:
        end_bound = today
    out = []
    cy, cm = (y, m + 1) if m < 12 else (y + 1, 1)
    while date(cy, cm, 1) <= end_bound:
        if cm == 12:
            nxt = date(cy + 1, 1, 1)
        else:
            nxt = date(cy, cm + 1, 1)
        w_end = min(nxt - __import__("datetime").timedelta(days=1), end_bound)
        out.append((f"{cy}{cm:02d}01", w_end.strftime("%Y%m%d")))
        cy, cm = (cy + 1, 1) if cm == 12 else (cy, cm + 1)
    return out


def fetch_period(pro, table: str, period: str, today: date) -> list[dict]:
    """拉取某报告期全市场财报，过滤出标准合并报表行，返回 dicts。

    部分 VIP 接口有单次行数上限 (cashflow_vip=6400)，命中上限时按
    ann_date 月度窗口翻页合并，去重后返回完整数据。
    """
    import pandas as pd
    api = TABLES[table]["api"]
    df = getattr(pro, api)(period=period)
    if df is None or df.empty:
        return []

    if len(df) >= 6400:  # 接口单次上限特征值
        chunks = [df]
        for s, e in _monthly_windows(period, today):
            try:
                c = getattr(pro, api)(period=period, start_date=s, end_date=e)
            except Exception:
                break  # 接口不支持窗口参数，放弃翻页
            if c is not None and not c.empty:
                chunks.append(c)
        if len(chunks) > 1:
            merged = pd.concat(chunks, ignore_index=True)
            dedup = ["ts_code"] + (["report_type"] if "report_type" in merged.columns else [])
            df = merged.drop_duplicates(subset=dedup, keep="first")

    rows = df.to_dict("records")
    # 过滤北交所重复挂牌变体代码 (如 833243!1.BJ, 11字符超 varchar(10)，主代码已包含同数据)
    rows = [r for r in rows if "!" not in str(r.get("ts_code", ""))]
    # income/balancesheet/cashflow 有 report_type: '1'=合并报表(标准);
    # fina_indicator 无该列
    if "report_type" in df.columns:
        rows = [r for r in rows if str(r.get("report_type")) == "1"]
    return rows


def upsert_table(conn, table: str, rows: list[dict], year: int, q: int,
                 symbol: str | None, force: bool, dry_run: bool) -> tuple[int, int]:
    """INSERT 缺失行 (默认) / force 时 UPDATE 已有行。返回 (inserted, updated)"""
    cols = TABLES[table]["cols"]
    cur = conn.cursor()

    # 该期已存在的标的集合 (report_year/report_type 为 varchar)
    cur.execute(
        f"SELECT ts_code FROM {table} WHERE report_year=%s AND report_type=%s",
        (str(year), str(q)))
    existing = {r[0] for r in cur.fetchall()}

    insert_rows, update_rows = [], []
    for r in rows:
        ts_code = r.get("ts_code")
        if not ts_code:
            continue
        if symbol and ts_code != symbol:
            continue
        # end_date 月份校验 (防止接口返回相邻期数据)
        end_date = str(r.get("end_date", ""))
        if len(end_date) >= 6 and int(end_date[4:6]) not in MONTH_TO_TYPE:
            continue

        values = {db_col: _pick(r, *cands) for db_col, cands in cols.items()}
        # 数值列转 float
        for db_col in values:
            if db_col not in ("ann_date", "f_ann_date"):
                values[db_col] = _f(values[db_col])

        if ts_code in existing:
            if force:
                update_rows.append((ts_code, values))
            continue
        insert_rows.append((ts_code, values))

    if dry_run:
        cur.close()
        return len(insert_rows), len(update_rows)

    db_cols = list(cols.keys())

    # 批量 INSERT
    if insert_rows:
        sql = (f"INSERT INTO {table} (ts_code, report_year, report_type, "
               f"{', '.join(db_cols)}) VALUES %s "
               f"ON CONFLICT (ts_code, report_year, report_type) DO NOTHING")
        data = [tuple([ts, str(year), str(q)] + [v[db_col] for db_col in db_cols])
                for ts, v in insert_rows]
        try:
            psycopg2.extras.execute_values(cur, sql, data, page_size=1000)
        except Exception as e:
            print(f"    ❌ {table} {year}Q{q} 批量插入失败: {e}")
            raise
        cur.close()
        return len(insert_rows), 0

    # force: 逐行 upsert 财务字段（COALESCE 保留已有非空值，不动 ann_date）
    if update_rows:
        numeric_cols = [c for c in db_cols if c not in ("ann_date", "f_ann_date")]
        sets = ", ".join(f"{c} = COALESCE(EXCLUDED.{c}, {table}.{c})"
                         for c in numeric_cols)
        upd_ok = 0
        for ts_code, values in update_rows:
            try:
                cur.execute(
                    f"INSERT INTO {table} (ts_code, report_year, report_type, "
                    f"{', '.join(db_cols)}) VALUES %s "
                    f"ON CONFLICT (ts_code, report_year, report_type) "
                    f"DO UPDATE SET {sets}",
                    (tuple([ts_code, str(year), str(q)] + [values[c] for c in db_cols]),))
                upd_ok += 1
            except Exception as e:
                print(f"    ⚠️ {table} {ts_code} {year}Q{q} 更新失败: {e}")
                conn.rollback()
                continue
        cur.close()
        return 0, upd_ok

    cur.close()
    return 0, 0


def periods_to_process(years: list[int], today: date) -> list[tuple[int, int]]:
    """生成年份×季度组合，只含已进入披露期的 (含披露期内增量)"""
    out = []
    for y in years:
        for q in (1, 2, 3, 4):
            if today >= fetch_available_from(y, q):
                tag = "" if today >= disclosure_deadline(y, q) else " (披露期内, 增量)"
                out.append((y, q))
                if tag:
                    print(f"  (注意 {y}Q{q}: 披露截止 {disclosure_deadline(y, q)} 未到, 拉已披露部分{tag})")
            else:
                print(f"  (跳过 {y}Q{q}: 未到可拉取日 {fetch_available_from(y, q)})")
    return out


def main():
    p = argparse.ArgumentParser(description="财报回补 v2 (tushare VIP 按期批量)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--year", type=int, help="指定年份（四期）")
    p.add_argument("--years", type=str, help="逗号分隔多年, 如 2024,2025")
    p.add_argument("--symbol", help="只处理单只股票")
    p.add_argument("--force", action="store_true", help="已存在行也更新财务字段")
    p.add_argument("--table",
                   choices=["income", "balance_sheet", "cashflow",
                            "financial_indicator", "all"],
                   default="all")
    args = p.parse_args()

    if not TUSHARE_TOKEN:
        print("❌ 未设置 TUSHARE_TOKEN 环境变量")
        return 1

    today = date.today()
    if args.year:
        years = [args.year]
    elif args.years:
        years = [int(y) for y in args.years.split(",")]
    else:
        years = [today.year - 1, today.year]

    periods = periods_to_process(years, today)
    if not periods:
        print("无可处理的报告期")
        return 0

    tables = list(TABLES.keys()) if args.table == "all" else [args.table]
    pro = ts.pro_api(TUSHARE_TOKEN)
    conn = get_conn()

    total_ins = total_upd = 0
    for tbl in tables:
        print(f"\n=== {tbl} ===")
        for y, q in periods:
            period = f"{y}{PERIOD_MD[q]}"
            try:
                rows = fetch_period(pro, tbl, period, today)
            except Exception as e:
                print(f"  ❌ {period} 拉取失败: {str(e)[:150]}")
                continue
            if not rows:
                print(f"  {period}: 接口无数据")
                continue
            ins, upd = upsert_table(conn, tbl, rows, y, q, args.symbol,
                                    args.force, args.dry_run)
            total_ins += ins
            total_upd += upd
            mode = " [dry-run]" if args.dry_run else ""
            print(f"  {period}: 拉到 {len(rows)} 行 → 插入 {ins}"
                  f"{f', 更新 {upd}' if upd else ''}{mode}")
            time.sleep(1)  # VIP 接口限频保护

    conn.close()
    print(f"\n{'✅' if not args.dry_run else '[dry-run] ✅'} 完成: "
          f"共插入 {total_ins} 行{f', 更新 {total_upd} 行' if total_upd else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

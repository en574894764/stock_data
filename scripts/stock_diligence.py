#!/usr/bin/env python3
"""A股排雷 + 选股 + 估值 + 买卖点 一体化尽职调查引擎

基于 stock_data 仓库 PG 数据, 实现四层流程:
  1. 排雷(Red Flags): 财务造假/困境/现金质量红旗, 含 Beneish M-Score / Altman Z-Score
  2. 质量打分: Piotroski F-Score (9项)
  3. 估值: PE/PB 历史分位 (daily_basic 2015至今)
  4. 买卖点信号: 估值分位 + 基本面改善 交叉

口径:
  - 财务用年报(report_type='4'), 当前年=2025 年报 vs 上年=2024 年报
  - 估值用 daily_basic 最新交易日
  - 金融股(银行/保险/证券/多元金融)剔除出 M/Z-Score (高杠杆是常态, 模型不适用)

已知数据缺口(已在报告说明):
  - Beneish DEPI 折旧指数: 本地无折旧字段, 取 1.0 中性
  - Altman X2 留存收益: 本地无, 用归母股东权益近似

用法:
  python3 scripts/stock_diligence.py [--top 30] [--out outputs/stock_diligence]
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd
import psycopg2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FIN_SECTORS = ("银行", "保险", "证券", "多元金融")


def get_conn():
    return psycopg2.connect(host="/tmp", dbname="investassist", user="james")


def _annual(df, year):
    """取某财年年报行 (report_type='4'), 按 ts_code 建索引"""
    d = df[(df["report_year"] == year) & (df["report_type"] == "4")].copy()
    return d.set_index("ts_code")


def load_financials(conn):
    """加载 2024/2025 年报三表 + 财务指标, 返回按 ts_code 对齐的宽表"""
    # 利润表
    inc = pd.read_sql(
        "SELECT ts_code, report_year, report_type, total_revenue, revenue, total_cogs, "
        "sell_exp, admin_exp, fin_exp, operate_profit, n_income, n_income_attr_p "
        "FROM income WHERE report_type='4' AND report_year IN (2024, 2025)", conn)
    # 现金流
    cf = pd.read_sql(
        "SELECT ts_code, report_year, report_type, n_cashflow_act FROM cashflow "
        "WHERE report_type='4' AND report_year IN (2024, 2025)", conn)
    # 资产负债表
    bs = pd.read_sql(
        "SELECT ts_code, report_year, report_type, total_assets, total_cur_assets, "
        "money_cap, accounts_receiv, inventories, fix_assets, total_liab, "
        "total_cur_liab, total_ncl, total_hldr_eqy_exc_min_int FROM balance_sheet "
        "WHERE report_type='4' AND report_year IN (2024, 2025)", conn)
    # 财务指标
    fi = pd.read_sql(
        "SELECT ts_code, report_year, report_type, roe, roa, grossprofit_margin, "
        "current_ratio, debt_to_assets, netprofit_yoy, or_yoy FROM financial_indicator "
        "WHERE report_type='4' AND report_year IN (2024, 2025)", conn)

    cur, prv = {}, {}
    for name, df in [("income", inc), ("cashflow", cf), ("balance", bs), ("fi", fi)]:
        c = _annual(df, 2025)
        p = _annual(df, 2024)
        # 去重(同一年报多次披露取最新 ann_date 前的最后一版, 这里简单取最后出现)
        c = c[~c.index.duplicated(keep="last")]
        p = p[~p.index.duplicated(keep="last")]
        for col in c.columns:
            if col in ("report_year", "report_type"):
                continue
            cur[f"{col}"] = c[col]
            prv[f"{col}_prev"] = p[col]
    cur_df = pd.DataFrame(cur)
    prv_df = pd.DataFrame(prv)
    return cur_df.join(prv_df, how="outer")


def load_universe(conn):
    """沪深 A 股非 ST, 带名称/行业/上市日期"""
    df = pd.read_sql(
        "SELECT ts_code, name, industry, list_date, list_status FROM stocks "
        "WHERE (ts_code LIKE '%.SZ' OR ts_code LIKE '%.SH')", conn)
    df = df[~df["name"].str.contains("ST", na=False)]
    df = df[~df["name"].str.contains("退", na=False)]
    return df


def load_valuation(conn):
    """最新交易日估值 + PE/PB 历史分位 + 两个年末股本 (判断增发); 分位/股本按 last_date 缓存"""
    last = pd.read_sql("SELECT MAX(trade_date) AS d FROM daily_basic", conn)["d"].iloc[0]
    last_s = last.strftime("%Y-%m-%d")
    val = pd.read_sql(
        f"SELECT ts_code, pe_ttm, pb, ps_ttm, dv_ttm, total_mv, total_share "
        f"FROM daily_basic WHERE trade_date = '{last_s}'", conn)
    val = val.drop_duplicates(subset="ts_code", keep="last")

    cache_dir = os.path.join(ROOT, "factor_cache")
    os.makedirs(cache_dir, exist_ok=True)
    meta_f = os.path.join(cache_dir, "_val_meta.json")
    pct_f = os.path.join(cache_dir, "_val_pct.parquet")
    share_f = os.path.join(cache_dir, "_val_share.parquet")
    use_cache = False
    if os.path.exists(meta_f):
        try:
            meta = json.load(open(meta_f))
            if meta.get("last_date") == last_s and os.path.exists(pct_f) and os.path.exists(share_f):
                use_cache = True
        except Exception:
            use_cache = False

    if use_cache:
        pct = pd.read_parquet(pct_f)
        share = pd.read_parquet(share_f)
    else:
        pct = pd.read_sql(
            f"SELECT ts_code, pe_cdf, pb_cdf FROM ("
            f"  SELECT ts_code, pe_ttm, pb,"
            f"    CUME_DIST() OVER (PARTITION BY ts_code ORDER BY pe_ttm) AS pe_cdf,"
            f"    CUME_DIST() OVER (PARTITION BY ts_code ORDER BY pb) AS pb_cdf,"
            f"    trade_date"
            f"  FROM daily_basic WHERE pe_ttm IS NOT NULL AND pe_ttm > 0 AND pb IS NOT NULL AND pb > 0"
            f") t WHERE t.trade_date = '{last_s}'", conn)
        pct = pct.drop_duplicates(subset="ts_code", keep="last")
        pct.to_parquet(pct_f)
        share = pd.read_sql(
            "SELECT ts_code, total_share, trade_date FROM daily_basic WHERE trade_date IN ("
            "  (SELECT MAX(trade_date) FROM daily_basic WHERE trade_date <= '2024-12-31'),"
            "  (SELECT MAX(trade_date) FROM daily_basic WHERE trade_date <= '2025-12-31'))", conn)
        share = share.pivot_table(index="ts_code", columns="trade_date", values="total_share", aggfunc="last")
        share.columns = ["total_share_2024", "total_share_2025"]
        share.to_parquet(share_f)
        json.dump({"last_date": last_s}, open(meta_f, "w"))

    return last, val.set_index("ts_code"), pct.set_index("ts_code"), share


def calc_scores(fin, val, uni):
    """计算 F-Score / M-Score / Z-Score / 红旗, 返回 DataFrame"""
    df = fin.copy()
    eps = 1e-9

    # ---- 基础比率 ----
    ta = df["total_assets"].replace(0, np.nan)
    df["roa_"] = df["n_income_attr_p"] / ta
    df["roa_prev"] = df["n_income_attr_p_prev"] / df["total_assets_prev"].replace(0, np.nan)
    df["gross_margin_"] = df["grossprofit_margin"]  # 已为百分比
    df["gross_margin_prev"] = df["grossprofit_margin_prev"]
    df["asset_turn_"] = df["revenue"] / ta
    df["asset_turn_prev"] = df["revenue_prev"] / df["total_assets_prev"].replace(0, np.nan)
    df["long_debt_ratio_"] = df["total_ncl"] / ta
    df["long_debt_ratio_prev"] = df["total_ncl_prev"] / df["total_assets_prev"].replace(0, np.nan)

    # ---- Piotroski F-Score (9项) ----
    f = pd.Series(0, index=df.index, dtype=float)
    f += (df["roa_"] > 0).astype(float)                                          # F1 ROA>0
    f += (df["n_cashflow_act"] > 0).astype(float)                                # F2 经营现金流>0
    f += (df["roa_"] > df["roa_prev"]).astype(float)                             # F3 ROA改善
    f += (df["n_cashflow_act"] > df["n_income_attr_p"]).astype(float)            # F4 现金流>净利(应计质量)
    f += (df["long_debt_ratio_"] < df["long_debt_ratio_prev"]).astype(float)     # F5 长期负债率下降
    f += (df["current_ratio"] > df["current_ratio_prev"]).astype(float)          # F6 流动比率改善
    f += (df["total_share_2025"] <= df["total_share_2024"]).fillna(True).astype(float)  # F7 无增发
    f += (df["gross_margin_"] > df["gross_margin_prev"]).astype(float)           # F8 毛利率改善
    f += (df["asset_turn_"] > df["asset_turn_prev"]).astype(float)               # F9 周转改善
    df["f_score"] = f

    # ---- Beneish M-Score (8项, DEPI 缺折旧取 1.0) ----
    rev, revp = df["revenue"].replace(0, np.nan), df["revenue_prev"].replace(0, np.nan)
    ar, arp = df["accounts_receiv"], df["accounts_receiv_prev"]
    dsri = (ar / rev) / (arp / revp)
    gm = df["gross_margin_"].replace(0, np.nan)
    gmp = df["gross_margin_prev"].replace(0, np.nan)
    gmi = gmp / gm
    ca, cap = df["total_cur_assets"], df["total_cur_assets_prev"]
    ppe, ppep = df["fix_assets"], df["fix_assets_prev"]
    ta_c, ta_p = df["total_assets"].replace(0, np.nan), df["total_assets_prev"].replace(0, np.nan)
    aqi = (1 - (ca + ppe) / ta_c) / (1 - (cap + ppep) / ta_p)
    sgi = rev / revp
    depi = pd.Series(1.0, index=df.index)  # 缺折旧, 中性
    sga = (df["sell_exp"] + df["admin_exp"]) / rev
    sgap = (df["sell_exp_prev"] + df["admin_exp_prev"]) / revp
    sgai = sga / sgap
    cl, clp = df["total_cur_liab"], df["total_cur_liab_prev"]
    ncl, nclp = df["total_ncl"], df["total_ncl_prev"]
    lvgi = ((cl + ncl) / ta_c) / ((clp + nclp) / ta_p)
    tata = (df["n_income_attr_p"] - df["n_cashflow_act"]) / ta_c
    df["m_score"] = (-4.84 + 0.92 * dsri + 0.528 * gmi + 0.404 * aqi + 0.892 * sgi
                     + 0.115 * depi - 0.172 * sgai + 4.679 * tata - 0.327 * lvgi)

    # ---- Altman Z-Score ----
    x1 = (df["total_cur_assets"] - df["total_cur_liab"]) / ta_c
    x2 = df["total_hldr_eqy_exc_min_int"] / ta_c      # 留存收益近似为归母权益
    x3 = df["operate_profit"] / ta_c                  # EBIT 近似为营业利润
    x4 = val["total_mv"] / df["total_liab"].replace(0, np.nan)   # 市值/总负债
    x5 = rev / ta_c
    df["z_score"] = 1.2 * x1 + 1.4 * x2 + 3.3 * x3 + 0.6 * x4 + 1.0 * x5

    # ---- Red Flags (排雷红旗) ----
    flags = pd.Series(0, index=df.index, dtype=int)
    red_list = {}
    # R1 连续两年归母净利为负
    m = (df["n_income_attr_p"] < 0) & (df["n_income_attr_p_prev"] < 0)
    flags += m.astype(int); red_list["连亏两年"] = m
    # R2 资产负债率 > 70%
    m = df["debt_to_assets"] > 70
    flags += m.astype(int); red_list["负债率>70%"] = m
    # R3 流动比率 < 1
    m = df["current_ratio"] < 1
    flags += m.astype(int); red_list["流动比率<1"] = m
    # R4 经营现金流连续为负
    m = (df["n_cashflow_act"] < 0) & (df["n_cashflow_act_prev"] < 0)
    flags += m.astype(int); red_list["经营现金流连负"] = m
    # R5 纸面利润: 净利>0 但经营现金流<0
    m = (df["n_income_attr_p"] > 0) & (df["n_cashflow_act"] < 0)
    flags += m.astype(int); red_list["纸面利润"] = m
    # R6 应收增速 > 营收增速*1.5 (虚增收入)
    rev_g = (df["revenue"] - df["revenue_prev"]) / df["revenue_prev"].replace(0, np.nan)
    ar_g = (df["accounts_receiv"] - df["accounts_receiv_prev"]) / df["accounts_receiv_prev"].replace(0, np.nan)
    m = (ar_g > rev_g * 1.5) & (ar_g > 0.3)
    flags += m.astype(int); red_list["应收异常"] = m
    # R7 存贷双高 (货币资金/总资产>20% 且 长期负债>总资产*15%)
    m = (df["money_cap"] / ta_c > 0.2) & (df["total_ncl"] / ta_c > 0.15)
    flags += m.astype(int); red_list["存贷双高"] = m
    # R8 M-Score > -2.22 (造假嫌疑)
    m = df["m_score"] > -2.22
    flags += m.astype(int); red_list["M造假嫌疑"] = m
    # R9 Z-Score < 1.81 (破产风险, 仅非金融)
    m = df["z_score"] < 1.81
    flags += m.astype(int); red_list["Z破产风险"] = m
    # R10 商誉/减值暴雷(用 固定资产/总资产 异常高近似, 缺商誉字段)
    df["n_flags"] = flags
    for k, v in red_list.items():
        df[f"flag_{k}"] = v

    # 一票否决红旗 (关键项)
    df["veto"] = (
        red_list["连亏两年"] | red_list["M造假嫌疑"] | red_list["Z破产风险"]
        | red_list["经营现金流连负"]
    ).astype(bool)
    return df


def value_score(pe_cdf, pb_cdf, dv_ttm):
    """估值分位 → 5 分制价值分 (分位越低越便宜分越高)"""
    def seg(c):
        return np.select([c < 0.2, c < 0.4, c < 0.6, c < 0.8], [5, 4, 3, 2], default=1)
    sc = seg(pe_cdf) + seg(pb_cdf)
    sc += (dv_ttm > 4).astype(float)  # 股息率>4% 加 1 分
    return sc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    conn = get_conn()
    uni = load_universe(conn)
    fin = load_financials(conn)
    last_date, val, pct, share = load_valuation(conn)
    print(f"股票池: {len(uni)} 只 | 年报财务: {len(fin)} 只 | 最新交易日: {last_date}")

    # 合并
    df = uni.set_index("ts_code").join(fin, how="inner")
    df = df.join(val, how="left")
    df = df.join(pct, how="left")
    df = df.join(share, how="left")
    is_fin = df["industry"].astype(str).str.contains("|".join(FIN_SECTORS), na=False)

    df = calc_scores(df, val, uni)

    # 金融股: M/Z-Score 置 NaN 不参与造假/破产判定
    df.loc[is_fin, ["m_score", "z_score"]] = np.nan
    df.loc[is_fin, "flag_M造假嫌疑"] = False
    df.loc[is_fin, "flag_Z破产风险"] = False
    # 重新算 veto (去掉 M/Z 两项, 金融股高杠杆是常态)
    df["veto"] = (df["flag_连亏两年"] | df["flag_M造假嫌疑"] | df["flag_Z破产风险"] | df["flag_经营现金流连负"])

    # 综合打分
    df["value_score"] = value_score(df["pe_cdf"], df["pb_cdf"], df["dv_ttm"])
    df["composite"] = df["f_score"] + df["value_score"]

    # 输出字段整理
    out_cols = ["name", "industry", "f_score", "m_score", "z_score", "n_flags",
                "pe_ttm", "pe_cdf", "pb", "pb_cdf", "dv_ttm", "roa_", "current_ratio",
                "debt_to_assets", "n_income_attr_p", "n_cashflow_act",
                "netprofit_yoy", "or_yoy", "total_mv", "composite", "value_score", "veto"]
    df["name"] = df["name"]
    df_out = df[out_cols].copy()
    df_out.columns = ["name", "industry", "F分数", "M分数", "Z分数", "红旗数",
                      "PE_TTM", "PE分位", "PB", "PB分位", "股息率TTM", "ROA", "流动比率",
                      "资产负债率", "归母净利", "经营现金流", "净利同比", "营收同比",
                      "总市值", "综合分", "价值分", "一票否决"]
    df_out["股息率TTM"] = df_out["股息率TTM"] / 100.0

    # 分层
    clean = df_out[(~df_out["一票否决"]) & (df_out["红旗数"] <= 1) & (df_out["F分数"] >= 6)]
    clean = clean.sort_values("综合分", ascending=False)

    grey = df_out[(~df_out["一票否决"]) & (df_out["F分数"] >= 7) & (df_out["红旗数"] >= 2)]
    grey = grey.sort_values("综合分", ascending=False)

    print(f"\n=== 排雷结果 ===")
    print(f"全市场: {len(df_out)} 只 | 一票否决(连亏/M造假/Z破产/现金流连负): {int(df_out['一票否决'].sum())} 只")
    print(f"通过排雷且 F≥6 (高价值池): {len(clean)} 只 | 灰区(F≥7但有2+红旗): {len(grey)} 只")

    # 保存
    out = args.out or os.path.join(ROOT, "outputs", "stock_diligence")
    df_out.to_csv(out + ".csv", index=True, encoding="utf-8-sig")
    with open(out + ".json", "w") as fp:
        json.dump({
            "last_date": str(last_date),
            "clean_top": clean.head(args.top).reset_index().to_dict("records"),
            "grey_top": grey.head(20).reset_index().to_dict("records"),
        }, fp, ensure_ascii=False, indent=2, default=str)

    # 终端打印 Top
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 200)
    print(f"\n=== 高价值标的 Top {args.top} (通过排雷, F≥6, 按综合分降序) ===")
    show = clean.head(args.top)[["name", "industry", "F分数", "红旗数", "PE_TTM", "PE分位", "PB分位",
                                 "股息率TTM", "ROA", "综合分"]]
    print(show.to_string(index=True, float_format=lambda x: f"{x:.2f}"))

    print(f"\n=== 待分析灰区标的 (F≥7 但有 2+ 红旗, 需人工甄别) ===")
    show2 = grey.head(20)[["name", "industry", "F分数", "红旗数", "PE分位", "PB分位", "M分数", "Z分数"]]
    print(show2.to_string(index=True, float_format=lambda x: f"{x:.2f}"))

    print(f"\nCSV: {out}.csv | JSON: {out}.json")
    conn.close()


if __name__ == "__main__":
    main()

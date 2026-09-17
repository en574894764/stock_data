#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
验证：把「ROE≥阈值」从单年(2025)改成「连续三年(2023/2024/2025)都≥阈值」，
异常（扣非<70%、扣非为负、净利同比为负、ROE>30%）会不会少很多。
"""

import numpy as np
import pandas as pd

import valuelib as vl

pd.set_option("display.width", 220)
pd.set_option("display.unicode.east_asian_width", True)


def main():
    conn = vl.connect()
    try:
        cfg = vl.load_config()
        roe_min = cfg["screen"]["roe_min"]
        debt_max = cfg["screen"]["debt_to_assets_max"]

        # 三年年报 ROE / 负债率 / 扣非净利
        fi = vl.query(
            "SELECT ts_code, report_year, roe, roe_dt, debt_to_assets, netprofit_yoy, profit_dedt "
            "FROM financial_indicator WHERE report_type='4' AND report_year IN (2023,2024,2025)",
            conn=conn)
        stocks = vl.query(
            "SELECT ts_code, name, industry, market, list_status FROM stocks", conn=conn)

        # 归母净利（用于扣非占比）
        inc = vl.query(
            "SELECT ts_code, n_income_attr_p FROM income "
            "WHERE report_type='4' AND report_year=2025", conn=conn)

        # 长表 → 宽表（ROE 三年）
        roe_w = fi.pivot_table(index="ts_code", columns="report_year", values="roe")
        roe_w.columns = ["roe_%d" % c for c in roe_w.columns]
        debt25 = fi[fi["report_year"] == 2025][["ts_code", "debt_to_assets", "netprofit_yoy", "profit_dedt", "roe_dt"]]

        base = stocks.merge(roe_w, on="ts_code", how="left").merge(debt25, on="ts_code", how="left").merge(inc, on="ts_code", how="left")
        base = base[base["ts_code"].str.endswith(tuple(cfg["screen"]["markets"]), na=False)]
        base = base[base["list_status"] == "L"]
        base = base[~base["name"].fillna("").str.contains("ST|退", regex=True)]

        base["扣非占比"] = base["profit_dedt"] / base["n_income_attr_p"]

        # 两个口径
        has3 = base[["roe_2023", "roe_2024", "roe_2025"]].notna().all(axis=1)
        base["三年ROE全≥阈值"] = (base["roe_2023"] >= roe_min) & (base["roe_2024"] >= roe_min) & (base["roe_2025"] >= roe_min)

        # 口径A：单年（当前）
        A = base[(base["roe_2025"] >= roe_min) & (base["debt_to_assets"] <= debt_max)]
        # 口径B：三年ROE
        B = base[(base["三年ROE全≥阈值"]) & (base["debt_to_assets"] <= debt_max)]
        # 口径C：三年ROE + 负债率也三年都低（更强）
        debt_w = fi.pivot_table(index="ts_code", columns="report_year", values="debt_to_assets")
        debt_w.columns = ["debt_%d" % c for c in debt_w.columns]
        base = base.merge(debt_w, on="ts_code", how="left")
        B3 = base[(base["三年ROE全≥阈值"])
                  & (base["debt_2023"] <= debt_max) & (base["debt_2024"] <= debt_max) & (base["debt_2025"] <= debt_max)]

        def report(name, d):
            n = len(d)
            print(f"\n{'='*70}\n{name}  n={n}\n{'='*70}")
            if n == 0:
                print("(空)")
                return
            print(f"  扣非占比 < 70%：      {(d['扣非占比']<0.7).sum():>4} 只   (原来 35)")
            print(f"  扣非为负：            {(d['profit_dedt']<=0).sum():>4} 只   (原来 8)")
            print(f"  净利同比为负：        {(d['netprofit_yoy']<0).sum():>4} 只   (原来 133)")
            print(f"  ROE(2025) > 30%：     {(d['roe_2025']>30).sum():>4} 只   (原来 27)")
            print(f"  负债率 40~50%(贴边)：  {((d['debt_to_assets']>40)&(d['debt_to_assets']<=50)).sum():>4} 只   (原来 158)")
            print(f"  ROE(2025) 中位：      {d['roe_2025'].median():.1f}%")

        report("口径A · 单年 ROE≥12% & 负债<50%（当前）", A)
        report("口径B · 三年 ROE 全≥12% & 负债(2025)<50%", B)
        report("口径C · 三年 ROE 全≥12% & 三年负债全<50%（更强）", B3)
        report("口径D · 三年ROE + 负债(2025)<50% + 扣非占比≥70%", B[B["扣非占比"] >= 0.7])
        report("口径E · D + 净利同比>0", B[(B["扣非占比"] >= 0.7) & (B["netprofit_yoy"] > 0)])

        # 异常的去向：口径A的异常里，有多少被口径B过滤掉
        print(f"\n{'='*70}\n异常个股在「三年ROE」口径下的去向\n{'='*70}")
        bad_mask = (A["扣非占比"] < 0.7) | (A["profit_dedt"] <= 0) | (A["netprofit_yoy"] < 0) | (A["roe_2025"] > 30)
        bad = A[bad_mask]
        kept_in_B = bad[bad["ts_code"].isin(B["ts_code"])]
        print(f"口径A 中「异常」合计 {len(bad)} 只；其中被三年ROE口径保留 {len(kept_in_B)} 只、被过滤掉 {len(bad)-len(kept_in_B)} 只")
        print("\n被三年ROE过滤后仍残留的异常（扣非<70% 或 扣非为负 或 净利负 或 ROE>30）前 15 只：")
        resid = kept_in_B.nsmallest(15, "扣非占比")
        print(resid[["ts_code", "name", "roe_2023", "roe_2024", "roe_2025", "扣非占比", "netprofit_yoy"]].to_string(index=False))

    finally:
        conn.close()


if __name__ == "__main__":
    main()

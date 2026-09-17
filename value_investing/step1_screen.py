#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
任务① 财报选股
─────────────────────────────────────────────────────────
完全按财报走，两个维度（均在 config.json 里可配）：
  1. ROE          ≥ screen.roe_min          （默认 12%）
  2. 资产负债率   ≤ screen.debt_to_assets_max（默认 50%）

数据源：financial_indicator（年报 report_type='4'）+ stocks（名称/行业/上市状态）
输出：  outputs/screen_list.csv

用法：
  python step1_screen.py            # 用配置里的阈值
  python step1_screen.py --roe 15 --debt 40   # 临时覆盖，不落盘
"""

from __future__ import annotations

import argparse
import sys

import pandas as pd

import valuelib as vl


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="价值投资 · 任务① 财报选股")
    ap.add_argument("--roe", type=float, help="临时覆盖 ROE 下限(%%)")
    ap.add_argument("--debt", type=float, help="临时覆盖 资产负债率上限(%%)")
    ap.add_argument("--year", type=int, help="临时覆盖基准年报年份")
    ap.add_argument("--out", default=str(vl.OUTPUT_DIR / "screen_list.csv"))
    args = ap.parse_args(argv)

    cfg = vl.load_config()
    if args.roe is not None:
        cfg["screen"]["roe_min"] = args.roe
    if args.debt is not None:
        cfg["screen"]["debt_to_assets_max"] = args.debt
    if args.year is not None:
        cfg["screen"]["base_year"] = args.year

    base_year = vl.resolve_base_year(cfg)
    roe_col = cfg["screen"].get("roe_field", "roe")
    nyears = int(cfg["screen"].get("roe_consecutive_years", 1))
    yoy_min = cfg["screen"].get("netprofit_yoy_min")
    dpr_min = cfg["screen"].get("deduct_profit_ratio_min")

    extra = f" | 连续 {nyears} 年 ROE" if nyears > 1 else ""
    extra += f" | 净利同比 ≥ {yoy_min}%" if yoy_min is not None else ""
    extra += f" | 扣非占比 ≥ {dpr_min}" if dpr_min is not None else ""
    print(f"[任务①] 基准年报 = {base_year} 年"
          f" | ROE({roe_col}) ≥ {cfg['screen']['roe_min']}%"
          f" | 资产负债率 ≤ {cfg['screen']['debt_to_assets_max']}%"
          f" | 股票池后缀 {cfg['screen']['markets']}{extra}")

    raw = vl.fetch_screen_universe(cfg, base_year)
    print(f"[任务①] 财报覆盖（不晚于 {base_year} 年的最近年报）：{len(raw):,} 只")

    # 连续 N 年 ROE 校验（默认 N=1 跳过）
    if nyears > 1:
        roe_hist = vl.fetch_roe_history(base_year, nyears)
        raw = raw.merge(roe_hist, on="ts_code", how="left")
        need = [f"roe_{base_year - k}" for k in range(nyears)]
        raw["_roe_ok"] = raw[need].apply(lambda r: (r >= cfg["screen"]["roe_min"]).all(), axis=1)
        before = len(raw)
        raw = raw[raw["_roe_ok"].fillna(False)].drop(columns=["_roe_ok"])
        print(f"[任务①] 连续 {nyears} 年 ROE 达标后：{before:,} → {len(raw):,} 只")

    res = vl.apply_screen(raw, cfg, base_year)
    res["base_year"] = res["report_year"]
    res["roe_used"] = res[roe_col]

    cols = ["ts_code", "symbol", "name", "industry", "market",
            "base_year", "ann_date", "roe_used", "roe", "roe_dt",
            "debt_to_assets", "roa", "netprofit_yoy", "profit_dedt",
            "n_income_attr_p", "扣非占比", "bps", "current_ratio"]
    res = res[[c for c in cols if c in res.columns]]
    rename = {"ts_code": "代码", "symbol": "代码简称", "name": "名称", "industry": "行业", "market": "市场",
              "base_year": "基准年报", "ann_date": "公告日", "roe_used": "ROE", "roe": "ROE摊薄", "roe_dt": "ROE扣非",
              "debt_to_assets": "资产负债率", "roa": "ROA", "netprofit_yoy": "净利同比",
              "profit_dedt": "扣非净利", "n_income_attr_p": "归母净利", "扣非占比": "扣非占比",
              "bps": "每股净资产", "current_ratio": "流动比率"}
    res = res.rename(columns=rename)

    res.to_csv(args.out, index=False, encoding="utf-8-sig")
    print(f"[任务①] 合格 list：{len(res):,} 只 → {args.out}")
    if not res.empty:
        print(f"[任务①] ROE 中位 {res['ROE'].median():.1f}%"
              f" | 资产负债率中位 {res['资产负债率'].median():.1f}%"
              f" | 行业前五：{res['行业'].value_counts().head(5).to_dict()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

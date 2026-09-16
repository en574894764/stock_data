#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
任务② 合理估值（纯财报口径）
─────────────────────────────────────────────────────────
基准：上一年年报（当前 2026 年 → 2025 年报，可在 config 里改）

  去年年报合理估值   = 归母净利润(FY基年) × PE倍数        （默认 30 倍）
  三年后净利润       = 归母净利润(FY基年) × (1 + g)^3
  三年后合理估值     = 三年后净利润 × PE倍数

增速 g 的拟合规则（全部可配）：
  1. 优先用最近 5 个年报利润点 → 几何年化 CAGR
  2. 不足 5 个点（或含亏损导致无法算 CAGR）→ 退化到 3 个点
  3. 仍不足 3 个点 → 用配置的默认增速
  4. 最后再做 [min_rate, max_rate] 上下限保护，并标记是否被截断

输出：outputs/fair_value.csv
"""

from __future__ import annotations

import argparse
import sys

import numpy as np
import pandas as pd

import valuelib as vl


def compute_fair_value(cfg: dict, base_year: int | None = None, screen: pd.DataFrame | None = None,
                       conn=None) -> pd.DataFrame:
    """返回带合理估值的 DataFrame（金额单位：亿元）"""
    base_year = base_year or vl.resolve_base_year(cfg)
    if screen is None:
        p = vl.OUTPUT_DIR / "screen_list.csv"
        if not p.exists():
            raise FileNotFoundError(f"缺少选股结果 {p}，请先跑 step1_screen.py")
        screen = pd.read_csv(p, dtype={"代码": str})

    codes = screen["代码"].tolist()
    hist = vl.fetch_profit_history(codes, base_year, cfg, conn=conn)

    # 长表 → {ts_code: {year: 归母净利润}}
    prof: dict[str, dict[int, float | None]] = {}
    for ts, grp in hist.groupby("ts_code"):
        prof[ts] = dict(zip(grp["report_year"].astype(int), grp["n_income_attr_p"]))

    pe = float(cfg["valuation"]["pe_multiple"])
    years_fwd = int(cfg["valuation"].get("projection_years", 3))

    rows = []
    for _, r in screen.iterrows():
        ts = r["代码"]
        series = prof.get(ts, {})
        extra = dict(名称=r["名称"], 行业=r["行业"],
                     ROE=r.get("ROE"), 资产负债率=r.get("资产负债率"),
                     基准年报=r.get("基准年报", base_year), 公告日=r.get("公告日"))
        np_base = series.get(base_year)
        if np_base is None:  # 基年无数据，退到不晚于基年的最近一年
            cand = [y for y in series if y <= base_year and series[y] is not None]
            np_base = series[max(cand)] if cand else None

        if np_base is None or np_base <= 0:
            rows.append(dict(代码=ts, **extra,
                             归母净利润=None, 年化增速=None, 增速口径="利润≤0或缺失", 增速被截断=None,
                             去年年报合理估值=None, 三年后净利润=None, 三年后合理估值=None,
                             估值可用=False))
            continue

        g, note, clipped = vl.fit_growth(series, cfg)
        np_fwd = np_base * (1 + g) ** years_fwd

        rows.append(dict(
            代码=ts, **extra,
            归母净利润=vl.yi(np_base),
            年化增速=g, 增速口径=note, 增速被截断=clipped,
            去年年报合理估值=vl.yi(np_base * pe),
            三年后净利润=vl.yi(np_fwd),
            三年后合理估值=vl.yi(np_fwd * pe),
            估值可用=True,
        ))

    df = pd.DataFrame(rows)
    return df.sort_values("三年后合理估值", ascending=False, na_position="last").reset_index(drop=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="价值投资 · 任务② 合理估值")
    ap.add_argument("--year", type=int)
    ap.add_argument("--out", default=str(vl.OUTPUT_DIR / "fair_value.csv"))
    args = ap.parse_args(argv)

    cfg = vl.load_config()
    if args.year:
        cfg["screen"]["base_year"] = args.year
    base_year = vl.resolve_base_year(cfg)
    g = cfg["valuation"]["growth"]

    print(f"[任务②] 基准年报 = {base_year} | PE = {cfg['valuation']['pe_multiple']}×"
          f" | 外推 {cfg['valuation']['projection_years']} 年"
          f" | 增速：优先{g['prefer_points']}点→退{g['fallback_points']}点→默认{g['default_rate']:.1%}"
          f" | 截断区间 [{g['min_rate']:.0%}, {g['max_rate']:.0%}]")

    df = compute_fair_value(cfg, base_year)
    df.to_csv(args.out, index=False, encoding="utf-8-sig")

    ok = df[df["估值可用"]]
    print(f"[任务②] 估值可用 {len(ok):,} / {len(df):,} 只 → {args.out}")
    if len(ok):
        dist = ok["增速口径"].str.split("(").str[0].value_counts().to_dict()
        print(f"[任务②] 增速口径分布：{dist}")
        print(f"[任务②] 增速中位 {ok['年化增速'].median():.1%}"
              f" | 被上下限截断 {int(ok['增速被截断'].fillna(False).sum())} 只")
    return 0


if __name__ == "__main__":
    sys.exit(main())

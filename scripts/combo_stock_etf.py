#!/usr/bin/env python3
"""核心+卫星合击验证: 个股截面策略 × ETF 动量轮动 的相关性与组合效果
====================================================================================
产出: 报告数字打印 + reports/combo_stock_etf.md
用法: python3 scripts/combo_stock_etf.py --start 2017-01-01
"""
import argparse
import os
import sys

import numpy as np
import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import factor_eval as fe  # noqa: E402
import strategy_lib as sl  # noqa: E402
from backtest_report import replay  # noqa: E402
from etf_rotation import run as etf_run, load_etf_returns, load_list_dates, POOL, stats  # noqa: E402


def fmt(v):
    return "-" if v is None or not np.isfinite(v) else f"{v*100:.1f}%"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2017-01-01")
    ap.add_argument("--topn", type=int, default=30)
    args = ap.parse_args()
    end = pd.Timestamp.today().strftime("%Y-%m-%d")
    conn = fe.get_conn()

    # --- 个股截面 (核心): 6因子 TopN
    uni, list_dates = fe.load_universe_filter(conn)
    st = sl.load_strategies(conn, [f"prod_6f_eq@topn={args.topn}"])[0]
    factors = {n: sl.load_factor(conn, n, args.start, end) for n in st["cfg"]["factors"]}
    load_start = (pd.Timestamp(args.start) - pd.Timedelta(days=40)).strftime("%Y-%m-%d")
    daily_ret = fe.load_daily_returns(conn, load_start, end)
    rebal = fe.rebalance_dates(daily_ret.index, args.start, end)
    sr = replay(st, factors, daily_ret, uni, list_dates, rebal)
    stock_nav = sr["nav"]

    # --- ETF 轮动 (卫星): 动量252 Top3
    wide = load_etf_returns(conn, list(POOL) + ["511990", "510880"], "2015-06-01", end)
    ld = load_list_dates(conn)
    er = etf_run(wide, ld, 252, 3, False, args.start)
    etf_nav = er["nav"]
    conn.close()

    # 对齐 (个股调仓日历为基准, ETF ffill)
    common = stock_nav.index.intersection(etf_nav.index)
    stock_nav = stock_nav.loc[common]
    etf_nav = etf_nav.reindex(common).ffill()
    rs = stock_nav.pct_change().fillna(0)
    re_ = etf_nav.pct_change().fillna(0)

    lines = [f"# 核心+卫星合击验证 ({args.start} ~ {end})\n",
             f"- 核心: 6因子等权 Top{args.topn} (月频) | 卫星: ETF 动量252日 Top3 (月频)",
             f"- 组合为日度再平衡近似 (月度再平衡结果会略优)\n",
             "| 组合 | 年化 | 波动 | 夏普 | 最大回撤 |", "|---|---|---|---|---|"]
    print(f"个股截面 Top{args.topn}: {fmt(stats(stock_nav)['ann'])} 夏普 {stats(stock_nav)['sharpe']:.2f} 回撤 {fmt(stats(stock_nav)['dd'])}")
    s_etf = stats(etf_nav)
    print(f"ETF轮动: {fmt(s_etf['ann'])} 夏普 {s_etf['sharpe']:.2f} 回撤 {fmt(s_etf['dd'])}")
    corr = rs.corr(re_)
    print(f"日收益相关: {corr:.2f}")
    lines.append(f"日收益相关系数: **{corr:.2f}**\n")

    for w in [0.3, 0.4, 0.5, 0.6, 0.7]:
        combo = (1 + w * rs + (1 - w) * re_).cumprod()
        s = stats(combo)
        label = f"个股 {w*100:.0f}% + ETF {(1-w)*100:.0f}%"
        lines.append(f"| {label} | {fmt(s['ann'])} | {fmt(s['vol'])} | {s['sharpe']:.2f} | {fmt(s['dd'])} |")
        print(f"[{label}] 年化 {fmt(s['ann'])} 夏普 {s['sharpe']:.2f} 回撤 {fmt(s['dd'])}")
    for name, nav in [("个股截面", stock_nav), ("ETF轮动", etf_nav)]:
        s = stats(nav)
        lines.append(f"| {name} (单跑) | {fmt(s['ann'])} | {fmt(s['vol'])} | {s['sharpe']:.2f} | {fmt(s['dd'])} |")

    # 逐年: 50/50 vs 单跑
    combo50 = (1 + 0.5 * rs + 0.5 * re_).cumprod()
    lines.append("\n## 逐年对照 (50/50 合击 vs 单跑)\n")
    lines.append("| 年份 | 合击50/50 | 个股 | ETF |")
    lines.append("|---|---|---|---|")
    for y in sorted(set(common.year)):
        def yr(nav):
            seg = nav[nav.index.year == y]
            return (seg.iloc[-1] / seg.iloc[0] - 1) if len(seg) > 20 else np.nan
        lines.append(f"| {y} | {fmt(yr(combo50))} | {fmt(yr(stock_nav))} | {fmt(yr(etf_nav))} |")

    out = os.path.join(REPO, "reports", "combo_stock_etf.md")
    with open(out, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n✅ 报告: {out}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""隔夜-日内因子: 等权池组合增益检验
====================================================================================
口径: Top30 + min_var_cap10 (60日协方差, 单边成本0.15%) —— 与生产一致;
      因子层等权 (已证最优), 基准 = 现有 6 因子池, 增量 = 逐一向池中加入新因子。
      主网格锚 2019-01-01 (生产相位); 关键方案过 4 相位稳健性 (offset 2/5/10/15)。

用法: python3 scripts/intraday_pool_test.py [--no-phase]
"""
import os
import sys

import numpy as np
import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import factor_eval as fe  # noqa: E402
import strategy_lib as sl  # noqa: E402
import factor_weight_scan as fws  # noqa: E402

OLD = ["turnover_20", "ivol_60", "ret_20d_rev", "ln_mv", "ep_ttm", "sue_delta"]
NEW = ["on_mom_20", "id_mom_20", "on_share_20", "id_on_amp_20",
       "on_vol_20", "on_skew_20", "on_rev_5", "tug_20"]
ALL = OLD + NEW


def w_mask(active):
    act = set(active)

    def f(t, ics, ridges):
        return {n: (1.0 if n in act else 0.0) for n in ALL}
    return f


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-phase", action="store_true")
    args = ap.parse_args()

    end = pd.Timestamp.today().strftime("%Y-%m-%d")
    conn = fe.get_conn()
    uni, list_dates = fe.load_universe_filter(conn)
    fwides = {n: sl.load_factor(conn, n, "2015-06-01", end) for n in ALL}
    daily_ret = fe.load_daily_returns(conn, "2015-10-01", end)
    conn.close()
    idx = daily_ret.index
    grid = fws.build_grid(idx, fws.ANCHOR, fws.BACK_TO, end)
    rebal = [t for t in grid if t >= pd.Timestamp(fws.ANCHOR)]
    sec = fws.Section(fwides, daily_ret, uni, list_dates)
    print(f"股票池 {len(uni)} | 因子 {len(ALL)} 个 | 网格 {len(grid)} 期 | 回测 {len(rebal)} 期")

    pools = [("pool6", OLD)] + [(f"pool6+{n}", OLD + [n]) for n in NEW] \
            + [("pool_all14", ALL)]
    OOS, T3Y = pd.Timestamp("2023-01-01"), pd.Timestamp("2023-09-01")

    lines = [f"# 隔夜-日内因子: 等权池组合增益检验 ({fws.ANCHOR} ~ {end})\n",
             "- 口径: Top30 + min_var_cap10 (60日协方差) · 单边成本 0.15% · 因子层等权",
             "- 基准 pool6 = 现有 6 因子; 增量方案 = 6 因子 + 单个新因子 (权重 1.0)",
             "- 主网格锚 2019-01-01 (生产相位)\n",
             "| 方案 | 年化 | 夏普 | 回撤 | 样本外夏普 | 样本外回撤 | 三年窗口年化 | 换手 |",
             "|---|---|---|---|---|---|---|---|"]
    results = {}
    for name, active in pools:
        nav, turn, _ = fws.backtest(sec, w_mask(active), grid, daily_ret, rebal)
        s, so = fws.stats(nav), fws.stats(nav[nav.index >= OOS])
        s3 = fws.stats(nav[nav.index >= T3Y])
        results[name] = {"nav": nav, "ann": s["ann"], "sharpe": s["sharpe"],
                         "dd": s["dd"], "oos": so["sharpe"], "3y": s3["ann"],
                         "turn": turn}
        lines.append(f"| {name} | {s['ann']*100:.1f}% | {s['sharpe']:.2f} | {s['dd']*100:.1f}% "
                     f"| {so['sharpe']:.2f} | {so['dd']*100:.1f}% | {s3['ann']*100:.1f}% "
                     f"| {turn*100:.0f}% |")
        print(f"{name:18s} 年化 {s['ann']*100:5.1f}% 夏普 {s['sharpe']:5.2f} 回撤 {s['dd']*100:6.1f}% "
              f"OOS夏普 {so['sharpe']:5.2f} 3Y {s3['ann']*100:5.1f}% 换手 {turn*100:.0f}%")

    # 多相位: pool6 + 主相位夏普最好的两个增量方案 + pool_all14
    if not args.no_phase:
        ranked = sorted([(n, r["sharpe"]) for n, r in results.items() if n != "pool6"],
                        key=lambda x: -x[1])
        phase_pools = ["pool6"] + [n for n, _ in ranked[:2]] + (["pool_all14"] if "pool_all14" not in
                                                              [n for n, _ in ranked[:2]] else [])
        lines.append("\n## 多相位稳健性检验 (offset 2/5/10/15 交易日, 报年化)\n")
        lines.append("| 方案 | 主相位 | +2日 | +5日 | +10日 | +15日 | 均值 | 最差 |")
        lines.append("|---|---|---|---|---|---|---|---|")
        print("\n多相位 ...")
        for name in phase_pools:
            active = dict(pools)[name]
            anns = [results[name]["ann"]]
            for off in fws.PHASE_OFFSETS:
                g = fws.offset_grid(idx, fws.ANCHOR, off, end)
                rb = [t for t in g if t >= pd.Timestamp(fws.ANCHOR)]
                nav_off, _, _ = fws.backtest(sec, w_mask(active), g, daily_ret, rb)
                anns.append(fws.stats(nav_off)["ann"])
            lines.append(f"| {name} | " + " | ".join(f"{a*100:.1f}%" for a in anns) +
                         f" | {np.mean(anns)*100:.1f}% | {np.min(anns)*100:.1f}% |")
            print(f"  {name:18s} " + " ".join(f"{a*100:5.1f}%" for a in anns) +
                  f" | 均值 {np.mean(anns)*100:.1f}% 最差 {np.min(anns)*100:.1f}%")

    out = os.path.join(REPO, "reports", "intraday_pool_test.md")
    with open(out, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n✅ 报告: {out}")


if __name__ == "__main__":
    main()

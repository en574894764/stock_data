#!/usr/bin/env python3
"""LGBM 因子升级 A/B: 30 特征(含隔夜-日内) vs 22 特征(原) 
====================================================================================
同一框架 (Top30 + min_var_cap10, 生产相位) 跑两个版本 lgbm_score 的组合绩效 + 单因子 IC。
旧版从 factor_cache/lgbm_score_backup.parquet 加载 (回滚保障)。

用法: python3 scripts/lgbm_upgrade_compare.py
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


def w_single(t, ics, ridges):
    return {"lgbm_score": 1.0}


def main():
    end = pd.Timestamp.today().strftime("%Y-%m-%d")
    conn = fe.get_conn()
    uni, list_dates = fe.load_universe_filter(conn)
    fw_new = sl.load_factor(conn, "lgbm_score", "2015-06-01", end)
    conn.close()

    old = pd.read_parquet(os.path.join(REPO, "factor_cache", "lgbm_score_backup.parquet"))
    fw_old = old.pivot_table(index="trade_date", columns="ts_code", values="value").sort_index(axis=1)
    fw_old.index = pd.to_datetime(fw_old.index)

    daily_ret = fe.load_daily_returns(fe.get_conn(), "2015-10-01", end)
    fwd = fe.fwd_from_daily(daily_ret)
    idx = daily_ret.index
    grid = fws.build_grid(idx, fws.ANCHOR, fws.BACK_TO, end)
    rebal = [t for t in grid if t >= pd.Timestamp(fws.ANCHOR)]
    print(f"网格 {len(grid)} 期 | 回测 {len(rebal)} 期 | 旧版 {fw_old.shape} 新版 {fw_new.shape}")

    OOS, T3Y = pd.Timestamp("2023-01-01"), pd.Timestamp("2023-09-01")
    lines = [f"# LGBM 因子升级 A/B: 30 特征 (含隔夜-日内 8 因子) vs 22 特征 ({fws.ANCHOR} ~ {end})\n",
             "- 口径: Top30 + min_var_cap10 (60日协方差) · 单边成本 0.15% · 生产相位锚 2019-01-01",
             "- 旧版从 factor_cache/lgbm_score_backup.parquet 加载 (22 特征); 新版为 30 特征重训\n"]
    rows = {}
    for label, fw in [("old_22feat", fw_old), ("new_30feat", fw_new)]:
        # 单因子 IC
        try:
            r = fe.evaluate_single("lgbm_score", fw, fwd, daily_ret, rebal, uni, list_dates)
            ics, q = r["ics"], r["q_navs"]
            ic, icir = ics.mean(), ics.mean() / ics.std()
            s5 = fe.nav_stats(q[5]) if q is not None else None
            ls = fe.nav_stats(q[5] / q[1]) if q is not None else None
        except Exception as e:
            ic = icir = np.nan
            s5 = ls = None
            print(f"{label} 单因子评估失败: {e}")
        # 组合回测
        sec = fws.Section({"lgbm_score": fw}, daily_ret, uni, list_dates)
        nav, turn, _ = fws.backtest(sec, w_single, grid, daily_ret, rebal)
        s = fws.stats(nav)
        so = fws.stats(nav[nav.index >= OOS])
        s3 = fws.stats(nav[nav.index >= T3Y])
        anns = [s["ann"]]   # lgbm_score 为稀疏因子 (仅 2019 网格有值), 不做相位偏移检验
        rows[label] = {"ic": ic, "icir": icir, "ann": s["ann"], "sharpe": s["sharpe"],
                       "dd": s["dd"], "oos_sharpe": so["sharpe"], "oos_dd": so["dd"],
                       "3y": s3["ann"], "turn": turn, "phase_mean": float(np.mean(anns)),
                       "phase_min": float(np.min(anns)), "q5": s5["ann_ret"] if s5 else np.nan,
                       "ls": ls["ann_ret"] if ls else np.nan}
        print(f"{label}: 单因子IC {ic*100:.1f}% ICIR {icir:.2f} | 组合 年化 {s['ann']*100:.1f}% "
              f"夏普 {s['sharpe']:.2f} 回撤 {s['dd']*100:.1f}% OOS夏普 {so['sharpe']:.2f} "
              f"3Y {s3['ann']*100:.1f}% 换手 {turn*100:.0f}%")

    lines.append("| 版本 | 单因子IC | ICIR | 组合年化 | 夏普 | 回撤 | 样本外夏普 | 样本外回撤 | 三年窗口 | 换手 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for label in ["old_22feat", "new_30feat"]:
        x = rows[label]
        lines.append(f"| {label} | {x['ic']*100:.1f}% | {x['icir']:.2f} | {x['ann']*100:.1f}% "
                     f"| {x['sharpe']:.2f} | {x['dd']*100:.1f}% | {x['oos_sharpe']:.2f} "
                     f"| {x['oos_dd']*100:.1f}% | {x['3y']*100:.1f}% | {x['turn']*100:.0f}% |")
    lines.append("\n## 单因子分层 (Q1/Q5/多空)\n")
    lines.append("| 版本 | Q1年化 | Q5年化 | 多空 |")
    lines.append("|---|---|---|---|")
    for label in ["old_22feat", "new_30feat"]:
        x = rows[label]
        lines.append(f"| {label} | - | {x['q5']*100:.1f}% | {x['ls']*100:.1f}% |")

    d = rows["new_30feat"]
    o = rows["old_22feat"]
    lines.append(f"\n**差异 (新-旧)**: 单因子IC {(d['ic']-o['ic'])*100:+.1f}pp · ICIR {d['icir']-o['icir']:+.2f} · "
                 f"年化 {(d['ann']-o['ann'])*100:+.1f}pp · 夏普 {d['sharpe']-o['sharpe']:+.2f} · "
                 f"回撤 {(d['dd']-o['dd'])*100:+.1f}pp · OOS夏普 {d['oos_sharpe']-o['oos_sharpe']:+.2f} · "
                 f"多空 {(d['ls']-o['ls'])*100:+.1f}pp")
    lines.append("\n注: lgbm_score 为稀疏因子 (仅 2019 起 20 日网格有值), 不做相位偏移检验。")

    out = os.path.join(REPO, "reports", "lgbm_upgrade_ab.md")
    with open(out, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n✅ 报告: {out}")


if __name__ == "__main__":
    main()

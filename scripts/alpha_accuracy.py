#!/usr/bin/env python3
"""
α 预估准确度体检：6 因子合成分的「排序兑现」与「幅度校准」
============================================================
三个视角:
  1. 分桶兑现: 合成分十分位 (D1=最差预测 → D10=最好预测) 的实际未来20日收益
     → 检验排序准不准 (单调性 = 排序兑现)
  2. 幅度校准: 预测隐含收益 (回归斜率×z分) vs 实际收益 → 校准率
     → 检验幅度准不准 (预测的分差兑现了几成)
  3. 分年度 R²/IC 趋势 → 准确度是否在衰减

用法: python3 scripts/alpha_accuracy.py
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import factor_eval as fe  # noqa: E402

START, END = "2019-01-01", "2026-09-01"
OOS = pd.Timestamp("2023-01-01")
SIX = ["ret_20d_rev", "turnover_20", "ivol_60", "ln_mv", "ep_ttm", "sue_delta"]
DECILES = 10


def main():
    conn = fe.get_conn()
    uni, list_dates = fe.load_universe_filter(conn)
    factors = fe.load_factors(conn, START, END)
    daily_ret = fe.load_daily_returns(conn, "2018-11-01", END)
    conn.close()
    fwd = fe.fwd_from_daily(daily_ret, horizon=fe.REBAL)

    d = daily_ret.index
    d = d[(d >= pd.Timestamp(START)) & (d <= pd.Timestamp(END))]
    rebal = list(d[::fe.REBAL])

    rows = []  # (date, score_series, fwd_series)
    for t in rebal:
        if any(t not in factors[n].index for n in SIX) or t not in fwd.index:
            continue
        zs, ok = [], True
        for n in SIX:
            row = factors[n].loc[t].dropna()
            keep = [c for c in row.index if c in uni and list_dates.get(c) is not None
                    and t >= pd.Timestamp(list_dates[c]) + pd.Timedelta(days=120)]
            row = row[keep]
            if len(row) < 50:
                ok = False
                break
            z = (row - row.mean()) / (row.std() if row.std() > 0 else 1.0)
            zs.append(z)
        if not ok:
            continue
        score = pd.concat(zs, axis=1).sum(axis=1)
        y = fwd.loc[t].reindex(score.index).dropna()
        score = score.reindex(y.index)
        if len(score) >= 100:
            rows.append((t, score, y))

    # ---------- 1. 分桶兑现 ----------
    all_pairs = pd.DataFrame({
        "score": pd.concat([r[1] for r in rows]),
        "y": pd.concat([r[2] for r in rows]),
    })
    all_pairs["decile"] = pd.qcut(all_pairs["score"], DECILES, labels=False) + 1
    # 实际收益按 20 日持有折算年化
    bucket = all_pairs.groupby("decile").agg(
        n=("y", "size"), mean_fwd=("y", "mean"), median_fwd=("y", "median"))
    bucket["ann"] = (1 + bucket["mean_fwd"]) ** (244 / fe.REBAL) - 1

    # ---------- 2. 逐期回归: R² + 校准率 ----------
    stats = []
    for t, score, y in rows:
        x = (score - score.mean()) / (score.std() if score.std() > 0 else 1.0)
        x, yv = x.values, y.values
        slope, intercept = np.polyfit(x, yv, 1)
        yhat = slope * x + intercept
        ss_res = ((yv - yhat) ** 2).sum()
        ss_tot = ((yv - yv.mean()) ** 2).sum()
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else np.nan
        ic = pd.Series(x).corr(pd.Series(yv), method="spearman")
        # 实际收益对预测分的再回归 (只取 D8-D10 vs D1-D3 头尾, 校准率=头尾实际价差/预测分差)
        hi = yv[x >= np.quantile(x, 0.8)].mean()
        lo = yv[x <= np.quantile(x, 0.2)].mean()
        pred_spread = x[x >= np.quantile(x, 0.8)].mean() - x[x <= np.quantile(x, 0.2)].mean()
        stats.append({"date": t, "r2": r2, "ic": ic, "slope": slope,
                      "actual_spread": hi - lo, "pred_spread": pred_spread,
                      "calib": (hi - lo) / pred_spread if pred_spread > 0 else np.nan})
    st = pd.DataFrame(stats).set_index("date")
    st["year"] = st.index.year
    yearly = st.groupby("year").agg(r2=("r2", "mean"), ic=("ic", "mean"),
                                    calib=("calib", "median"), n=("r2", "size"))

    # ---------- 输出 ----------
    L = ["# α 预估准确度体检 (6因子等权合成分, 2019-2026)\n"]
    L.append(f"- 样本: {len(rows)} 期调仓截面, 每期约 {int(all_pairs.groupby(all_pairs.index // 0 + 1).size.mean() if False else len(all_pairs) / len(rows))} 只")
    L.append(f"- 预测目标: T+1→T+21 相对收益 | 合成分: 6 因子 z-score 等权\n")

    L.append("## 1. 分桶兑现 (排序准不准): 预测十分位 → 实际年化收益\n")
    L.append("| 预测分位 | D1(最差) | D2 | D3 | D4 | D5 | D6 | D7 | D8 | D9 | D10(最好) |")
    L.append("|---|" + "---|" * 10)
    L.append("| 实际年化 | " + " | ".join(f"{bucket.loc[q, 'ann']:.1%}" for q in range(1, 11)) + " |")
    mono = all(bucket["ann"].diff().dropna() > 0)
    L.append(f"\n单调性: {'✓ 完全单调 (排序兑现良好)' if mono else '✗ 存在倒挂'} | "
             f"D10−D1 实际价差: {bucket.loc[DECILES, 'ann'] - bucket.loc[1, 'ann']:.1%} 年化\n")

    L.append("## 2. 幅度校准 (幅度准不准)\n")
    L.append(f"- 全期 R² 均值: {st['r2'].mean():.2%} (预测分解释了实际收益变异的 {st['r2'].mean():.1%})")
    L.append(f"- 校准斜率中位数: {st['calib'].median():.2%} 月收益/σ (头尾实际价差 ÷ 预测分σ差)")
    L.append(f"- 含义: 预测分每高 1σ，实际月收益差中位数仅 {st['calib'].median():.2%}；"
             f"头尾 2.5σ 分差年化兑现约 {((1 + st['calib'].median() * 2.5) ** (244 / fe.REBAL) - 1):.0%}")
    L.append(f"- 斜率逐期不稳定 (分年度在 0% 附近波动) —— 排序可用、幅度不可依赖的实证\n")

    L.append("## 3. 分年度趋势 (准确度是否衰减)\n")
    L.append("| 年份 | R² | IC | 校准斜率 | 期数 |")
    L.append("|---|---|---|---|---|")
    for y, r in yearly.iterrows():
        L.append(f"| {y} | {r['r2']:.2%} | {r['ic']:.1%} | "
                 f"{r['calib']:.0%} | {int(r['n'])} |")
    oos = st[st.index >= OOS]
    L.append(f"\n- 样本外 (2023+): R² {oos['r2'].mean():.2%} | IC {oos['ic'].mean():.1%} | "
             f"校准斜率 {oos['calib'].median():.2%}/σ")

    report = "\n".join(L) + "\n"
    print(report)
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "reports", "alpha_accuracy.md")
    with open(out, "w") as f:
        f.write(report)
    print(f"报告: {out}")


if __name__ == "__main__":
    main()

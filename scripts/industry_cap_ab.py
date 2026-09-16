#!/usr/bin/env python3
"""行业约束 A/B 对照: 无约束 vs 行业≤15% (min_var_cap10 组合)
====================================================================================
验证 generate_signals 新增的 industry_cap=0.15 是否"看起来该改反而变差"。
组合层锁定 Top30 + min_var_cap10 + 60日协方差 + 单边成本 0.15%, **唯一变量 = 行业约束**。

20 相位稳健性 + 双口径 (raw/rank) 交叉验证 —— 行业约束是风控项, 预期是
「年化略降 / 回撤与集中度改善」的 trade-off, 而非纯 alpha 改进。

用法: python3 scripts/industry_cap_ab.py [--n-phase 20]
输出: reports/industry_cap_ab.md + outputs/industry_cap_ab.json
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import sys

import numpy as np
import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import factor_eval as fe          # noqa: E402
import strategy_lib as sl         # noqa: E402
import factor_weight_scan as fws  # noqa: E402
from phase_robustness import SectionZ  # noqa: E402  (raw/rank 口径)

ANCHOR = "2019-01-01"
OOS = "2023-01-01"
CAP = 0.15
SCHEMES = [("no_cap", None), ("cap15", CAP)]


def pct(v):
    return "-" if v is None or not np.isfinite(v) else f"{v*100:.1f}%"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-phase", type=int, default=20)
    args = ap.parse_args()

    end = pd.Timestamp.today().strftime("%Y-%m-%d")
    conn = fe.get_conn()
    uni, list_dates = fe.load_universe_filter(conn)
    daily_ret = fe.load_daily_returns(conn, "2015-10-01", end)
    idx = daily_ret.index
    fw = {n: sl.load_factor(conn, n, "2015-06-01", end).astype("float32")
          for n in fws.FACTORS}
    industry_map = sl.load_industry_map(conn)
    conn.close()
    print(f"股票池 {len(uni)} | 行业映射 {len(industry_map)} | 相位 offset 0..{args.n_phase-1}", flush=True)

    secs = {m: SectionZ(fw, daily_ret, uni, list_dates, mode=m) for m in ("raw", "rank")}

    rows = []
    for off in range(args.n_phase):
        g = fws.offset_grid(idx, ANCHOR, off, end)
        rb = [t for t in g if t >= pd.Timestamp(ANCHOR)]
        for mode, sec in secs.items():
            for scheme, cap in SCHEMES:
                nav, turn, _ = fws.backtest(sec, fws.w_equal, g, daily_ret, rb,
                                            industry_cap=cap, industry_map=industry_map)
                s = fws.stats(nav)
                so = fws.stats(nav[nav.index >= pd.Timestamp(OOS)])
                rows.append({"offset": off, "mode": mode, "scheme": scheme,
                             "ann": s["ann"], "sharpe": s["sharpe"], "dd": s["dd"],
                             "oos_sharpe": so["sharpe"], "turn": turn})
                print(f"  off={off:2d} {mode:4s} {scheme:7s} 年化 {pct(s['ann'])} "
                      f"夏普 {s['sharpe']:.2f} 回撤 {pct(s['dd'])}", flush=True)
        gc.collect()

    df = pd.DataFrame(rows)

    L = ["# 行业约束 A/B 对照：无约束 vs 行业≤15%（20 相位）", "",
         f"> {pd.Timestamp.today().date()} | 组合层锁定生产口径 Top30 + min_var_cap10 + 60日协方差 + 单边成本 0.15%",
         f"> 唯一变量 = 行业权重约束（≤{CAP:.0%}），行业映射来自 stocks.industry（申万，NaN→其他）",
         f"> 相位 = 2019-01 起头 20 个交易日各自作锚点（等效「每月挑哪天调仓」的完整周期）", "",
         "## 汇总（20 相位）", "",
         "| 口径 | 方案 | 年化均值 | 年化中位 | 年化离散 | 年化最差 | 夏普均值 | 夏普最差 | 回撤均值 | OOS夏普均值 |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    summ = {}
    for mode in secs:
        for scheme, _ in SCHEMES:
            d = df[(df["mode"] == mode) & (df["scheme"] == scheme)]
            r = {"ann_mean": d["ann"].mean(), "ann_med": d["ann"].median(),
                 "ann_std": d["ann"].std(), "ann_min": d["ann"].min(),
                 "sharpe_mean": d["sharpe"].mean(), "sharpe_min": d["sharpe"].min(),
                 "dd_mean": d["dd"].mean(), "oos_mean": d["oos_sharpe"].mean()}
            summ[f"{mode}/{scheme}"] = r
            L.append(f"| `{mode}` | {scheme} | **{pct(r['ann_mean'])}** | {pct(r['ann_med'])} | "
                     f"{r['ann_std']*100:.1f}pp | {pct(r['ann_min'])} | "
                     f"{r['sharpe_mean']:.2f} | {r['sharpe_min']:.2f} | {pct(r['dd_mean'])} | {r['oos_mean']:.2f} |")

    L += ["", "## 判读（预先声明的判据）", ""]
    for mode in ("raw", "rank"):
        base = summ[f"{mode}/no_cap"]
        r = summ[f"{mode}/cap15"]
        d_ann = (r["ann_mean"] - base["ann_mean"]) * 100
        d_shp = r["sharpe_mean"] - base["sharpe_mean"]
        d_dd = (r["dd_mean"] - base["dd_mean"]) * 100
        win = (df[(df["mode"] == mode) & (df["scheme"] == "cap15")].sort_values("offset")["ann"].to_numpy()
               > df[(df["mode"] == mode) & (df["scheme"] == "no_cap")].sort_values("offset")["ann"].to_numpy())
        L.append(f"- **`{mode}` 口径**：行业≤15% 使年化均值 **{d_ann:+.1f}pp**、夏普均值 {d_shp:+.2f}、"
                 f"回撤均值 {d_dd:+.1f}pp、最差相位 {pct(r['ann_min'])} vs {pct(base['ann_min'])}、"
                 f"胜率 {win.mean()*100:.0f}%（{win.sum()}/{len(win)}）")
    L += ["", "- **判据**：行业约束是「降集中风险」的风控项，预期是均值略降/回撤改善的 trade-off。",
          "- 若两个口径下夏普均值下降 < 0.05 且回撤均值改善 → 值得保留（纯风控，代价小）；",
          "- 若夏普均值下降 > 0.10 且回撤未改善 → 否决（约束破坏 min_var 最优，得不偿失）；",
          "- 若 raw 与 rank 方向相反 → 疑似噪音，不采信。"]

    out_md = os.path.join(REPO, "reports", "industry_cap_ab.md")
    with open(out_md, "w") as f:
        f.write("\n".join(L) + "\n")
    with open(os.path.join(REPO, "outputs", "industry_cap_ab.json"), "w") as f:
        json.dump({"n_phase": args.n_phase, "anchor": ANCHOR, "cap": CAP,
                   "summary": summ, "rows": rows}, f, ensure_ascii=False, indent=2)
    print("\n" + "\n".join(L))
    print(f"\n✅ {out_md}")


if __name__ == "__main__":
    main()

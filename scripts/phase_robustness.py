#!/usr/bin/env python3
"""相位稳健性裁决实验：原始 z / winsorize / rank-normal，20 个相位
====================================================================================
要裁决的问题
  `sl.score_cross` 用原始 z-score 合成因子，因截面厚尾导致「6 因子等权」名不副实
  （ep_ttm 实际暴露 +3.98σ vs ln_mv −0.11σ，差几十倍）。
  上轮 winsorize_test.py 只跑了 4 个相位（offset 2/5/10/15），样本量不足以裁决：
      raw_z 均值 15.9%（离散 8.8pp，最差 10.1%）
      rank_normal 均值 13.0%（离散 2.9pp，最差 11.6%）
  本实验扩到**一个完整月度周期**（offset 0..19，即 2019-01 起头 20 个交易日
  各作一次锚点）—— 这回答的是实盘真问题：**每个月总得挑一天调仓，挑哪天影响多大**。

三个口径
  raw       原始截面 z-score（= 现行生产）
  winsor    z 裁剪到 ±3σ（保留尾部排序与存在性，只限制幅度）
  rank      秩→正态得分（Φ⁻¹((rank−0.5)/n)，完全等权化）

判读标准（预先声明，避免事后挑数）
  1. 比**20 相位均值**（单相位数字无意义）
  2. 比**离散度 / 最差相位**（实盘关心下沿）
  3. 比**风险调整后**（夏普、回撤）而非只看年化
  4. 若 winsor 是 Pareto 改进（均值不降且离散降）→ 应采纳；
     若 rank 均值降而离散也降 → 是 trade-off，须看夏普与最差相位定夺

用法: python3 scripts/phase_robustness.py [--n-phase 20] [--cases raw,winsor,rank]
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import sys

import numpy as np
import pandas as pd
from scipy.stats import norm

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import factor_eval as fe          # noqa: E402
import strategy_lib as sl         # noqa: E402
import factor_weight_scan as fws  # noqa: E402

ANCHOR, BACK_TO = "2019-01-01", "2016-01-01"
OOS = "2023-01-01"
PROD6 = ["ln_mv", "ep_ttm", "ivol_60", "sue_delta", "ret_20d_rev", "turnover_20"]
CAP = 3.0


class SectionZ(fws.Section):
    """与 fws.Section 同接口，只改截面 z 的构造口径。"""

    def __init__(self, *a, mode="raw", **kw):
        super().__init__(*a, **kw)
        self.mode = mode

    def zdf(self, t):
        if t in self._z:
            return self._z[t]
        zs = {}
        for n in self.names:
            fw = self.fwides[n]
            if t not in fw.index:
                self._z[t] = None
                return None
            s = fw.loc[t].dropna()
            s = s[s.index.isin(self.uni)]
            s = s[[c for c in s.index if self.list_dates.get(c) is not None
                   and t > pd.Timestamp(self.list_dates[c]) + pd.Timedelta(days=120)]]
            if len(s) < 50:
                self._z[t] = None
                return None
            if self.mode == "rank":
                zs[n] = pd.Series(norm.ppf((s.rank() - 0.5) / len(s)), index=s.index)
            else:
                sd = s.std(ddof=0)
                z = (s - s.mean()) / (sd if sd and sd > 0 else 1.0)
                if self.mode == "winsor":
                    z = z.clip(-CAP, CAP)
                zs[n] = z
        z = pd.DataFrame(zs)
        self._z[t] = z if len(z) >= 50 else None
        return self._z[t]


def pct(v):
    return "-" if v is None or not np.isfinite(v) else f"{v*100:.1f}%"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-phase", type=int, default=20)
    ap.add_argument("--cases", default="raw,winsor,rank")
    args = ap.parse_args()

    end = pd.Timestamp.today().strftime("%Y-%m-%d")
    conn = fe.get_conn()
    uni, list_dates = fe.load_universe_filter(conn)
    daily_ret = fe.load_daily_returns(conn, "2015-10-01", end)
    idx = daily_ret.index
    fw = {n: sl.load_factor(conn, n, "2015-06-01", end).astype("float32") for n in PROD6}
    conn.close()
    print(f"股票池 {len(uni)} | 因子 {PROD6}", flush=True)

    # 共享一个 Section 的 z 缓存（按 t 缓存，可跨相位复用），避免 20 相位重复算 z
    secs = {m: SectionZ(fw, daily_ret, uni, list_dates, mode=m)
            for m in args.cases.split(",") if m in ("raw", "winsor", "rank")}
    print(f"口径 {list(secs)} | 相位 offset 0..{args.n_phase-1}", flush=True)

    rows = []
    for off in range(args.n_phase):
        g = fws.offset_grid(idx, ANCHOR, off, end)
        rb = [t for t in g if t >= pd.Timestamp(ANCHOR)]
        for mode, sec in secs.items():
            nav, turn, _ = fws.backtest(sec, fws.w_equal, g, daily_ret, rb)
            s, so = fws.stats(nav), fws.stats(nav[nav.index >= pd.Timestamp(OOS)])
            rows.append({"offset": off, "mode": mode, "ann": s["ann"], "sharpe": s["sharpe"],
                         "dd": s["dd"], "oos_sharpe": so["sharpe"], "turn": turn})
            print(f"  off={off:2d} {mode:7s} 年化 {pct(s['ann'])} 夏普 {s['sharpe']:.2f} "
                  f"回撤 {pct(s['dd'])} OOS夏普 {so['sharpe']:.2f}", flush=True)
        gc.collect()

    df = pd.DataFrame(rows)
    L = ["# 相位稳健性裁决：原始 z / winsorize / rank-normal（20 相位）", "",
         f"> {pd.Timestamp.today().date()} | 组合层锁定生产口径 Top30 + min_var_cap10 + 60日协方差 + 单边成本 0.15%",
         f"> 相位 = 以 2019-01 起头 20 个交易日各自作锚点（等效「每月挑哪天调仓」的完整周期）",
         f"> 三个口径：raw = 原始 z-score（现行生产）｜winsor = z 裁剪到 ±{CAP:.0f}σ｜rank = 秩→正态得分", "",
         "## 汇总", "",
         "| 口径 | 年化均值 | 年化中位 | 年化离散 | 年化最差 | 年化最好 | 夏普均值 | 夏普最差 | 回撤均值 | OOS夏普均值 |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    summ = {}
    for mode in secs:
        d = df[df["mode"] == mode]
        r = {"ann_mean": d["ann"].mean(), "ann_med": d["ann"].median(),
             "ann_std": d["ann"].std(), "ann_min": d["ann"].min(), "ann_max": d["ann"].max(),
             "sharpe_mean": d["sharpe"].mean(), "sharpe_min": d["sharpe"].min(),
             "dd_mean": d["dd"].mean(), "oos_mean": d["oos_sharpe"].mean(),
             "anns": d.sort_values("offset")["ann"].tolist(),
             "sharpes": d.sort_values("offset")["sharpe"].tolist()}
        summ[mode] = r
        L.append(f"| `{mode}` | **{pct(r['ann_mean'])}** | {pct(r['ann_med'])} | "
                 f"{r['ann_std']*100:.1f}pp | {pct(r['ann_min'])} | {pct(r['ann_max'])} | "
                 f"{r['sharpe_mean']:.2f} | {r['sharpe_min']:.2f} | {pct(r['dd_mean'])} | {r['oos_mean']:.2f} |")

    L += ["", "## 逐相位年化", "",
          "| offset | " + " | ".join(f"`{m}`" for m in secs) + " |", "|---" * (len(secs) + 1) + "|"]
    for off in range(args.n_phase):
        cells = []
        for m in secs:
            v = df[(df["offset"] == off) & (df["mode"] == m)]["ann"]
            cells.append(pct(v.iloc[0]) if len(v) else "-")
        L.append(f"| {off} | " + " | ".join(cells) + " |")

    base = summ.get("raw")
    L += ["", "## 判读", ""]
    if base:
        for m in secs:
            if m == "raw":
                continue
            r = summ[m]
            L.append(f"- **`{m}` vs `raw`**：年化均值 {(r['ann_mean']-base['ann_mean'])*100:+.1f}pp、"
                     f"离散 {r['ann_std']*100:.1f}pp vs {base['ann_std']*100:.1f}pp、"
                     f"最差相位 {pct(r['ann_min'])} vs {pct(base['ann_min'])}、"
                     f"夏普均值 {r['sharpe_mean']:.2f} vs {base['sharpe_mean']:.2f}")
            win = (df[df["mode"] == m].sort_values("offset")["ann"].to_numpy()
                   > df[df["mode"] == "raw"].sort_values("offset")["ann"].to_numpy())
            L.append(f"  - 胜率：`{m}` 在 **{win.mean()*100:.0f}%**（{win.sum()}/{len(win)}）的相位上年化更高")
        if all(summ[m]["ann_mean"] <= base["ann_mean"] + 0.005 for m in secs if m != "raw"):
            L.append("- → **两个替代口径都没有提升均值**，若同时离散度也未显著下降，则维持现状（不动生产）")
        for m in secs:
            if m == "raw":
                continue
            if summ[m]["ann_std"] < base["ann_std"] * 0.7 and summ[m]["ann_min"] > base["ann_min"]:
                L.append(f"- → `{m}` 离散度与最差相位同时改善（{summ[m]['ann_std']*100:.1f}pp vs "
                         f"{base['ann_std']*100:.1f}pp；最差 {pct(summ[m]['ann_min'])} vs {pct(base['ann_min'])}）"
                         f"，**下沿更好但均值更低 = 风险偏好选择**，不是对错问题")

    outp = os.path.join(REPO, "reports", "phase_robustness.md")
    with open(outp, "w") as f:
        f.write("\n".join(L) + "\n")
    with open(os.path.join(REPO, "outputs", "phase_robustness.json"), "w") as f:
        json.dump({"n_phase": args.n_phase, "anchor": ANCHOR, "cap": CAP,
                   "summary": summ, "rows": rows}, f, ensure_ascii=False, indent=2)
    print("\n" + "\n".join(L))
    print(f"\n✅ {outp}")


if __name__ == "__main__":
    main()

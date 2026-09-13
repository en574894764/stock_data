#!/usr/bin/env python3
"""对照实验：score_cross 加 rank-normal 后，因子权重才名副其实
====================================================================================
起因（两个独立观察指向同一件事）
  1. 任务三归因：6 因子等权在 z 空间**实际并不等权** —— 截面 |z|max 高达 22-68σ
     （sue_gr 66σ / roe_lf 68σ / ep_ttm 22σ），极端值主导合成分数。
  2. 任务四：把 sue_delta 换成单季 SUE（单因子 IC 更高：+1.88% vs +1.38%）
     组合年化反而从 18.7% 崩到 13.7%（多相位均值 −4.7pp）——
     说明「单因子 IC 高」并不能预测「在组合里的贡献」，因为合成尺度被尾部绑架。

本实验
  把 sl.score_cross 的截面 z-score 换成**秩→正态得分 rank-normal**（单调变换，
  IC 完全不变，但合成尺度变了），其余口径全部锁定生产（Top30 + min_var_cap10 +
  60日协方差 + 单边成本 0.15% + 同相位网格 + 多相位稳健性检验）。

预期
  若 rank-normal 后替换不再崩（或基线本身变好），则证实：
  「因子等权」目前是假的，改一行 score_cross 就能拿到真实的信息增益。

用法: python3 scripts/winsorize_test.py [--no-phase]
"""
from __future__ import annotations

import argparse
import gc
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
import compute_qfa_sue as cq      # noqa: E402

ANCHOR, BACK_TO = "2019-01-01", "2016-01-01"
PHASE_OFFSETS = [2, 5, 10, 15]
PROD6 = ["ln_mv", "ep_ttm", "ivol_60", "sue_delta", "ret_20d_rev", "turnover_20"]
SWAP = [x if x != "sue_delta" else "sue_q_np" for x in PROD6]


class SectionRN(fws.Section):
    """与 fws.Section 同接口，唯一差别：截面 z 用 rank-normal 而非原始 z-score。

    秩→正态得分 z = Φ⁻¹((rank−0.5)/n)：严格标准化、无极端值、对单调变换不变。
    IC（Spearman）完全不受影响 —— 变的只是**多因子合成的相对尺度**。"""

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
            zs[n] = pd.Series(norm.ppf((s.rank() - 0.5) / len(s)), index=s.index)
        z = pd.DataFrame(zs)
        self._z[t] = z if len(z) >= 50 else None
        return self._z[t]


def fmt(v):
    return "-" if v is None or not np.isfinite(v) else f"{v*100:.1f}%"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-phase", action="store_true")
    args = ap.parse_args()

    end = pd.Timestamp.today().strftime("%Y-%m-%d")
    conn = fe.get_conn()
    uni, list_dates = fe.load_universe_filter(conn)
    daily_ret = fe.load_daily_returns(conn, "2015-10-01", end)
    idx = daily_ret.index
    grid = fws.build_grid(idx, ANCHOR, BACK_TO, end)
    rebal = [t for t in grid if t >= pd.Timestamp(ANCHOR)]
    need = sorted(set(PROD6) | set(SWAP))
    pg = [n for n in need if n != "sue_q_np"]
    fw = {n: sl.load_factor(conn, n, "2015-06-01", end).astype("float32") for n in pg}
    if "sue_q_np" in need:
        # 新因子尚未入库 → 从 income 在内存构造（省掉 11.8M 行 upsert）
        dsel = daily_ret.index
        ev = cq.load_quarterly(conn)
        ev["sue_q_np"] = cq.clip_series("sue_q_np", ev["sue_q_np"].to_numpy(dtype=float))
        fw["sue_q_np"] = (cq.pit_wide(ev, dsel, "sue_q_np")
                          .reindex(index=dsel, columns=daily_ret.columns).astype("float32"))
        del ev
        gc.collect()
    conn.close()
    print(f"股票池 {len(uni)} | 网格 {len(grid)} 期 | 回测 {len(rebal)} 期 | 因子 {need}")

    cases = [("raw_z", fws.Section, PROD6, "原始 z-score（= 现行生产）"),
             ("rank_normal", SectionRN, PROD6, "秩→正态得分"),
             ("raw_z_swap", fws.Section, SWAP, "原始 z + sue_delta→sue_q_np"),
             ("rn_swap", SectionRN, SWAP, "秩正态 + sue_delta→sue_q_np")]

    print("\n=== 主相位 ===")
    navs, res = {}, []
    for label, cls, flist, note in cases:
        sec = cls({n: fw[n] for n in flist}, daily_ret, uni, list_dates)
        nav, turn, _ = fws.backtest(sec, fws.w_equal, grid, daily_ret, rebal)
        s, so = fws.stats(nav), fws.stats(nav[nav.index >= pd.Timestamp("2023-01-01")])
        navs[label] = nav
        res.append({"label": label, "note": note, "factors": flist, "ann": s["ann"],
                    "sharpe": s["sharpe"], "dd": s["dd"], "oos_sharpe": so["sharpe"], "turn": turn})
        print(f"  {label:12s} 年化 {fmt(s['ann'])} 夏普 {s['sharpe']:.2f} 回撤 {fmt(s['dd'])} "
              f"OOS夏普 {so['sharpe']:.2f} 换手 {turn*100:.0f}%  [{note}]")
        del sec
        gc.collect()

    phase = {}
    if not args.no_phase:
        print(f"\n=== 多相位稳健性 offset {PHASE_OFFSETS} ===")
        for label, cls, flist, _ in cases:
            anns = [next(r["ann"] for r in res if r["label"] == label)]
            for off in PHASE_OFFSETS:
                g = fws.offset_grid(idx, ANCHOR, off, end)
                rb = [t for t in g if t >= pd.Timestamp(ANCHOR)]
                sec = cls({n: fw[n] for n in flist}, daily_ret, uni, list_dates)
                nav_o, _, _ = fws.backtest(sec, fws.w_equal, g, daily_ret, rb)
                anns.append(fws.stats(nav_o)["ann"])
                del sec
                gc.collect()
            phase[label] = {"anns": anns, "mean": float(np.mean(anns)), "worst": float(np.min(anns))}
            print(f"  {label:12s} 主 {fmt(anns[0])} | " +
                  " ".join(f"+{o}:{fmt(a)}" for o, a in zip(PHASE_OFFSETS, anns[1:])) +
                  f" | 均值 {fmt(phase[label]['mean'])} 最差 {fmt(phase[label]['worst'])}")

    L = ["# 对照实验：score_cross 加 rank-normal 是否让「因子等权」名副其实", "",
         f"> {pd.Timestamp.today().date()} | 组合层锁定生产口径 Top30 + min_var_cap10 + 60日协方差 + 单边成本 0.15%",
         f"> 同相位网格（锚 {ANCHOR}）| 回测 {len(rebal)} 期 | rank-normal = Φ⁻¹((rank−0.5)/n)，IC 不变、只改合成尺度", "",
         "## 主相位", "", "| 方案 | 说明 | 年化 | 夏普 | 回撤 | 样本外夏普 | 换手 |", "|---|---|---|---|---|---|---|"]
    for r in res:
        L.append(f"| `{r['label']}` | {r['note']} | **{fmt(r['ann'])}** | {r['sharpe']:.2f} | "
                 f"{fmt(r['dd'])} | {r['oos_sharpe']:.2f} | {r['turn']*100:.0f}% |")
    if phase:
        L += ["", "## 多相位稳健性（offset 2/5/10/15 交易日，报年化）", "",
              "| 方案 | 主相位 | " + " | ".join(f"+{o}日" for o in PHASE_OFFSETS) +
              " | 多相位均值 | 最差 |", "|---" * (4 + len(PHASE_OFFSETS)) + "|"]
        for label, p in phase.items():
            L.append(f"| `{label}` | " + " | ".join(fmt(a) for a in p["anns"]) +
                     f" | **{fmt(p['mean'])}** | {fmt(p['worst'])} |")
        bm = phase["raw_z"]["mean"]
        rnm = phase["rank_normal"]["mean"]
        L += ["", "## 判读", "",
              f"- 基线 原始z 多相位均值 **{fmt(bm)}** → rank-normal **{fmt(rnm)}**（{(rnm-bm)*100:+.1f}pp）",
              f"- 替换口径 原始z **{fmt(phase['raw_z_swap']['mean'])}** → rank-normal "
              f"**{fmt(phase['rn_swap']['mean'])}**（{(phase['rn_swap']['mean']-phase['raw_z_swap']['mean'])*100:+.1f}pp）"]
        d_raw = phase["raw_z_swap"]["mean"] - bm
        d_rn = phase["rn_swap"]["mean"] - rnm
        L.append(f"- 替换的劣化幅度：原始z **{d_raw*100:+.1f}pp** → rank-normal **{d_rn*100:+.1f}pp**")
        if rnm > bm + 0.01:
            L.append("- ✅ **rank-normal 提升基线** → 「因子等权」当前确实是假的，改 score_cross 有真实增益")
        elif abs(d_rn) < abs(d_raw):
            L.append("- ⚠ rank-normal 未提升基线，但**显著缓解了替换的劣化** → 证实劣化源自合成尺度被尾部绑架")
        else:
            L.append("- ❌ rank-normal 既未提升基线也未缓解劣化 → 替换劣化的原因不在合成尺度")
    outp = os.path.join(REPO, "reports", "winsorize_test.md")
    with open(outp, "w") as f:
        f.write("\n".join(L) + "\n")
    print("\n" + "\n".join(L))
    print(f"\n✅ {outp}")


if __name__ == "__main__":
    main()

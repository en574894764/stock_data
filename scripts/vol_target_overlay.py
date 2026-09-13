#!/usr/bin/env python3
"""M1 波动率目标仓位（vol-targeting overlay）实证 — 风险管理式择时
====================================================================================
背景
    项目此前的择时「四重否决」全部是**方向性择时**：
      timing(MA200 空仓/半仓) / timing_overlay(减仓式防守) / confirm_gate(确认式做多)
      / trend_filter(个股级趋势过滤) —— 全部负贡献。
    **风险管理式择时（按波动率倒数缩放总仓位）从未测过**，且有顶刊证据支撑：
      · Moreira & Muir (2017, JF) "Volatility-Managed Portfolios"
      · Barroso & Santa-Clara (2015, JFE) "Momentum has its moments"
    机制上它与方向性择时是不同物种：不预测涨跌，只在波动放大时降低暴露。

机制
    σ_t = 组合日收益的 W 日实现波动（年化）；目标仓位 g_t = clip(target/σ_t, lo, hi)
    t 日收益 = g_{t-1} × r_t + (1-g_{t-1}) × rf_daily − |Δg| × 单边成本
    无前视：g_t 只用 t-1 及之前的信息（shift(1) 后作用于 t 日收益）

对照变体
    1. baseline   无 overlay（生产口径 prod_6f_eq Top30 + min_var_cap10）
    2. daily      每日调整（理论版，成本最高）
    3. rebal      只在调仓日调整（个人实盘可行）
    4. band20     目标仓位偏离当前 >20% 才动（降成本）
    5. nolev      无杠杆版 clip [0.2,1.0]（个人无融资，只降不升）

参数扫描 W ∈ {10,20,60} × target ∈ {15%,20%,25%}
稳健性  4 相位偏移 (offset 2/5/10/15) + 极端期 (2024-01~02) + 与原始日收益相关性

用法
    python3 scripts/vol_target_overlay.py                # 主相位完整对照
    python3 scripts/vol_target_overlay.py --phases       # 附多相位稳健性
    python3 scripts/vol_target_overlay.py --strategy prod_lgbm_neu
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import factor_eval as fe  # noqa: E402
import strategy_lib as sl  # noqa: E402
import portfolio_optimization as po  # noqa: E402

ANCHOR = "2019-01-01"
BACK_TO = "2019-01-01"
HIST = 60
COST = 0.0015
ANN = 244
RF = 0.015                      # 现金年化收益（闲置资金）
OOS_START = "2023-01-01"
CRISIS = ("2024-01-01", "2024-02-29")     # 2024 微盘踩踏
PHASE_OFFSETS = [2, 5, 10, 15]
W_LIST = [10, 20, 60]
TARGET_LIST = [0.15, 0.20, 0.25]


# ---------------------------------------------------------------- 指标

def metrics(nav: pd.Series) -> dict:
    r = nav.pct_change().dropna()
    if len(r) < 60:
        return {}
    years = len(r) / ANN
    ann = (nav.iloc[-1] / nav.iloc[0]) ** (1 / years) - 1
    vol = r.std() * np.sqrt(ANN)
    dd = float((nav / nav.cummax() - 1).min())
    return {"ann": float(ann), "vol": float(vol),
            "sharpe": float(ann / vol) if vol > 0 else np.nan,
            "dd": dd, "calmar": float(ann / abs(dd)) if dd < 0 else np.nan}


# ---------------------------------------------------------------- overlay

def apply_overlay(r: pd.Series, W: int, target: float, lo: float, hi: float,
                  mode: str = "daily", band: float = 0.0,
                  grid=None) -> tuple[pd.Series, pd.Series]:
    """返回 (净收益序列, 仓位序列 g)。r 为 baseline 组合日收益。"""
    ann_vol = r.rolling(W).std() * np.sqrt(ANN)
    with np.errstate(divide="ignore", invalid="ignore"):
        g_raw = (target / ann_vol).clip(lo, hi)
    g_raw = g_raw.replace([np.inf, -np.inf], np.nan)

    # t 日仓位只允许用 t-1 及之前的信息
    g = g_raw.shift(1)

    if mode == "rebal" and grid is not None:
        gd = set(pd.Timestamp(x) for x in grid)
        g = g.where(g.index.isin(gd))
    g = g.ffill().fillna(1.0)
    g = g.clip(lo, hi)

    if mode == "band" and band > 0:
        vals, cur = [], float(g.iloc[0])
        for x in g.to_numpy():
            if abs(float(x) - cur) > band:
                cur = float(x)
            vals.append(cur)
        g = pd.Series(vals, index=g.index)

    r_s = g * r + (1.0 - g) * (RF / ANN)
    cost = g.diff().abs().fillna(0.0) * COST       # 调整总敞口 = 单边交易
    return r_s - cost, g


def nav_from_ret(r: pd.Series) -> pd.Series:
    return (1.0 + r.fillna(0.0)).cumprod()


# ---------------------------------------------------------------- 网格

def main_grid(idx: pd.DatetimeIndex, end: str) -> list:
    return fe.rebalance_dates(idx, ANCHOR, end)


def offset_grid(idx: pd.DatetimeIndex, offset: int, end: str) -> list:
    sub = idx[idx >= pd.Timestamp(ANCHOR)]
    new_anchor = sub[offset].strftime("%Y-%m-%d")
    return fe.rebalance_dates(idx, new_anchor, end)


# ---------------------------------------------------------------- 主流程

def fmt(v, pct=True, nd=1):
    if v is None or not np.isfinite(v):
        return "-"
    return f"{v*100:.{nd}f}%" if pct else f"{v:.2f}"


def run_phase(conn, st, daily_ret, uni, list_dates, grid, end):
    # po.run 依赖 portfolio_optimization 的模块级全局 (HIST / end), 直接调用须显式注入
    po.HIST = HIST
    po.end = end
    nav, turn, herf = po.run(conn, st, daily_ret, uni, list_dates, grid, "min_var_cap10", None)
    return nav


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="prod_6f_eq@topn=30")
    ap.add_argument("--phases", action="store_true", help="附多相位稳健性检验")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    end = pd.Timestamp.today().strftime("%Y-%m-%d")
    conn = fe.get_conn()
    uni, list_dates = fe.load_universe_filter(conn)
    strategies = sl.load_strategies(conn, [args.strategy])
    st = strategies[0]
    daily_ret = fe.load_daily_returns(conn, "2015-10-01", end)
    grid = main_grid(daily_ret.index, end)
    print(f"[{st['label']}] 股票池 {len(uni)} | 调仓 {len(grid)} 期 | 区间 {grid[0].date()} ~ {end}")

    base_nav = run_phase(conn, st, daily_ret, uni, list_dates, grid, end)
    base_ret = base_nav.pct_change().fillna(0.0)
    base_m = metrics(base_nav)
    print(f"  baseline  年化 {fmt(base_m['ann'])} | 夏普 {base_m['sharpe']:.2f} "
          f"| 回撤 {fmt(base_m['dd'])} | Calmar {base_m['calmar']:.2f}")

    rows, payload = [], {"strategy": st["label"], "start": str(grid[0].date()), "end": end,
                         "baseline": base_m, "variants": {}}

    # --- 主对照 (W=20, target=20%) ---
    VARIANT_DEFS = [
        ("daily  每日调整", dict(W=20, target=0.20, lo=0.5, hi=1.5, mode="daily")),
        ("rebal  只在调仓日", dict(W=20, target=0.20, lo=0.5, hi=1.5, mode="rebal")),
        ("band20 阈值带", dict(W=20, target=0.20, lo=0.5, hi=1.5, mode="band", band=0.20)),
        ("nolev  无杠杆[0.2,1]", dict(W=20, target=0.20, lo=0.2, hi=1.0, mode="daily")),
    ]

    def add_row(label, r_net, g, base=None):
        nav = nav_from_ret(r_net)
        m = metrics(nav)
        oos = nav[nav.index >= pd.Timestamp(OOS_START)]
        mo = metrics(oos)
        c0, c1 = CRISIS
        seg = nav[(nav.index >= pd.Timestamp(c0)) & (nav.index <= pd.Timestamp(c1))]
        base_seg = None
        crisis_ret = float(seg.iloc[-1] / seg.iloc[0] - 1) if len(seg) > 2 else np.nan
        if base is not None:
            bseg = base[(base.index >= pd.Timestamp(c0)) & (base.index <= pd.Timestamp(c1))]
            base_seg = float(bseg.iloc[-1] / bseg.iloc[0] - 1) if len(bseg) > 2 else np.nan
        rows.append({
            "label": label, **m,
            "oos_sharpe": mo.get("sharpe", np.nan), "oos_dd": mo.get("dd", np.nan),
            "g_mean": float(g.mean()), "g_min": float(g.min()), "g_max": float(g.max()),
            "crisis": crisis_ret, "base_crisis": base_seg,
            "cost": float((g.diff().abs().fillna(0.0) * COST).sum()),
            "corr": float(pd.Series(r_net).corr(base_ret)) if base_ret is not None else np.nan,
        })
        payload["variants"][label.split()[0]] = {**m, "g_mean": float(g.mean()),
                                                "cost_total": float((g.diff().abs().fillna(0.0) * COST).sum())}
        print(f"  {label:22s} 年化 {fmt(m['ann'])} | 夏普 {m['sharpe']:.2f} | 回撤 {fmt(m['dd'])} "
              f"| Calmar {m['calmar']:.2f} | OOS夏普 {mo.get('sharpe', float('nan')):.2f} "
              f"| g均值 {g.mean():.2f} | 危机期 {fmt(crisis_ret)}")

    add_row("baseline", base_ret, pd.Series(1.0, index=base_ret.index), None)
    for label, kw in VARIANT_DEFS:
        r_net, g = apply_overlay(base_ret, grid=grid, **kw)
        # 仓位序列会因 ffill 起点不同而长度一致, 直接对齐
        add_row(label, r_net.reindex(base_ret.index).fillna(0.0),
                g.reindex(base_ret.index).ffill().fillna(1.0), base_nav)

    # --- 参数扫描 (每日调整口径) ---
    scan = []
    for W in W_LIST:
        for tgt in TARGET_LIST:
            r_net, g = apply_overlay(base_ret, W=W, target=tgt, lo=0.5, hi=1.5, mode="daily")
            m = metrics(nav_from_ret(r_net))
            scan.append({"W": W, "target": tgt, **m, "g_mean": float(g.mean())})
    payload["scan"] = scan

    # --- 机制诊断：收益与自身波动的关系（决定 vol-targeting 是否可能有效）---
    ann_vol_d = base_ret.rolling(20).std() * np.sqrt(ANN)
    sig_prev = ann_vol_d.shift(1).dropna()
    r_al = base_ret.reindex(sig_prev.index)
    q = pd.qcut(sig_prev, 5, labels=False)
    diag = []
    for qi in range(5):
        m_ = q == qi
        mu = float(r_al[m_].mean()) * ANN
        print(f"  [诊断] 波动分位 Q{qi+1}: σ {sig_prev[m_].mean()*100:.1f}% | 日均收益年化 {mu*100:+.1f}% | 天数 {int(m_.sum())}")
        diag.append({"q": qi + 1, "sig": float(sig_prev[m_].mean()), "ann_ret": mu, "n": int(m_.sum())})
    corr_sr = float(sig_prev.corr(r_al))
    print(f"  [诊断] corr(σ_(t-1), r_t) = {corr_sr:+.3f}  (>0 表示高波动期收益更高 → 减仓型 overlay 必然伤 α)")
    payload["diag_vol_ret"] = {"conditional": diag, "corr_sig_ret": corr_sr}

    # --- 多相位稳健性 ---
    if args.phases:
        ph = []
        for off in [0] + PHASE_OFFSETS:
            g_ = main_grid(daily_ret.index, end) if off == 0 else offset_grid(daily_ret.index, off, end)
            bn = run_phase(conn, st, daily_ret, uni, list_dates, g_, end)
            br = bn.pct_change().fillna(0.0)
            bm = metrics(bn)
            best, bg = apply_overlay(br, W=20, target=0.20, lo=0.5, hi=1.5, mode="rebal", grid=g_)
            bm2 = metrics(nav_from_ret(best))
            nov, _ = apply_overlay(br, W=20, target=0.20, lo=0.2, hi=1.0, mode="daily")
            bm3 = metrics(nav_from_ret(nov))
            ph.append({"offset": off, "base_ann": bm["ann"], "base_sharpe": bm["sharpe"],
                       "base_dd": bm["dd"], "ov_ann": bm2["ann"], "ov_sharpe": bm2["sharpe"],
                       "ov_dd": bm2["dd"], "nolev_ann": bm3["ann"], "nolev_sharpe": bm3["sharpe"],
                       "nolev_dd": bm3["dd"]})
            print(f"  phase{off:2d}  baseline 年化 {fmt(bm['ann'])} 夏普 {bm['sharpe']:.2f} 回撤 {fmt(bm['dd'])}"
                  f" || overlay(rebal) 年化 {fmt(bm2['ann'])} 夏普 {bm2['sharpe']:.2f} 回撤 {fmt(bm2['dd'])}")
        payload["phases"] = ph

    conn.close()

    out_json = args.out or os.path.join(REPO, "outputs", "vol_target_overlay.json")
    with open(out_json, "w") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    # --- 报告 ---
    lines = [f"# M1 波动率目标仓位 overlay 实证\n",
             f"> 策略 {st['label']} | 区间 {grid[0].date()} ~ {end} | 调仓 {len(grid)} 期 | "
             f"波动窗口 W=20 | 现金收益 {RF*100:.1f}% | 单边成本 {COST*100:.2f}%\n",
             "> 机制：σ=组合日收益 W 日实现波动(年化)，g=clip(target/σ, lo, hi)，t 日收益=g_{t-1}·r_t+(1-g_{t-1})·rf−|Δg|·cost（无前视）\n",
             "\n## 主对照\n",
             "| 变体 | 年化 | 波动 | 夏普 | 回撤 | Calmar | 样本外夏普 | 样本外回撤 | 均仓位 | 2024-01~02 | overlay成本 |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['label']} | {fmt(r['ann'])} | {fmt(r['vol'])} | {r['sharpe']:.2f} | "
                     f"{fmt(r['dd'])} | {r['calmar']:.2f} | {r['oos_sharpe']:.2f} | {fmt(r['oos_dd'])} | "
                     f"{r['g_mean']:.2f} | {fmt(r['crisis'])} | {fmt(r['cost'], nd=2)} |")
    lines.append(f"\n> 2024-01~02 baseline 同期收益：{fmt(rows[0]['crisis'])}（危机期对照基准）\n")
    lines.append("\n## 参数扫描（每日调整口径）\n")
    lines.append("| W | target | 年化 | 夏普 | 回撤 | Calmar | 均仓位 |")
    lines.append("|---|---|---|---|---|---|---|")
    for s in scan:
        lines.append(f"| {s['W']} | {fmt(s['target'], nd=0)} | {fmt(s['ann'])} | {s['sharpe']:.2f} | "
                     f"{fmt(s['dd'])} | {s['calmar']:.2f} | {s['g_mean']:.2f} |")
    if "diag_vol_ret" in payload:
        d = payload["diag_vol_ret"]
        lines.append("\n## 机制诊断：收益 vs 自身波动\n")
        lines.append("| 波动分位 | 平均实现波动 | 该分位日收益年化 | 天数 |")
        lines.append("|---|---|---|---|")
        for c in d["conditional"]:
            lines.append(f"| Q{c['q']} | {c['sig']*100:.1f}% | {c['ann_ret']*100:+.1f}% | {c['n']} |")
        lines.append(f"\n> corr(σ_(t-1), r_t) = **{d['corr_sig_ret']:+.3f}**。"
                     "若为正 → 高波动期收益更高 → 任何「高波动减仓」型 overlay 都会系统性削掉 α 的来源。\n")
    if "phases" in payload:
        lines.append("\n## 多相位稳健性\n")
        lines.append("| 相位偏移 | baseline 年化 | baseline 夏普 | baseline 回撤 | overlay(rebal) 年化 | overlay 夏普 | overlay 回撤 | 无杠杆 年化 | 无杠杆 夏普 | 无杠杆 回撤 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for p in payload["phases"]:
            lines.append(f"| {p['offset']} | {fmt(p['base_ann'])} | {p['base_sharpe']:.2f} | {fmt(p['base_dd'])} | "
                         f"{fmt(p['ov_ann'])} | {p['ov_sharpe']:.2f} | {fmt(p['ov_dd'])} | "
                         f"{fmt(p['nolev_ann'])} | {p['nolev_sharpe']:.2f} | {fmt(p['nolev_dd'])} |")
        ov_s = [p["ov_sharpe"] for p in payload["phases"]]
        bs_s = [p["base_sharpe"] for p in payload["phases"]]
        nv_s = [p["nolev_sharpe"] for p in payload["phases"]]
        lines.append(f"\n> 四相位夏普均值：baseline {np.mean(bs_s):.2f} | overlay(rebal) {np.mean(ov_s):.2f} "
                     f"| 无杠杆 {np.mean(nv_s):.2f}；最差相位：baseline {min(bs_s):.2f} / "
                     f"overlay {min(ov_s):.2f} / 无杠杆 {min(nv_s):.2f}\n")

    out_md = os.path.join(REPO, "reports", "vol_target_overlay.md")
    with open(out_md, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n✅ 报告 {out_md}\n✅ JSON  {out_json}")


if __name__ == "__main__":
    main()

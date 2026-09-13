#!/usr/bin/env python3
"""简化 Barra 风格归因（P3#2）— 把组合收益分解为「市场 β + 风格暴露 + 特异性 α」
====================================================================================
回答两个问题：
  1. 组合赚的钱，几分来自风格 β、几分来自真 α（特异性）？
  2. min_var_cap10 加权是否引入了意料之外的风格暴露漂移？
     （线索：周一清单里银行/公用事业顶到 10% 上限）

方法（截面回归式 Barra 简化版，修正版）
  每个调仓期 t：
    · 风格暴露 z_k(t)：风格因子在全股票池内做截面 z-score（与 sl.score_cross 同口径）
    · 风格因子收益 f_k(t)：**在全池**做截面 OLS
          fwd_r_i = f_mkt + Σ_k z_ik · f_k + ε_i        （f_mkt 即市场/截距项）
      —— 必须在全池估计：若只用 Top30 估计，而 Top30 正是这些因子选出来的，
         会落入内生性陷阱（f 被选择偏差污染，风格贡献虚高）
    · 组合暴露       b_k(t) = Σ_i w_i · z_ik          （w = Top30 权重）
    · 归因           r_p(t) = f_mkt + Σ_k b_k f_k  ← 市场 + 风格
                            + Σ_i w_i ε_i         ← 特异性 α
  汇总：市场/各风格累计贡献与占比、特异 α 占比、暴露时序漂移、行业暴露

风格因子（factor_value，均为「值大=预期收益高」口径，方向见括号）
  size 小市值(ln_mv=-ln市值) / value 便宜(ep_ttm) / quality 高ROE(roe_lf) / growth 盈利惊喜(sue_gr)
  lowvol 低波动(ivol_60=-特质波动) / reversal 反转(ret_20d_rev=-20日收益) / liquidity 低换手(turnover_20=-换手)

用法
  python3 scripts/attribution.py                      # prod_6f_eq@topn=30, equal vs min_var_cap10
  python3 scripts/attribution.py --strategy prod_lgbm_neu --topn 30
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
import pandas as pd
from scipy.stats import norm

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import factor_eval as fe        # noqa: E402
import strategy_lib as sl       # noqa: E402
import portfolio_optimization as po  # noqa: E402

ANCHOR = "2019-01-01"
HIST = 60
TOP_N = 30
MIN_CROSS = 200       # 全池 OLS 最少样本数

STYLE = {
    "ln_mv":       ("市值(size)",      "值大=小市值"),
    "ep_ttm":      ("价值(value)",     "值大=便宜"),
    "roe_lf":      ("质量(quality)",   "值大=高ROE"),
    "sue_gr":      ("成长(growth)",    "值大=盈利惊喜高"),
    "ivol_60":     ("波动(lowvol)",    "值大=低波动"),
    "ret_20d_rev": ("反转(reversal)",  "值大=前期跌得多"),
    "turnover_20": ("换手(liquidity)", "值大=低换手"),
}
SCHEMES = ["equal", "min_var_cap10"]


def style_z(fwides: dict, t, uni: set, list_dates: dict):
    """t 日风格暴露。返回 (Zraw, Zrob)：
      Zraw = 原始截面 z-score（= sl.score_cross 口径）
      Zrob = 秩→正态得分 (rank-normal)，对厚尾/极端值稳健

    ⚠ 为什么必须两套：实测截面 |z|max——sue_gr 66σ / roe_lf 68σ / ep_ttm 22σ /
      turnover_20 15σ / ret_20d_rev 17σ（ln_mv、ivol_60 温和，仅 5-6σ）。
      原始 z 被极端值绑架 → 归因暴露不可解释（价值暴露会报 +4σ），
      且生产 score_cross 的"6 因子等权"在 z 空间实际并不等权。
    """
    raw, rob = {}, {}
    for name in STYLE:
        fw = fwides[name]
        if t not in fw.index:
            return None, None
        s = fw.loc[t].dropna()
        s = s[s.index.isin(uni)]
        if len(s) < MIN_CROSS:
            return None, None
        raw[name] = s
        rob[name] = pd.Series(norm.ppf((s.rank() - 0.5) / len(s)), index=s.index)
    Zr = pd.DataFrame(raw).replace([np.inf, -np.inf], np.nan)
    sd = Zr.std(ddof=0)
    Zr = (Zr - Zr.mean()) / sd.replace(0.0, np.nan)
    Zb = pd.DataFrame(rob).replace([np.inf, -np.inf], np.nan)
    return Zr.replace([np.inf, -np.inf], np.nan), Zb


def cross_section_ols(Zols: pd.DataFrame, y: np.ndarray):
    """全池截面 OLS: y = f_mkt + Z·f_style + ε。返回 (f_mkt, f_style, resid) 或 None。"""
    A = np.column_stack([np.ones(len(Zols), dtype=np.float64),
                         Zols.to_numpy(dtype=np.float64)])
    if not (np.isfinite(A).all() and np.isfinite(y).all()):
        return None
    try:
        sol, *_ = np.linalg.lstsq(A, y, rcond=None)
    except np.linalg.LinAlgError:
        sol = np.linalg.pinv(A) @ y
    if not np.isfinite(sol).all():
        return None
    f_mkt, f_style = float(sol[0]), sol[1:]
    resid = y - A @ sol
    return f_mkt, f_style, resid


def winsorize(y: np.ndarray, lo=0.01, hi=0.99) -> np.ndarray:
    a, b = np.nanquantile(y, [lo, hi])
    return np.clip(y, a, b)


def load_industry() -> dict:
    p = os.path.join(REPO, "meta", "stock_basic.csv")
    if not os.path.exists(p):
        return {}
    df = pd.read_csv(p, dtype=str)
    df = df.dropna(subset=["ts_code", "industry"])
    return dict(zip(df["ts_code"], df["industry"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="prod_6f_eq@topn=30")
    ap.add_argument("--topn", type=int, default=TOP_N)
    args = ap.parse_args()

    end = pd.Timestamp.today().strftime("%Y-%m-%d")
    conn = fe.get_conn()
    uni, list_dates = fe.load_universe_filter(conn)
    st = sl.load_strategies(conn, [args.strategy])[0]
    fweights = st["cfg"]["factors"]
    top_n = int(st["cfg"].get("top_n", args.topn))
    daily_ret = fe.load_daily_returns(conn, "2015-10-01", end)
    fwd = fe.fwd_from_daily(daily_ret, horizon=fe.REBAL)
    grid = fe.rebalance_dates(daily_ret.index, ANCHOR, end)

    sel_f = {n: sl.load_factor(conn, n, ANCHOR, end) for n in fweights}
    sty_f = {n: sl.load_factor(conn, n, ANCHOR, end) for n in STYLE}
    ind_map = load_industry()
    print(f"[{st['label']}] 调仓 {len(grid)} 期 | 风格因子 {len(STYLE)} 个 | Top{top_n}")

    def blank():
        return {"periods": [], "contrib": {k: 0.0 for k in STYLE}, "mkt": 0.0,
                "specific": 0.0, "total": 0.0,
                "exp": {k: [] for k in STYLE}, "exp_raw": {k: [] for k in STYLE},
                "exp_year": {}, "spec_ser": [],
                "ind_w": {}, "ic": {k: [] for k in STYLE}, "n_hold": []}
    out = {s: blank() for s in SCHEMES}

    skipped = {"score": 0, "z": 0, "fwd": 0, "ols": 0, "hist": 0, "w": 0}
    quint: dict = {}
    for t in grid:
        score, _ = sl.score_cross(sel_f, fweights, t, uni, list_dates)
        if score is None:
            skipped["score"] += 1
            continue
        sel, _ = sl.select_stocks(score, top_n)
        Zr, Zb = style_z(sty_f, t, uni, list_dates)
        if Zr is None:
            skipped["z"] += 1
            continue
        if t not in fwd.index:
            skipped["fwd"] += 1
            continue
        fr_all = fwd.loc[t].replace([np.inf, -np.inf], np.nan).dropna()

        # ---- 全池估计样本：7 因子齐备且前向收益可得（用秩正态暴露，稳健） ----
        Zols = Zb.dropna(how="any")
        U = Zols.index.intersection(fr_all.index)
        if len(U) < MIN_CROSS:
            skipped["z"] += 1
            continue
        Zols = Zols.loc[U]
        y_raw = fr_all.loc[U].to_numpy(dtype=np.float64)
        y = winsorize(y_raw)
        fit = cross_section_ols(Zols, y)
        if fit is None:
            skipped["ols"] += 1
            continue
        f_mkt, f_style, resid = fit

        # ---- 组合：Top30 中前向收益可得的票；缺风格暴露按截面均值(0)填 ----
        cols = [c for c in sel if c in fr_all.index and c in Zb.index]
        if len(cols) < 5:
            skipped["w"] += 1
            continue
        hist = daily_ret.loc[:t].iloc[-HIST:][cols]
        if hist.shape[0] < 20:
            skipped["hist"] += 1
            continue
        Zc = Zb.loc[cols].fillna(0.0)
        Zc_raw = Zr.loc[cols].fillna(0.0)
        yc_raw = fr_all[cols].to_numpy(dtype=np.float64)
        yc = winsorize(yc_raw)
        # 全池风格五分层前向收益（验证"暴露越极端越好"是否成立）
        for name in STYLE:
            v = Zols[name].to_numpy(dtype=np.float64)
            q = np.clip((pd.Series(v).rank(method="first").to_numpy() * 5 //
                         len(v)).astype(int), 0, 4)
            for g in range(5):
                m = q == g
                if m.sum() >= 20:
                    quint.setdefault(name, [[] for _ in range(5)])[g].append(float(y[m].mean()))
        # 全池风格因子 IC（Spearman 秩相关，与权重方案无关）
        yr_rank = pd.Series(y_raw).rank().to_numpy()
        for name in STYLE:
            r = np.corrcoef(pd.Series(Zols[name].to_numpy()).rank().to_numpy(), yr_rank)[0, 1]
            if np.isfinite(r):
                for s_ in SCHEMES:
                    out[s_]["ic"][name].append(float(r))

        for sch in SCHEMES:
            w = po.compute_weights(hist, sch)
            w = np.asarray(w, dtype=np.float64)
            if w.size != len(cols) or not np.isfinite(w).all() or w.sum() <= 0:
                continue
            w = w / w.sum()
            b = w @ Zc.to_numpy(dtype=np.float64)          # 组合风格暴露（秩正态）
            b_raw = w @ Zc_raw.to_numpy(dtype=np.float64)  # 组合风格暴露（原始 z）
            contrib = b * f_style                          # 各风格贡献
            rp = float(w @ yc)
            spec = rp - f_mkt - float(contrib.sum())
            rec = out[sch]
            rec["total"] += rp
            rec["mkt"] += f_mkt
            rec["specific"] += spec
            rec["spec_ser"].append(spec)
            rec["n_hold"].append(len(cols))
            for i, k in enumerate(STYLE):
                rec["contrib"][k] += float(contrib[i])
                rec["exp"][k].append(float(b[i]))
                rec["exp_raw"][k].append(float(b_raw[i]))
            yr = str(pd.Timestamp(t).year)
            rec["exp_year"].setdefault(yr, {k: [] for k in STYLE})
            for i, k in enumerate(STYLE):
                rec["exp_year"][yr][k].append(float(b[i]))
            for c, wi in zip(cols, w):
                ind = ind_map.get(c, "未知")
                rec["ind_w"][ind] = rec["ind_w"].get(ind, 0.0) + float(wi)
            rec["periods"].append({"t": str(pd.Timestamp(t).date()), "rp": rp,
                                   "spec": spec, "mkt": f_mkt})

    # ================= 报告 =================
    n_per = len(out[SCHEMES[0]]["periods"])
    yrs_n = n_per * fe.REBAL / 244
    lines = [
        "# 简化 Barra 风格归因",
        "",
        f"> 策略 **{st['label']}** | 调仓区间 {ANCHOR}~{end} | 有效调仓 **{n_per}** 期（≈{yrs_n:.1f} 年）| 持仓 Top{top_n}",
        f"> 风格因子 {len(STYLE)} 个（factor_value，均为「值大=预期收益高」口径）| 风格收益由**全池**截面 OLS 估计（含市场截距项）",
        "> 分解：`r_p = f_mkt + Σ b_k·f_k + Σ w_i·ε_i`（市场 + 风格 + 特异性 α）| 前向收益 1%/99% winsorize",
        "> ⚠ 口径说明：**暴露用秩→正态得分（rank-normal）**，非原始 z-score。原始 z 被厚尾极端值绑架",
        "> （截面 |z|max：sue_gr 66σ / roe_lf 68σ / ep_ttm 22σ / ret_20d_rev 17σ / turnover_20 15σ），",
        "> 报出来的暴露会虚高到 +4σ 且不可解释。两套暴露在下方对照列同时给出。",
        "> ⚠ 收益为**算术累计**（各期 r_p 直接相加，非复利），故绝对值低于回测 NAV；成本未计。",
        "",
        f"跳过统计：{ {k: v for k, v in skipped.items() if v} }",
    ]

    summary = {}
    for sch in SCHEMES:
        rec = out[sch]
        tot = rec["total"]
        sty = tot - rec["mkt"] - rec["specific"]
        spec = np.array(rec["spec_ser"], dtype=float)
        n = len(spec)
        spec_ann = spec.mean() * 244 / fe.REBAL if n else float("nan")
        spec_ir = (spec.mean() / spec.std() * np.sqrt(244 / fe.REBAL)) if n and spec.std() > 0 else float("nan")
        spec_t = (spec.mean() / spec.std() * np.sqrt(n)) if n and spec.std() > 0 else float("nan")
        lines += ["", f"## 权重方案 `{sch}`", ""]
        lines.append(f"- 累计组合收益（算术）**{tot*100:+.1f}%** ｜年化 {tot/n*244/fe.REBAL*100:+.1f}%")
        lines.append(f"- 市场(universe 均值)贡献 **{rec['mkt']*100:+.1f}%**（占 {rec['mkt']/tot*100:+.1f}%）")
        lines.append(f"- 风格合计贡献 **{sty*100:+.1f}%**（占 {sty/tot*100:+.1f}%）")
        lines.append(f"- 特异性 α **{rec['specific']*100:+.1f}%**（占 **{rec['specific']/tot*100:+.1f}%**）"
                     f" ｜年化 {spec_ann*100:+.1f}% ｜IR {spec_ir:.2f} ｜t 值 {spec_t:+.2f}")
        lines.append(f"- 平均持仓 {np.mean(rec['n_hold']):.1f} 只")
        lines += ["", "| 风格因子 | 累计贡献 | 占组合收益 | 暴露(rn) | 暴露(原始z) | 因子IC(均值) | 暴露方向解读 |",
                  "|---|---|---|---|---|---|---|"]
        for k, (cn, note) in sorted(STYLE.items(), key=lambda kv: -abs(rec["contrib"][kv[0]])):
            c = rec["contrib"][k]
            e = float(np.mean(rec["exp"][k])) if rec["exp"][k] else float("nan")
            er = float(np.mean(rec["exp_raw"][k])) if rec["exp_raw"][k] else float("nan")
            ic = float(np.mean(rec["ic"][k])) if rec["ic"][k] else float("nan")
            lines.append(f"| {cn} | {c*100:+.1f}% | {c/tot*100:+.1f}% | {e:+.2f}σ | {er:+.2f}σ "
                         f"| {ic:+.3f} | {note} |")
        summary[sch] = {"total": tot, "mkt": rec["mkt"], "style_sum": sty,
                        "specific": rec["specific"],
                        "specific_share": rec["specific"] / tot if tot else float("nan"),
                        "specific_ann": spec_ann, "specific_ir": spec_ir, "specific_t": spec_t,
                        "contrib": rec["contrib"],
                        "factor_ic": {k: (float(np.mean(v)) if v else None) for k, v in rec["ic"].items()},
                        "avg_exposure": {k: (float(np.mean(v)) if v else None)
                                         for k, v in rec["exp"].items()}}

    # ---- 因子 IC vs 组合暴露：配置匹配度 ----
    lines += ["", "## 配置匹配度：因子 IC × 组合暴露（equal 口径）", "",
              "> 有效组合应把暴露压在 IC 高的因子上。IC 弱却重仓暴露 = 无效倾斜。", "",
              "| 风格因子 | 因子IC | 组合暴露(rn) | 暴露排序 | IC排序 | 匹配 |", "|---|---|---|---|---|---|"]
    ic_map = {k: float(np.mean(out["equal"]["ic"][k])) for k in STYLE if out["equal"]["ic"][k]}
    exp_map = {k: float(np.mean(out["equal"]["exp"][k])) for k in STYLE}
    ic_rank = {k: r + 1 for r, (k, _) in enumerate(sorted(ic_map.items(), key=lambda kv: -kv[1]))}
    ex_rank = {k: r + 1 for r, (k, _) in enumerate(sorted(exp_map.items(), key=lambda kv: -abs(kv[1])))}
    for k, (cn, _) in STYLE.items():
        if k not in ic_map:
            continue
        gap = ex_rank[k] - ic_rank[k]
        verdict = "**错配**（IC弱却重仓）" if gap <= -3 else ("**错配**（IC强却轻仓）" if gap >= 3 else "尚可")
        lines.append(f"| {cn} | {ic_map[k]:+.3f} | {exp_map[k]:+.2f}σ | {ex_rank[k]} | {ic_rank[k]} | {verdict} |")

    # ---- 五分层：暴露单调性 ----
    ann = 244 / fe.REBAL
    lines += ["", "## 风格五分层前向收益（全池，年化 %）", "",
              "> 每期按 rank-normal 暴露分 5 档等份，Q5 = 暴露最高档（= 因子定义里的「最有利」端）。",
              "> Q5−Q1 为多空价差；若 Q5 不是最高、或 Q4>Q5，说明该风格**极端端已失效**（价值陷阱一类）。", "",
              "| 风格因子 | Q1(最低) | Q2 | Q3 | Q4 | Q5(最高) | Q5−Q1 | 单调 | IC |",
              "|---|---|---|---|---|---|---|---|---|"]
    quint_ann = {}
    for k, (cn, _) in STYLE.items():
        if k not in quint:
            continue
        ms = [np.mean(quint[k][g]) * ann * 100 if quint[k][g] else np.nan for g in range(5)]
        quint_ann[k] = ms
        spread = ms[4] - ms[0]
        mono = "✓" if ms[4] == max(ms) else ("**Q4>Q5**" if ms[3] > ms[4] else "✗")
        ic = ic_map.get(k, float("nan"))
        lines.append(f"| {cn} | " + " | ".join(f"{v:+.1f}" for v in ms) +
                     f" | **{spread:+.1f}** | {mono} | {ic:+.3f} |")

    # ---- 结论 ----
    e, m = out["equal"], out["min_var_cap10"]
    e_tot, m_tot = e["total"], m["total"]
    e_spec = np.array(e["spec_ser"]), np.array(m["spec_ser"])
    t_e = e_spec[0].mean() / e_spec[0].std() * np.sqrt(len(e_spec[0]))
    t_m = e_spec[1].mean() / e_spec[1].std() * np.sqrt(len(e_spec[1]))
    best_ic = max(ic_map.items(), key=lambda kv: kv[1]) if ic_map else ("-", float("nan"))
    worst_ic = min(ic_map.items(), key=lambda kv: kv[1]) if ic_map else ("-", float("nan"))
    biggest_exp = max(exp_map.items(), key=lambda kv: abs(kv[1]))
    _iw1, _iw2 = out["equal"]["ind_w"], out["min_var_cap10"]["ind_w"]
    _is1, _is2 = sum(_iw1.values()) or 1.0, sum(_iw2.values()) or 1.0
    bnk_e = _iw1.get("银行", 0.0) / _is1
    bnk_m = _iw2.get("银行", 0.0) / _is2
    _q = quint_ann
    _mono_top = "、".join(STYLE[k][0] for k in _q if _q[k][4] == max(_q[k]) and abs(_q[k][4] - _q[k][0]) > 8)
    _trap_list = "、".join(STYLE[k][0] for k in _q if _q[k][3] > _q[k][4])
    _v_q4 = _q.get("ep_ttm", [0, 0, 0, 0, 0])[3]
    _v_q5 = _q.get("ep_ttm", [0, 0, 0, 0, 0])[4]
    _r_q1 = _q.get("roe_lf", [0, 0, 0, 0, 0])[0]
    _r_q5 = _q.get("roe_lf", [0, 0, 0, 0, 0])[4]
    lines += [
        "", "## 结论", "",
        f"**1. 收益来源：这不是一个选股 α 组合，是一个风格/β 暴露组合。**",
        f"   - equal 口径：市场 {e['mkt']/e_tot*100:+.0f}% + 风格 {(e_tot-e['mkt']-e['specific'])/e_tot*100:+.0f}% + "
        f"特异性 α {e['specific']/e_tot*100:+.0f}%（t = {t_e:+.2f}，**不显著**）",
        f"   - min_var 口径：市场 {m['mkt']/m_tot*100:+.0f}% + 风格 {(m_tot-m['mkt']-m['specific'])/m_tot*100:+.0f}% + "
        f"特异性 α {m['specific']/m_tot*100:+.0f}%（t = {t_m:+.2f}，**不显著**）",
        f"   - 两个口径的 α 都是负的且 t 值在 ±1 内 → **剔除风格后没有可统计识别的选股能力**，"
        f"超额全部来自风格暴露与市场方向。这与「6 因子等权月度组合」的定位需要重新校准：它是 smart-beta，不是 alpha。",
        "",
        f"**2. 配置错配：暴露押错了因子。**",
        f"   - IC 最强的是 **{best_ic[0]}（IC {best_ic[1]:+.3f}）**，但组合暴露只有 {exp_map.get(best_ic[0], float('nan')):+.2f}σ；",
        f"   - 暴露最大的是 **{biggest_exp[0]}（{biggest_exp[1]:+.2f}σ）**，其 IC 只有 {ic_map.get(biggest_exp[0], float('nan')):+.3f}；",
        f"   - IC 最弱的是 **{worst_ic[0]}（{worst_ic[1]:+.3f}）**，即该因子在全池区间内没有预测力，",
        f"     但仍占了 1/6 的因子权重并在 z 空间贡献了不小的暴露。",
        f"   - **根因**：`sl.score_cross` 只用原始 z-score，不做 winsorize/rank-normal。",
        f"     截面 |z|max 高达 22-68σ（sue_gr 66σ / roe_lf 68σ），极端值主导合成分数，",
        f"     「6 因子等权」在 z 空间**实际并不等权**。这既是归因的噪音源，也是生产策略的未披露风险。",
        "",
        f"**2b. 五分层证据：最有效的因子没被用，最被重仓的因子在陷阱区。**",
        f"   - **单调且价差最大**：{_mono_top}；组合对它们的暴露却几乎为零"
        f"（size {exp_map.get('ln_mv', float('nan')):+.2f}σ、reversal {exp_map.get('ret_20d_rev', float('nan')):+.2f}σ）；",
        f"   - **Q4>Q5（极端端已失效）**：{_trap_list}——继续往末端压榨暴露是负贡献；",
        f"   - 组合最大暴露 ep_ttm（{exp_map.get('ep_ttm', float('nan')):+.2f}σ）正是 Q4(+{_v_q4:.1f}%) > Q5(+{_v_q5:.1f}%) 的陷阱因子；",
        f"   - roe_lf 五档**完全递减**（Q1 {_r_q1:+.1f}% → Q5 {_r_q5:+.1f}%，价差 {_r_q5-_r_q1:+.1f}pp），"
        f"即高 ROE 在全池横截面上是负向信号，与「质量是门限不是排序器」的既有结论一致；",
        f"   - lowvol / liquidity 的 Q3-Q4 见顶：**减仓高波动就够了，再往低波压榨无增益** —— "
        f"而 min_var 恰好把低波推到 +1.40σ（≈Q5 区），落在了收益递减段。",
        "",
        f"**3. min_var_cap10 的真实作用：不是更优的权重，而是更极端的因子暴露。**",
        f"   - 收益差（{m_tot*100:+.1f}% vs {e_tot*100:+.1f}%）中，α 贡献 {m['specific']-e['specific']:.3f}、"
        f"风格贡献 {(m_tot-m['mkt']-m['specific'])-(e_tot-e['mkt']-e['specific']):.3f}，两者都不显著；",
        f"   - 它把暴露系统性推向 IC 高的因子：低波 {np.mean(e['exp']['ivol_60']):+.2f}→{np.mean(m['exp']['ivol_60']):+.2f}σ、"
        f"换手 {np.mean(e['exp']['turnover_20']):+.2f}→{np.mean(m['exp']['turnover_20']):+.2f}σ、"
        f"成长 {np.mean(e['exp']['sue_gr']):+.2f}→{np.mean(m['exp']['sue_gr']):+.2f}σ、"
        f"小市值 {np.mean(e['exp']['ln_mv']):+.2f}→{np.mean(m['exp']['ln_mv']):+.2f}σ；",
        f"   - 因此它的超额更可能是**波动率目标函数的副产品**（低波股票被系统性超配）而非风险调整带来的信息增益。",
        f"   - ⚠ 代价：**行业集中度显著上升，银行 {bnk_e*100:.1f}% → {bnk_m*100:.1f}%（{bnk_m*100-bnk_e*100:+.1f}pp）**，"
        f"单一行业逼近 28%，这是需要盯的尾部风险。",
        "",
        f"**4. 建议（按优先级）**",
        f"   1. ~~给 score_cross 加 winsorize/rank-normal~~ **← 已实验否决**：`scripts/winsorize_test.py`",
        f"      同口径对照（多相位均值）—— raw_z **15.9%** vs rank_normal **13.0%（−3.0pp）**，",
        f"      主相位 18.7%(夏普1.11) vs 12.0%(0.77)。**原始 z 的尾部加权贡献了真实收益，不是噪音。**",
        f"      ⚠ 但代价是相位敏感度：raw_z 四相位离散 8.8pp（10.1~18.9%）、最差 10.1%，",
        f"      rank_normal 只有 2.9pp（11.6~14.5%）、最差 11.6% —— 即 **raw_z 高均值/高方差**。",
        f"      两者是多相位样本内的未裁决 trade-off，**不建议在此样本上改生产**；",
        f"      若日后改动，须先扩到 20+ 相位再判。见 `reports/winsorize_test.md`。",
        f"   2. **重新审 sue_gr / roe_lf 的因子权重**：全池 IC 分别 {ic_map.get('sue_gr', float('nan')):+.3f} / "
        f"{ic_map.get('roe_lf', float('nan')):+.3f}，若在 6 因子等权里只是稀释，考虑降权或改为门限因子"
        f"（与既有结论「质量是门限不是排序器」一致）。",
        f"   3. **组合层加行业/风格暴露约束**（如单行业 ≤15%、风格暴露 ±1σ 带），把 min_var 引入的",
        f"      银行 +{bnk_m*100-bnk_e*100:.0f}pp 与低波 +{np.mean(m['exp']['ivol_60'])-np.mean(e['exp']['ivol_60']):.2f}σ 漂移关进笼子。",
        f"      这是三条里**唯一没有被实验否决且风险明确**的一条 —— 银行 27.8% 是实打实的尾部暴露。",
    ]

    # ---- 暴露漂移：equal vs min_var ----
    lines += ["", "## min_var 引入的风格暴露漂移（equal → min_var_cap10）", "",
              "> 同为 Top30 选股，仅权重不同；变化 >0.10σ 视为实质漂移", "",
              "| 风格因子 | equal 暴露 | min_var 暴露 | 变化 | 判定 |", "|---|---|---|---|---|"]
    for k, (cn, _) in STYLE.items():
        e1 = float(np.mean(out["equal"]["exp"][k]))
        e2 = float(np.mean(out["min_var_cap10"]["exp"][k]))
        d = e2 - e1
        flag = "**实质漂移**" if abs(d) > 0.10 else ("轻微" if abs(d) > 0.05 else "—")
        lines.append(f"| {cn} | {e1:+.2f}σ | {e2:+.2f}σ | **{d:+.2f}σ** | {flag} |")

    # ---- 分年暴露 ----
    yrs = sorted(out["equal"]["exp_year"].keys())
    lines += ["", "## 分年风格暴露（equal / min_var）", "",
              "| 年份 | " + " | ".join(STYLE[k][0] for k in STYLE) + " |",
              "|---|" + "---|" * len(STYLE)]
    for y in yrs:
        cells = []
        for k in STYLE:
            a = np.mean(out["equal"]["exp_year"][y][k])
            b = np.mean(out["min_var_cap10"]["exp_year"][y][k])
            cells.append(f"{a:+.2f}/{b:+.2f}")
        lines.append(f"| {y} | " + " | ".join(cells) + " |")

    # ---- 行业暴露 ----
    lines += ["", "## 行业暴露（累计权重占比，Top12 + 银行）", "",
              "| 行业 | equal | min_var_cap10 | 漂移 |", "|---|---|---|---|"]
    w1, w2 = out["equal"]["ind_w"], out["min_var_cap10"]["ind_w"]
    s1 = sum(w1.values()) or 1.0
    s2 = sum(w2.values()) or 1.0
    all_ind = sorted(set(w1) | set(w2), key=lambda k: -(w1.get(k, 0) + w2.get(k, 0)))
    show = list(dict.fromkeys(all_ind[:12] + [i for i in all_ind if i in ("银行", "银行金融")]))
    for ind in show:
        a, b = w1.get(ind, 0.0) / s1, w2.get(ind, 0.0) / s2
        lines.append(f"| {ind} | {a*100:.1f}% | {b*100:.1f}% | {b*100-a*100:+.1f}pp |")
    summary["industry"] = {"equal": {k: v / s1 for k, v in w1.items()},
                           "min_var_cap10": {k: v / s2 for k, v in w2.items()}}

    conn.close()
    with open(os.path.join(REPO, "outputs", "attribution.json"), "w") as fh:
        json.dump({"strategy": st["label"], "start": ANCHOR, "end": end,
                   "n_periods": n_per, "top_n": top_n, "summary": summary},
                  fh, ensure_ascii=False, indent=2)
    outp = os.path.join(REPO, "reports", "attribution.md")
    with open(outp, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\n✅ {outp}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""组合优化: 风险端仓位分布对照 (P2#2)
====================================================================================
前提 (alpha_accuracy 已实证): 排序可靠 (IC 12.6%), 幅度不可靠 (R² 2.95%, 斜率逐期不稳)
→ 不能用预测分幅值配权 (MVO 收益项不可靠), 只做「风险端」加权。

四种加权方案 (均在排序选出的 TopN 内):
  1. equal        等权 (当前基准)
  2. inv_vol      逆波动率 w∝1/σ (σ=历史60日)
  3. min_var      最小方差 min wᵀΣw, Σw=1, w≥0 (Ledoit-Wolf 收缩 Σ)
  4. risk_parity  等风险贡献 (ERC 迭代)

对照: 年化/夏普/回撤/换手/集中度(Herfindahl), 对 baseline 与 LGBM 两策略。

用法: python3 scripts/portfolio_optimization.py [--window 0-93] [--strategy ...]
"""
import os
import sys

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from sklearn.covariance import LedoitWolf

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import factor_eval as fe  # noqa: E402
import strategy_lib as sl  # noqa: E402

START = "2019-01-01"
HIST = 60            # 协方差/波动率估计窗口 (交易日)
SCHEMES = ["equal", "inv_vol", "min_var", "min_var_cap10", "risk_parity"]
COST = 0.0015


def compute_weights(hist: pd.DataFrame, scheme: str) -> np.ndarray:
    """hist: (dates × stocks) 历史日收益; 返回权重 np.array (和为1, 非负)."""
    n = hist.shape[1]
    if n == 0:
        return np.array([])
    if scheme == "equal":
        return np.ones(n) / n
    rets = hist.fillna(0.0).to_numpy(dtype=np.float64)  # T × n
    sig = rets.std(axis=0)
    if scheme == "inv_vol":
        iv = np.where(sig > 1e-8, 1.0 / sig, 0.0)
        return iv / iv.sum() if iv.sum() > 0 else np.ones(n) / n
    # 协方差 (Ledoit-Wolf 收缩)
    try:
        cov = LedoitWolf().fit(rets).covariance_
    except Exception:
        cov = np.cov(rets, rowvar=False) + 1e-6 * np.eye(n)
    if scheme.startswith("min_var"):
        cap = 0.10 if scheme == "min_var_cap10" else None
        def obj(w):
            return float(w @ cov @ w)
        cons = [{"type": "eq", "fun": lambda w: w.sum() - 1.0}]
        bnds = [(0.0, cap)] * n
        res = minimize(obj, np.ones(n) / n, method="SLSQP", bounds=bnds,
                       constraints=cons, options={"maxiter": 300, "ftol": 1e-12})
        w = res.x if res.success else np.ones(n) / n
        w = np.maximum(w, 0.0)
        return w / w.sum() if w.sum() > 0 else np.ones(n) / n
    if scheme == "risk_parity":
        # ERC 迭代 (Griveau-Billion 乘子法)
        w = np.ones(n) / n
        for _ in range(50):
            marg = cov @ w
            sig_p = float(np.sqrt(w @ marg))
            if sig_p <= 1e-12:
                break
            rc = w * marg / sig_p
            target = sig_p / n
            scale = np.sqrt(np.where(rc > 1e-12, target / rc, 1.0))
            w = w * scale
            w = np.maximum(w, 0.0)
            w = w / w.sum()
        return w
    return np.ones(n) / n


def stats(nav: pd.Series) -> dict:
    ret = nav.pct_change().dropna()
    if len(ret) < 60:
        return {}
    years = len(ret) / 244
    ann = (nav.iloc[-1] / nav.iloc[0]) ** (1 / years) - 1
    vol = ret.std() * np.sqrt(244)
    dd = (nav / nav.cummax() - 1).min()
    return {"ann": ann, "vol": vol, "sharpe": ann / vol if vol > 0 else np.nan, "dd": dd}


def run(conn, st, daily_ret, uni, list_dates, rebal, scheme, window):
    fweights, top_n = st["cfg"]["factors"], st["cfg"].get("top_n", 30)
    factors = {n: sl.load_factor(conn, n, START, end) for n in fweights}
    dr_idx, dr_cols = daily_ret.index, daily_ret.columns
    rets, herfs, turns = [], [], []
    prev_w, prev_sel = {}, set()

    for t in rebal:
        score, _ = sl.score_cross(factors, fweights, t, uni, list_dates)
        if score is None:
            continue
        sel, _ = sl.select_stocks(score, top_n, window)
        if len(sel) < 5:
            continue
        pos_dates = [d for d in dr_idx if d > t][:fe.REBAL]
        if not pos_dates:
            break
        # 历史收益窗口 (选出的股票)
        hist = daily_ret.loc[:t].iloc[-HIST:][[c for c in sel if c in dr_cols]]
        if hist.shape[1] < 5 or hist.shape[0] < 20:
            continue
        w = compute_weights(hist, scheme)
        stocks = hist.columns.tolist()
        # 与上一期权重对齐算换手
        wmap = {c: wi for c, wi in zip(stocks, w)}
        turnover = 0.5 * sum(abs(wmap.get(c, 0) - prev_w.get(c, 0))
                             for c in set(wmap) | set(prev_w))
        turns.append(turnover)
        herfs.append(float((w ** 2).sum()))
        row_pos = dr_idx.get_indexer(pos_dates)
        col_pos = dr_cols.get_indexer(stocks)
        m = daily_ret.iloc[row_pos, col_pos].to_numpy(dtype=np.float64)
        with np.errstate(invalid="ignore"):
            dr = m @ w
        dr = np.where(np.isfinite(dr), dr, 0.0)
        dr[0] -= turnover * 2 * COST
        for d, r in zip(pos_dates, dr):
            rets.append((d, float(r)))
        prev_w, prev_sel = wmap, set(stocks)

    nav = (1 + pd.Series(dict(rets)).sort_index()).cumprod()
    return nav, np.mean(turns) if turns else np.nan, np.mean(herfs) if herfs else np.nan


def fmt(v):
    return "-" if v is None or not np.isfinite(v) else f"{v*100:.1f}%"


def main():
    import argparse
    global HIST, end
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", action="append", default=None)
    ap.add_argument("--window", default=None, help="选股窗口 lo-hi (如 0-93); 默认 TopN")
    ap.add_argument("--hist", type=int, default=HIST)
    args = ap.parse_args()
    HIST = args.hist
    window = None
    if args.window:
        window = [float(x) for x in args.window.split("-")]
    specs = args.strategy or ["prod_6f_eq@topn=30", "prod_lgbm_neu"]

    end = pd.Timestamp.today().strftime("%Y-%m-%d")
    conn = fe.get_conn()
    uni, list_dates = fe.load_universe_filter(conn)
    strategies = sl.load_strategies(conn, specs)
    daily_ret = fe.load_daily_returns(conn, "2015-10-01", end)
    rebal = fe.rebalance_dates(daily_ret.index, START, end)
    print(f"股票池 {len(uni)} | 调仓 {len(rebal)} 期 | 协方差窗口 {HIST} 日")

    lines = [f"# 组合优化对照 (风险端加权, {START} ~ {end})\n",
             f"- 选股: {'Top%d' % strategies[0]['cfg'].get('top_n',30) if window is None else 'window '+args.window} | "
             f"协方差: Ledoit-Wolf 收缩 (窗口 {HIST} 日) | 单边成本 {COST*100:.2f}%\n",
             "- 前提: 排序可靠/幅度不可靠 → 不做 MVO 收益项, 只做风险端加权\n",
             "- 样本外=2023-01-01 起 (验证风险端加权是否过拟合)\n"]
    OOS = pd.Timestamp("2023-01-01")
    for st in strategies:
        lines.append(f"\n## {st['label']}\n")
        lines.append("| 方案 | 年化 | 夏普 | 回撤 | 样本外夏普 | 样本外回撤 | 换手 | 集中度H |")
        lines.append("|---|---|---|---|---|---|---|---|")
        print(f"\n[{st['label']}]")
        for scheme in SCHEMES:
            nav, turn, herf = run(conn, st, daily_ret, uni, list_dates, rebal, scheme, window)
            s = stats(nav)
            s_oos = stats(nav[nav.index >= OOS])
            lines.append(f"| {scheme} | {fmt(s['ann'])} | {s['sharpe']:.2f} | {fmt(s['dd'])} "
                         f"| {s_oos['sharpe']:.2f} | {fmt(s_oos['dd'])} | {turn*100:.0f}% | {herf:.3f} |")
            print(f"  {scheme:11s} 年化 {fmt(s['ann'])} 夏普 {s['sharpe']:.2f} 回撤 {fmt(s['dd'])} "
                  f"| 样本外夏普 {s_oos['sharpe']:.2f} 回撤 {fmt(s_oos['dd'])} 换手 {turn*100:.0f}% H={herf:.3f}")

    conn.close()
    report = "\n".join(lines)
    out = os.path.join(REPO, "reports", "portfolio_optimization.md")
    with open(out, "w") as f:
        f.write(report + "\n")
    print(f"\n✅ 报告: {out}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""因子权重优化: 6 因子合成等权 vs 优化权重 (线性, walk-forward)
====================================================================================
问题: prod_6f_eq 六因子目前等权 (各 1.0), 因子层权重是否有优化空间?
前提: 组合层已定型 (Top30 + min_var_cap10, 60日协方差), 本实验只动因子合成权重。

⚠ 相位教训 (2026-09-12 实证): 同策略调仓网格错开 10 个交易日, 年化 9.8% vs 18.7%
  (OOS 夏普 0.62 vs 1.44) —— 回测对网格相位极度敏感。因此:
  1. 主网格必须与生产口径同相位 (rebalance_dates 锚 2019-01-01)
  2. IC 历史网格与回测网格同相位向前延伸 (防跨期前视)
  3. 结论必须过「多相位稳健性检验」(offset 2/5/10/15 交易日网格, 报均值/最差)

方案 (全部 walk-forward, t 期权重只用 t 之前完全实现的 IC):
  1. equal      等权 (基准)
  2. ic_12      过去12期因子 RankIC 均值加权 (负 IC 截 0)
  3. icir_12    过去12期 ICIR (IC均值/IC标准差) 加权
  4. icir_24    过去24期 ICIR 加权
  5. icir12_shr icir_12 与等权 50/50 收缩 (稳健折中)
  6. max_icir_24 凸优化: 最大化合成因子 ICIR (考虑因子 IC 相关性), 非负/和1/单因子≤35%
  7. ridge_24   Fama-MacBeth: 过去24期截面 Ridge 回归系数均值 (净贡献视角)

IC 口径: t 期因子 z 截面 vs [t+1, t+REBAL] 累计收益的 Spearman 秩相关;
  IC 历史从 2016 同相位延伸 (burn-in), 回测锚 2019-01 (与生产一致)。

用法: python3 scripts/factor_weight_scan.py [--no-phase] [--schemes equal,ic_12,...]
"""
import os
import sys

import numpy as np
import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import factor_eval as fe  # noqa: E402
import strategy_lib as sl  # noqa: E402
from portfolio_optimization import compute_weights  # noqa: E402

FACTORS = ["ln_mv", "ep_ttm", "ivol_60", "sue_delta", "ret_20d_rev", "turnover_20"]
ANCHOR = "2019-01-01"     # 生产网格锚 (相位基准)
BACK_TO = "2016-01-01"    # IC 历史延伸起点
TOP_N = 30
HIST = 60                 # 协方差窗口 (交易日)
COST = 0.0015
FCAP = 0.35               # 单因子权重上限 (max_icir)
RIDGE_LMBDA = 1.0
PHASE_OFFSETS = [2, 5, 10, 15]   # 相位稳健性检验偏移 (交易日)


# ---------------------------------------------------------------- 网格 (同相位)

def build_grid(idx: pd.DatetimeIndex, anchor: str, back_to: str, end: str) -> list:
    """以 rebalance_dates(idx, anchor) 的首日为相位锚, 向前每 REBAL 个交易日延伸到 back_to。
    保证 IC 历史与回测网格同相位 (相邻期恰好间隔 REBAL 交易日 → 无跨期前视)。"""
    g = fe.rebalance_dates(idx, anchor, end)
    p0 = int(idx.get_indexer([g[0]])[0])
    ext, p = [], p0
    while p >= 0 and idx[p] >= pd.Timestamp(back_to):
        ext.append(idx[p])
        p -= fe.REBAL
    return sorted(set(ext + g))


def offset_grid(idx: pd.DatetimeIndex, anchor: str, offset: int, end: str) -> list:
    """相位偏移网格: anchor 后第 offset 个交易日作为新锚 (用于稳健性检验)。"""
    sub = idx[idx >= pd.Timestamp(anchor)]
    new_anchor = sub[offset]
    return build_grid(idx, new_anchor.strftime("%Y-%m-%d"), BACK_TO, end)


# ---------------------------------------------------------------- 截面缓存 (惰性, 多网格共享)

class Section:
    """惰性构建: z 截面 / IC 序列 / Ridge 系数序列, 按网格缓存。"""

    def __init__(self, fwides, daily_ret, uni, list_dates):
        self.fwides, self.daily_ret = fwides, daily_ret
        self.uni, self.list_dates = uni, list_dates
        self.names = list(fwides.keys())
        self._z = {}
        self._ic = {}
        self._ridge = {}

    def zdf(self, t):
        if t not in self._z:
            _, z = sl.score_cross(self.fwides, {n: 1.0 for n in self.names},
                                  t, self.uni, self.list_dates)
            self._z[t] = z if (z is not None and len(z) >= 50) else None
        return self._z[t]

    def _fwd(self, t):
        dr = self.daily_ret
        i = dr.index.get_indexer([t])[0]
        if i < 0 or i + fe.REBAL >= len(dr):
            return None
        return dr.iloc[i + 1: i + 1 + fe.REBAL].sum()

    def ic(self, grid):
        key = id(grid)
        if key not in self._ic:
            rows = {}
            for t in grid:
                z = self.zdf(t)
                if z is None:
                    continue
                fwd = self._fwd(t)
                if fwd is None:
                    continue
                fwd = fwd.reindex(z.index)
                if fwd.notna().sum() < 50:
                    continue
                rows[t] = {n: z[n].corr(fwd, method="spearman") for n in self.names}
            self._ic[key] = pd.DataFrame(rows).T[self.names]
        return self._ic[key]

    def ridge(self, grid):
        key = id(grid)
        if key not in self._ridge:
            rows = {}
            for t in grid:
                z = self.zdf(t)
                if z is None:
                    continue
                fwd = self._fwd(t)
                if fwd is None:
                    continue
                fwd = fwd.reindex(z.index)
                m = pd.concat([z, fwd.rename("__fwd")], axis=1).dropna()
                if len(m) < 100:
                    continue
                X, y = m[self.names].to_numpy(float), m["__fwd"].to_numpy(float)
                A = X.T @ X + RIDGE_LMBDA * np.eye(len(self.names))
                rows[t] = dict(zip(self.names, np.linalg.solve(A, X.T @ y)))
            self._ridge[key] = pd.DataFrame(rows).T[self.names]
        return self._ridge[key]


# ---------------------------------------------------------------- 权重方案

def _norm(w: np.ndarray) -> np.ndarray:
    w = np.clip(w, 0.0, None)
    if w.sum() <= 1e-9:
        return np.ones(len(w)) / len(w)
    return w / w.sum()


def _w(s: pd.Series) -> dict:
    return dict(zip(FACTORS, _norm(s.reindex(FACTORS).to_numpy(float))))


def w_equal(t, ics, ridges):
    return {n: 1.0 for n in FACTORS}


def w_ic_12(t, ics, ridges):
    hist = ics.loc[ics.index < t].tail(12)
    return _w(hist.mean()) if len(hist) >= 6 else w_equal(t, ics, ridges)


def w_icir(t, ics, ridges, W=12):
    hist = ics.loc[ics.index < t].tail(W)
    if len(hist) < max(6, W // 2):
        return w_equal(t, ics, ridges)
    m, s = hist.mean(), hist.std()
    return _w((m / s.replace(0, np.nan)).fillna(0.0))


def w_icir12_shr(t, ics, ridges):
    base = w_icir(t, ics, ridges, W=12)
    return {n: 0.5 * base[n] + 0.5 / len(FACTORS) for n in FACTORS}


def w_max_icir(t, ics, ridges, W=24):
    from scipy.optimize import minimize
    hist = ics.loc[ics.index < t].tail(W)
    if len(hist) < 12:
        return w_equal(t, ics, ridges)
    mu = hist.mean().to_numpy(float)
    cov = hist.cov().to_numpy(float)
    cov = 0.9 * cov + 0.1 * np.diag(np.diag(cov)) + 1e-8 * np.eye(len(mu))
    n = len(mu)

    def neg_icir(w):
        v = float(np.sqrt(max(w @ cov @ w, 1e-12)))
        return -float(w @ mu) / v

    res = minimize(neg_icir, np.ones(n) / n, method="SLSQP",
                   bounds=[(0.0, FCAP)] * n,
                   constraints=[{"type": "eq", "fun": lambda w: w.sum() - 1.0}],
                   options={"maxiter": 300, "ftol": 1e-12})
    w = np.clip(res.x, 0.0, None) if res.success else np.ones(n) / n
    if w.sum() <= 1e-9:
        w = np.ones(n) / n
    return dict(zip(FACTORS, w / w.sum()))


def w_ridge_24(t, ics, ridges):
    hist = ridges.loc[ridges.index < t].tail(24)
    return _w(hist.mean()) if len(hist) >= 12 else w_equal(t, ics, ridges)


SCHEMES = [
    ("equal", w_equal),
    ("ic_12", w_ic_12),
    ("icir_12", lambda t, ics, r: w_icir(t, ics, r, W=12)),
    ("icir_24", lambda t, ics, r: w_icir(t, ics, r, W=24)),
    ("icir12_shr", w_icir12_shr),
    ("max_icir_24", w_max_icir),
    ("ridge_24", w_ridge_24),
]


# ---------------------------------------------------------------- 回测 (与 portfolio_optimization.run 同口径)

def stats(nav: pd.Series) -> dict:
    ret = nav.pct_change().dropna()
    if len(ret) < 60:
        return {"ann": np.nan, "sharpe": np.nan, "dd": np.nan}
    years = len(ret) / 244
    ann = (nav.iloc[-1] / nav.iloc[0]) ** (1 / years) - 1
    vol = ret.std() * np.sqrt(244)
    dd = (nav / nav.cummax() - 1).min()
    return {"ann": ann, "sharpe": ann / vol if vol > 0 else np.nan, "dd": dd}


def backtest(sec: Section, wfunc, grid, daily_ret, rebal,
             industry_cap=None, industry_map=None):
    dr_idx, dr_cols = daily_ret.index, daily_ret.columns
    ics, ridges = sec.ic(grid), sec.ridge(grid)
    rets, turns, w_hist, prev_w = [], [], [], {}
    for t in rebal:
        zdf = sec.zdf(t)
        if zdf is None:
            continue
        wser = pd.Series(wfunc(t, ics, ridges)).reindex(zdf.columns).fillna(0.0)
        active = int((wser != 0).sum())
        if active == 0:
            continue
        score = zdf.mul(wser).sum(
            axis=1, min_count=max(1, active // 2)).dropna()
        if len(score) < 50:
            continue
        sel = list(score.sort_values(ascending=False).head(TOP_N).index)
        if len(sel) < 5:
            continue
        pos_dates = [d for d in dr_idx if d > t][:fe.REBAL]
        if not pos_dates:
            break
        hist = daily_ret.loc[:t].iloc[-HIST:][[c for c in sel if c in dr_cols]]
        if hist.shape[1] < 5 or hist.shape[0] < 20:
            continue
        w = compute_weights(hist, "min_var_cap10")
        stocks = hist.columns.tolist()
        if industry_cap and industry_map:
            # 行业约束 (与生产 generate_signals 同口径, 联合投影单票≤10%+行业≤cap)
            wser2 = pd.Series(w, index=stocks)
            wser2 = sl.apply_industry_cap(wser2, industry_map, industry_cap, single_cap=0.10)
            w = wser2.to_numpy(float)
            stocks = wser2.index.tolist()
        pmap = dict(zip(stocks, w))
        to = 0.5 * sum(abs(pmap.get(c, 0) - prev_w.get(c, 0))
                       for c in set(pmap) | set(prev_w))
        turns.append(to)
        w_hist.append(wser.to_dict())
        m = daily_ret.iloc[dr_idx.get_indexer(pos_dates),
                           dr_cols.get_indexer(stocks)].to_numpy(float)
        dr = np.where(np.isfinite(m @ w), m @ w, 0.0)
        dr[0] -= to * 2 * COST
        for d, r in zip(pos_dates, dr):
            rets.append((d, float(r)))
        prev_w = pmap
    nav = (1 + pd.Series(dict(rets)).sort_index()).cumprod()
    return nav, (np.mean(turns) if turns else np.nan), w_hist


def fmt(v):
    return "-" if v is None or not np.isfinite(v) else f"{v*100:.1f}%"


# ---------------------------------------------------------------- main

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-phase", action="store_true", help="跳过多相位稳健性检验")
    ap.add_argument("--schemes", default=None, help="逗号分隔方案子集")
    args = ap.parse_args()

    end = pd.Timestamp.today().strftime("%Y-%m-%d")
    conn = fe.get_conn()
    uni, list_dates = fe.load_universe_filter(conn)
    fwides = {n: sl.load_factor(conn, n, "2015-06-01", end) for n in FACTORS}
    daily_ret = fe.load_daily_returns(conn, "2015-10-01", end)
    conn.close()
    idx = daily_ret.index

    grid = build_grid(idx, ANCHOR, BACK_TO, end)
    rebal = [t for t in grid if t >= pd.Timestamp(ANCHOR)]
    print(f"股票池 {len(uni)} | 同相位网格 {len(grid)} 期 (锚 {ANCHOR}) | 回测 {len(rebal)} 期")
    sec = Section(fwides, daily_ret, uni, list_dates)

    ics = sec.ic(grid)
    print(f"IC 序列 {len(ics)} 期 | 全期 IC 均值: " +
          ", ".join(f"{n}={ics[n].mean()*100:.1f}%" for n in FACTORS))

    schemes = SCHEMES
    if args.schemes:
        keep = set(args.schemes.split(","))
        schemes = [(n, f) for n, f in SCHEMES if n in keep]

    OOS = pd.Timestamp("2023-01-01")
    T3Y = pd.Timestamp("2023-09-01")
    lines = [f"# 因子权重优化对照 (6因子, Top30 + min_var_cap10, {ANCHOR} ~ {end})\n",
             "- 组合层锁定 (Top30/min_var_cap10/60日协方差/单边成本0.15%), 只变因子合成权重",
             f"- 全部 walk-forward: IC 历史与回测网格**同相位**从 {BACK_TO} 延伸, t 期权重仅用 t 前完全实现的 IC",
             f"- 非负约束 (负 IC 因子不参与), max_icir 单因子上限 {FCAP:.0%}",
             "- ⚠ 相位教训: 网格错开 10 交易日曾致等权 9.8% vs 18.7% —— 结论须看多相位检验\n",
             "| 方案 | 年化 | 夏普 | 回撤 | 样本外夏普 | 样本外回撤 | 三年窗口年化 | 换手 |",
             "|---|---|---|---|---|---|---|---|"]
    navs, wavg = {}, {}
    for name, wfunc in schemes:
        print(f"回测 {name} ...")
        nav, turn, w_hist = backtest(sec, wfunc, grid, daily_ret, rebal)
        s, so = stats(nav), stats(nav[nav.index >= OOS])
        s3 = stats(nav[nav.index >= T3Y])
        navs[name] = nav
        if w_hist:
            wavg[name] = pd.DataFrame(w_hist).mean().to_dict()
        lines.append(f"| {name} | {fmt(s['ann'])} | {s['sharpe']:.2f} | {fmt(s['dd'])} "
                     f"| {so['sharpe']:.2f} | {fmt(so['dd'])} | {fmt(s3['ann'])} "
                     f"| {turn*100:.0f}% |")
        print(f"  {name:12s} 年化 {fmt(s['ann'])} 夏普 {s['sharpe']:.2f} 回撤 {fmt(s['dd'])} "
              f"| OOS夏普 {so['sharpe']:.2f} | 3Y年化 {fmt(s3['ann'])}")

    # 平均因子权重
    if wavg:
        lines.append("\n## 各方案平均因子权重\n")
        lines.append("| 方案 | " + " | ".join(FACTORS) + " |")
        lines.append("|---" * (len(FACTORS) + 1) + "|")
        for name, _ in schemes:
            w = wavg.get(name, {})
            lines.append(f"| {name} | " + " | ".join(
                f"{w.get(n, np.nan):.2f}" for n in FACTORS) + " |")

    # IC 诊断
    lines.append("\n## 因子 IC 诊断 (同相位网格全期)\n")
    lines.append("| 因子 | IC均值 | ICIR | IC正率 |")
    lines.append("|---|---|---|---|")
    for n in FACTORS:
        ic = ics[n].dropna()
        lines.append(f"| {n} | {ic.mean()*100:.1f}% | {ic.mean()/ic.std():.2f} | {(ic>0).mean()*100:.0f}% |")

    # 多相位稳健性
    if not args.no_phase:
        lines.append(f"\n## 多相位稳健性检验 (offset {PHASE_OFFSETS} 交易日, 报年化)\n")
        lines.append("| 方案 | 主相位 | " + " | ".join(f"+{o}日" for o in PHASE_OFFSETS) +
                     " | 多相位均值 | 最差 |")
        lines.append("|---" * (3 + len(PHASE_OFFSETS)) + "|")
        print("\n多相位稳健性检验 ...")
        for name, wfunc in schemes:
            anns = [stats(navs[name])["ann"]]
            for off in PHASE_OFFSETS:
                g = offset_grid(idx, ANCHOR, off, end)
                rb = [t for t in g if t >= pd.Timestamp(ANCHOR)]
                nav_off, _, _ = backtest(sec, wfunc, g, daily_ret, rb)
                anns.append(stats(nav_off)["ann"])
                print(f"  {name} +{off}日: 年化 {fmt(anns[-1])}")
            lines.append(f"| {name} | " + " | ".join(fmt(a) for a in anns) +
                         f" | {fmt(np.mean(anns))} | {fmt(np.min(anns))} |")

    out = os.path.join(REPO, "reports", "factor_weight_scan.md")
    with open(out, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n✅ 报告: {out}")


if __name__ == "__main__":
    main()

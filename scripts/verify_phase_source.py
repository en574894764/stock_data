#!/usr/bin/env python3
"""验证相位敏感性的来源：不同 anchor 下 20 相位 raw 年化离散度对比。

背景：phase_robustness.py 发现 offset 0..19 的年化在 9.9%~20.4% 之间大幅波动，
  上一轮被误读为「月初效应」。本脚本验证真相：
  offset = 把「每 REBAL=20 交易日调仓」的整张网格平移 N 天（非日历锚定）。
  若 2019 年初暴涨是主因，anchor 后移（2020/2021）后离散度应显著收窄；
  若离散度不降，则相位敏感性是月度调仓的固有特性。

用法: python3 scripts/verify_phase_source.py [anchor] [--n-phase 20]
"""
import os
import sys

import numpy as np
import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import factor_eval as fe          # noqa: E402
import strategy_lib as sl         # noqa: E402
import factor_weight_scan as fws  # noqa: E402
from portfolio_optimization import compute_weights  # noqa: E402
from phase_robustness import SectionZ, PROD6       # noqa: E402

HIST = 60
COST = 0.0015
TOP_N = 30


def backtest_light(sec, grid, daily_ret, rebal, cost=COST):
    """与 fws.backtest 同口径，但跳过 IC/Ridge（等权合成用不到），快约 4 倍。"""
    dr_idx, dr_cols = daily_ret.index, daily_ret.columns
    rets, turns, prev_w = [], [], {}
    for t in rebal:
        zdf = sec.zdf(t)
        if zdf is None:
            continue
        score = zdf.sum(axis=1, min_count=3).dropna()   # 等权合成
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
        pmap = dict(zip(stocks, w))
        to = 0.5 * sum(abs(pmap.get(c, 0) - prev_w.get(c, 0))
                       for c in set(pmap) | set(prev_w))
        turns.append(to)
        m = daily_ret.iloc[dr_idx.get_indexer(pos_dates),
                           dr_cols.get_indexer(stocks)].to_numpy(float)
        dr = np.where(np.isfinite(m @ w), m @ w, 0.0)
        dr[0] -= to * 2 * cost
        for d, r in zip(pos_dates, dr):
            rets.append((d, float(r)))
        prev_w = pmap
    nav = (1 + pd.Series(dict(rets)).sort_index()).cumprod()
    return nav, (np.mean(turns) if turns else np.nan)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("anchor", nargs="?", default="2019-01-01")
    ap.add_argument("--n-phase", type=int, default=20)
    ap.add_argument("--rebal", type=int, default=20, help="调仓间隔(交易日)")
    ap.add_argument("--cost", type=float, default=0.0015, help="单边成本(0=零成本归因)")
    args = ap.parse_args()
    anchor = args.anchor
    fe.REBAL = args.rebal   # monkey-patch: 影响 rebalance_dates/build_grid/offset_grid/持有期切片

    end = pd.Timestamp.today().strftime("%Y-%m-%d")
    conn = fe.get_conn()
    uni, list_dates = fe.load_universe_filter(conn)
    daily_ret = fe.load_daily_returns(conn, "2015-10-01", end)
    idx = daily_ret.index
    fw = {n: sl.load_factor(conn, n, "2015-06-01", end).astype("float32") for n in PROD6}
    conn.close()

    sec = SectionZ(fw, daily_ret, uni, list_dates, mode="raw")
    anns = []
    for off in range(args.n_phase):
        g = fws.offset_grid(idx, anchor, off, end)
        rb = [t for t in g if t >= pd.Timestamp(anchor)]
        nav, _ = backtest_light(sec, g, daily_ret, rb, cost=args.cost)
        a = fws.stats(nav)["ann"]
        anns.append(a)
        print(f"REBAL={args.rebal} anchor={anchor} off={off:2d} 年化 {a*100:5.1f}%", flush=True)

    arr = np.array(anns)
    print(f"\nREBAL={args.rebal} anchor={anchor} | 均值 {arr.mean()*100:.1f}% | "
          f"离散 {arr.std()*100:.1f}pp | 最差 {arr.min()*100:.1f}% | 最好 {arr.max()*100:.1f}% | "
          f"范围 {arr.max()*100-arr.min()*100:.1f}pp")


if __name__ == "__main__":
    main()

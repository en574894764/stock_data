#!/usr/bin/env python3
"""
择时层实证 (timing_filter 有效性验证)
=====================================
6 因子组合日收益 (等权合成截面, Q5 代理 top50) × 大盘 200 日均线择时,
按调仓日采样择时状态 (调仓日收盘状态决定整个持有期, 无前视):

    无择时     组合原样
    flat       指数 < MA200 的调仓期 → 空仓 (该期收益 0)
    half       指数 < MA200 的调仓期 → 半仓 (该期收益 × 0.5)

用法:
    python3 scripts/timing_eval.py                      # 默认 000300.SH / MA200
    python3 scripts/timing_eval.py --window 120 --mode flat,half
"""
import argparse
import os
import sys
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
CACHE = os.path.join(REPO, "factor_cache")

import factor_eval as fe

PROD_FACTORS = ["ret_20d_rev", "turnover_20", "ivol_60", "ln_mv", "ep_ttm", "sue_delta"]


def load_index_ma(conn, symbol, window):
    cur = conn.cursor()
    cur.execute("SELECT trade_date, close FROM index_daily WHERE symbol=%s ORDER BY trade_date", (symbol,))
    rows = cur.fetchall()
    cur.close()
    s = pd.Series({pd.Timestamp(r[0]): float(r[1]) for r in rows}).sort_index()
    ma = s.rolling(window).mean()
    return s, ma


def combo_q5_returns(conn, factors, daily_ret, rebal, uni, list_dates):
    """等权合成 → evaluate_single → Q5 日收益序列 (pct_change)."""
    frames = {}
    for name in factors:
        p = os.path.join(CACHE, f"{name}.parquet")
        if not os.path.exists(p):
            raise SystemExit(f"无因子缓存: {name}")
        frames[name] = pd.read_parquet(p).sort_index().sort_index(axis=1)
    combo_rows = {}
    for t in rebal:
        zs = []
        for name in factors:
            w = frames[name]
            if t not in w.index:
                continue
            row = w.loc[t].dropna()
            keep = [c for c in row.index if c in uni and list_dates.get(c) is not None
                    and t > pd.Timestamp(list_dates[c]) + pd.Timedelta(days=120)]
            row = row[keep]
            if len(row) < 50:
                zs = None
                break
            sd = row.std()
            zs.append((row - row.mean()) / (sd if sd > 0 else 1.0))
        if zs is None or len(zs) < 2:
            continue
        combo_rows[t] = pd.concat(zs, axis=1).sum(axis=1, min_count=1)
    combo = pd.DataFrame(combo_rows).T.sort_index()
    combo.index = pd.to_datetime(combo.index)

    fwd = fe.fwd_from_daily(daily_ret)
    r = fe.evaluate_single("combo", combo, fwd, daily_ret, rebal, uni, list_dates)
    q = r["q_navs"]
    if q is None or q.empty:
        raise SystemExit("组合回测为空")
    return q[fe.QUANTILES].pct_change().dropna(), q


def apply_timing(daily_ret: pd.Series, rebal_dates, index_close, index_ma, mode: str) -> pd.Series:
    """mode: none/flat/half. 择时状态在调仓日收盘确定, 覆盖其整个持有期."""
    if mode == "none":
        return daily_ret
    factor = 0.0 if mode == "flat" else 0.5
    out = daily_ret.copy()
    state = None
    for i, t in enumerate(rebal_dates):
        if t not in index_close.index or pd.isna(index_ma.get(t, np.nan)):
            continue
        state = factor if index_close[t] < index_ma[t] else 1.0
        # 该调仓期覆盖的交易日
        nxt = rebal_dates[i + 1] if i + 1 < len(rebal_dates) else daily_ret.index[-1]
        mask = (daily_ret.index > t) & (daily_ret.index <= nxt)
        out[mask] = daily_ret[mask] * state
    return out


def stats(ret: pd.Series, bench: pd.Series) -> dict:
    nav = (1 + ret).cumprod()
    years = len(ret) / 244
    ann = nav.iloc[-1] ** (1 / years) - 1
    vol = ret.std() * np.sqrt(244)
    dd = (nav / nav.cummax() - 1).min()
    b = (1 + bench).cumprod().iloc[-1]
    return {"ann": ann, "vol": vol, "sharpe": ann / vol if vol > 0 else np.nan,
            "dd": dd, "total": nav.iloc[-1] - 1, "excess": nav.iloc[-1] - b}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--end", default="2026-09-01")
    ap.add_argument("--index", default="000300.SH")
    ap.add_argument("--window", type=int, default=200)
    ap.add_argument("--modes", default="none,flat,half")
    args = ap.parse_args()

    conn = fe.get_conn()
    uni, list_dates = fe.load_universe_filter(conn)
    load_start = (pd.Timestamp(args.start) - pd.Timedelta(days=40)).strftime("%Y-%m-%d")
    daily_ret = fe.load_daily_returns(conn, load_start, args.end)
    rebal = fe.rebalance_dates(daily_ret.index, args.start, args.end)

    combo_ret, q_nav = combo_q5_returns(conn, PROD_FACTORS, daily_ret, rebal, uni, list_dates)
    idx_close, idx_ma = load_index_ma(conn, args.index, args.window)

    bench = daily_ret.mean(axis=1)  # 全市场等权代理
    print(f"6 因子等权 Q5 组合: {len(combo_ret)} 交易日, 无择时年化 "
          f"{stats(combo_ret, bench)['ann']:+.1%}\n")

    L = [f"# 择时层实证: {args.index} MA{args.window}\n",
         f"- 区间: {args.start} ~ {args.end} | 组合: 6 因子等权 Q5 | 择时状态按调仓日收盘采样 (无前视)\n",
         "| 模式 | 年化 | 波动 | 夏普 | 最大回撤 | 累计 | 相对全市场超额 |",
         "|---|---|---|---|---|---|---|"]
    results = {}
    for mode in args.modes.split(","):
        timed = apply_timing(combo_ret, rebal, idx_close, idx_ma, mode)
        s = stats(timed, bench)
        results[mode] = (s, timed)
        label = {"none": "无择时", "flat": "跌破空仓", "half": "跌破半仓"}.get(mode, mode)
        L.append("| {} | {:.1%} | {:.1%} | {:.2f} | {:.1%} | {:.1%} | {:+.1%} |".format(
            label, s["ann"], s["vol"], s["sharpe"], s["dd"], s["total"], s["excess"]))

    # 分年对比 (空仓 vs 无择时)
    if "flat" in results:
        L.append("\n## 分年收益对比 (无择时 vs 跌破空仓)\n")
        L.append("| 年份 | 无择时 | 空仓 | 差 |")
        L.append("|---|---|---|---|")
        base = results["none"][1]
        flat = results["flat"][1]
        for y in sorted(set(base.index.year)):
            b = (1 + base[base.index.year == y]).prod() - 1
            f = (1 + flat[flat.index.year == y]).prod() - 1
            L.append(f"| {y} | {b:+.1%} | {f:+.1%} | {f-b:+.1%} |")

    # 跌破时段统计
    below = (idx_close < idx_ma).dropna()
    below_recent = below[below.index >= pd.Timestamp(args.start)]
    L.append(f"\n**{args.index} 跌破 MA{args.window} 占比**: {below_recent.mean():.0%} "
             f"({below_recent.sum()} / {len(below_recent)} 交易日)")

    report = "\n".join(L)
    print("\n" + report)
    out = os.path.join(REPO, "reports", "timing_eval.md")
    with open(out, "w") as fo:
        fo.write(report + "\n")
    print(f"\n报告已写入: {out}")
    conn.close()


if __name__ == "__main__":
    main()

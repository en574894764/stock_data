#!/usr/bin/env python3
"""配套对照：调仓缩短时，把 20 日窗口因子同步改成 W 日窗口，能回收多少年化？
====================================================================================
背景
  reports/rebalance_freq.md 的成本归因发现：双周(10日)零成本下仍比月度少 0.4pp，
  归因为**因子期限错配** —— ret_20d_rev / turnover_20 是 20 日口径，却只持有 10 日。
  该报告估计「换 W 日口径可回收 0.4pp」，但当时没有实测。

本实验
  对 W ∈ {10, 5} 各跑两个变体（同锚点、同完整相位周期、同成本）：
    baseline  ret_20d_rev + turnover_20      (生产口径，期限错配)
    matched   ret_{W}d_rev + turnover_{W}    (窗口与持有期对齐)
  另做 W=20 的口径一致性 sanity（内存重算的 ret_20d_rev vs PG 因子截面秩相关）

用法: python3 scripts/rebal_window_match_test.py [--anchor 2019-01-01] [--rebals 10,5]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np
import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import factor_eval as fe          # noqa: E402
import strategy_lib as sl         # noqa: E402
import factor_weight_scan as fws  # noqa: E402
from phase_robustness import SectionZ, PROD6  # noqa: E402
from rebal_freq_multi_anchor import backtest_light, OOS, pct  # noqa: E402


def load_turnover_wide(start: str, end: str) -> pd.DataFrame:
    """daily_basic.turnover_rate 宽表, 按年分块 pivot 防内存峰值。"""
    conn = fe.get_conn()
    years = range(int(start[:4]), int(end[:4]) + 1)
    parts = []
    for y in years:
        sql = ("SELECT trade_date, ts_code, turnover_rate FROM daily_basic "
               f"WHERE trade_date >= '{y}-01-01' AND trade_date <= '{y}-12-31'")
        d = pd.read_sql(sql, conn)
        if d.empty:
            continue
        d["trade_date"] = pd.to_datetime(d["trade_date"])
        d["turnover_rate"] = pd.to_numeric(d["turnover_rate"], errors="coerce")
        parts.append(d.pivot_table(index="trade_date", columns="ts_code",
                                   values="turnover_rate", aggfunc="last").astype("float32"))
        print(f"    换手率 {y}: {len(d):,} 行", flush=True)
    conn.close()
    w = pd.concat(parts).sort_index()
    return w


def ret_w_rev(ret_wide: pd.DataFrame, w: int) -> pd.DataFrame:
    """-(过去 W 交易日累计收益), 与 compute_factors.ret_20d_rev 同构。"""
    logret = np.log1p(ret_wide.clip(-0.95, 10)).astype("float32")
    cum = logret.cumsum()
    return (np.exp(cum - cum.shift(w)) - 1.0).mul(-1.0).astype("float32")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--anchor", default="2019-01-01")
    ap.add_argument("--rebals", default="10,5")
    ap.add_argument("--cost", type=float, default=0.0015)
    args = ap.parse_args()
    rebals = [int(x) for x in args.rebals.split(",")]

    end = pd.Timestamp.today().strftime("%Y-%m-%d")
    conn = fe.get_conn()
    uni, list_dates = fe.load_universe_filter(conn)
    daily_ret = fe.load_daily_returns(conn, "2015-10-01", end)
    idx = daily_ret.index
    base_fw = {n: sl.load_factor(conn, n, "2015-06-01", end).astype("float32")
               for n in ["ln_mv", "ep_ttm", "ivol_60", "sue_delta"]}
    print("加载换手率宽表 ...", flush=True)
    turn_wide = load_turnover_wide("2015-06-01", end)
    conn.close()

    # sanity: 内存重算的 20 日反转 vs PG 因子，截面秩相关
    my20 = ret_w_rev(daily_ret, 20)
    conn = fe.get_conn()
    pg20 = sl.load_factor(conn, "ret_20d_rev", "2015-06-01", end)
    conn.close()
    rs = []
    for t in my20.index[-300:]:
        if t in pg20.index:
            a, b = my20.loc[t].dropna(), pg20.loc[t].dropna()
            ix = a.index.intersection(b.index)
            if len(ix) > 200:
                rs.append(a[ix].rank().corr(b[ix].rank()))
    print(f"  sanity: 内存 ret_20d_rev vs PG 因子 秩相关中位 {np.nanmedian(rs):.3f} "
          f"(n={len(rs)} 期)", flush=True)

    rows = []
    for w in rebals:
        fe.REBAL = w
        variants = {
            "baseline": dict(base_fw, ret_20d_rev=pg20, turnover_20=turn_wide.rolling(
                20, min_periods=15).mean().mul(-1.0).astype("float32")),
            "matched": dict(base_fw, **{
                f"ret_{w}d_rev": ret_w_rev(daily_ret, w),
                f"turnover_{w}": turn_wide.rolling(w, min_periods=max(3, int(w * 0.75)))
                .mean().mul(-1.0).astype("float32")}),
        }
        for vname, fw in variants.items():
            sec = SectionZ(fw, daily_ret, uni, list_dates, mode="raw")
            t0 = time.time()
            for off in range(w):
                g = fws.offset_grid(idx, args.anchor, off, end)
                rb = [t for t in g if t >= pd.Timestamp(args.anchor)]
                nav, turn, n_per = backtest_light(sec, daily_ret, rb, cost=args.cost, period=w)
                if nav.empty:
                    continue
                s, so = fws.stats(nav), fws.stats(nav[nav.index >= OOS])
                rows.append({"rebal": w, "variant": vname, "offset": off,
                             "ann": s["ann"], "sharpe": s["sharpe"], "dd": s["dd"],
                             "oos_sharpe": so.get("sharpe", np.nan),
                             "turn_per_period": turn,
                             "ann_cost": turn * 2 * args.cost * (244.0 / w)})
            d = pd.DataFrame([r for r in rows if r["rebal"] == w and r["variant"] == vname])
            print(f"  W={w:2d} {vname:9s} | 年化均值 {pct(d['ann'].mean())} "
                  f"离散 {d['ann'].std()*100:.1f}pp 夏普 {d['sharpe'].mean():.2f} "
                  f"换手 {d['turn_per_period'].mean()*100:.0f}% "
                  f"年成本 {d['ann_cost'].mean()*100:.2f}% ({time.time()-t0:.0f}s)", flush=True)
            del sec

    df = pd.DataFrame(rows)
    L = [f"# 因子窗口与持有期对齐测试 (anchor={args.anchor}, 单边成本 {args.cost*100:.2f}%)", "",
         f"> {pd.Timestamp.today().date()} | 组合层 Top30 + min_var_cap10 | 相位 = 完整周期 offset 0..W-1", "",
         "| 持有期 | 变体 | 年化均值 | 年化离散 | 最差相位 | 夏普均值 | 单期换手 | 年化成本 |",
         "|---|---|---|---|---|---|---|---|"]
    for w in rebals:
        for vname in ("baseline", "matched"):
            d = df[(df["rebal"] == w) & (df["variant"] == vname)]
            if d.empty:
                continue
            L.append(f"| {w}日 | {vname} | **{pct(d['ann'].mean())}** | {d['ann'].std()*100:.1f}pp | "
                     f"{pct(d['ann'].min())} | {d['sharpe'].mean():.2f} | "
                     f"{d['turn_per_period'].mean()*100:.0f}% | {d['ann_cost'].mean()*100:.2f}% |")
    L += ["", "## 判读", ""]
    for w in rebals:
        a = df[(df["rebal"] == w) & (df["variant"] == "baseline")]["ann"].mean()
        b = df[(df["rebal"] == w) & (df["variant"] == "matched")]["ann"].mean()
        L.append(f"- {w}日：窗口对齐 {(b-a)*100:+.1f}pp（{pct(a)} → {pct(b)}）")
    out = os.path.join(REPO, "reports", "rebal_window_match_test.md")
    with open(out, "w") as f:
        f.write("\n".join(L) + "\n")
    with open(os.path.join(REPO, "outputs", "rebal_window_match_test.json"), "w") as f:
        json.dump({"anchor": args.anchor, "cost": args.cost, "rows": rows}, f,
                  ensure_ascii=False, indent=2)
    print("\n" + "\n".join(L))
    print(f"\n✅ {out}")


if __name__ == "__main__":
    main()

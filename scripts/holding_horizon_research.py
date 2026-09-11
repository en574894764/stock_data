#!/usr/bin/env python3
"""持有期限结构调研: "持有期从 20 天拉到 1/2/3 个月, 垃圾股过滤的价值是否上升?"
====================================================================================
纯诊断脚本 (不改策略代码, 不囤新数据, 只用现有 PG):

① 垃圾股的期限曲线: baseline Top30 每期持仓分"垃圾组"(亏损 或 上期Q1年化ROE<0)
   vs "干净组", 计算各自在不同持有期 (5/10/20/40/60/120 交易日) 的实际前向收益。
   若垃圾组的劣势随持有期扩大 → James 的假设成立。

② 因子 IC 期限衰减: 6 因子对前向收益的 RankIC 随持有期 (10/20/40/60/120) 的变化。
   拉长持有期不只是"垃圾要不要过滤"的问题, 是整个引擎燃料 (反转因子) 的半衰期问题。

口径: 前向收益 = 信号日 t 之后 h 个交易日的累计收益 (等权不加成本); IC 在调仓网格上计算
(相邻日期前向窗口有重叠, 自相关会推高显著性, 但均值方向可信)。

用法: python3 scripts/holding_horizon_research.py
输出: reports/holding_horizon_research.md
"""
import os
import sys

import numpy as np
import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import factor_eval as fe  # noqa: E402
import strategy_lib as sl  # noqa: E402
from backtest_report import replay  # noqa: E402
from junk_filter_test import load_fin_events, asof_snapshot  # noqa: E402

START = "2019-01-01"
TOPN = 30
HORIZONS = [5, 10, 20, 40, 60, 120]


def fwd_ret(daily_ret, t, codes, h):
    idx = daily_ret.index
    pos = idx.get_indexer([t])
    if len(pos) == 0 or pos[0] < 0 or pos[0] + 1 + h > len(idx):
        return np.nan, 0
    seg = daily_ret.iloc[pos[0] + 1: pos[0] + 1 + h]
    cols = [c for c in codes if c in seg.columns]
    if not cols:
        return np.nan, 0
    per_stock = (1 + seg[cols]).prod() - 1
    return float(per_stock.mean()), len(cols)


def main():
    end = pd.Timestamp.today().strftime("%Y-%m-%d")
    conn = fe.get_conn()
    uni, list_dates = fe.load_universe_filter(conn)
    st = sl.load_strategies(conn, [f"prod_6f_eq@topn={TOPN}"])[0]
    factors = {n: sl.load_factor(conn, n, START, end) for n in st["cfg"]["factors"]}
    ep_wide = sl.load_factor(conn, "ep_ttm", START, end)
    ev = load_fin_events(conn)
    load_start = (pd.Timestamp(START) - pd.Timedelta(days=40)).strftime("%Y-%m-%d")
    daily_ret = fe.load_daily_returns(conn, load_start, end)
    rebal = fe.rebalance_dates(daily_ret.index, START, end)

    # ---------- baseline picks + 垃圾标记 ----------
    orig = sl.score_cross
    picks_log = {}

    def patched(fwides, weights, t, uni_, ld, _mc=50):
        score, zdf = orig(fwides, weights, t, uni_, ld, _mc)
        if score is not None:
            sel, _ = sl.select_stocks(score.dropna(), TOPN)
            picks_log[t] = sel
        return score, zdf

    sl.score_cross = patched
    replay(st, factors, daily_ret, uni, list_dates, rebal)
    sl.score_cross = orig
    conn.close()

    # ---------- ① 垃圾组 vs 干净组 期限曲线 ----------
    junk_by_h = {h: [] for h in HORIZONS}
    clean_by_h = {h: [] for h in HORIZONS}
    n_junk = n_clean = 0
    for t, sel in picks_log.items():
        ep = ep_wide.loc[t] if t in ep_wide.index else pd.Series(dtype=float)
        try:
            roe, _ = asof_snapshot(ev, t)
        except ValueError:
            continue
        epv = ep.reindex(sel)
        rv = roe.reindex(sel)
        junk = [c for c in sel if (pd.isna(epv[c]) or epv[c] < 0)
                or (pd.notna(rv[c]) and rv[c] < 0)]
        clean = [c for c in sel if c not in junk]
        n_junk += len(junk)
        n_clean += len(clean)
        for h in HORIZONS:
            rj, _ = fwd_ret(daily_ret, t, junk, h)
            rc, _ = fwd_ret(daily_ret, t, clean, h)
            if np.isfinite(rj):
                junk_by_h[h].append(rj)
            if np.isfinite(rc):
                clean_by_h[h].append(rc)

    lines = [f"# 持有期限结构调研 (TopN{TOPN}, {START} ~ {end})\n",
             "- 垃圾组 = Top30 中 亏损(ep_ttm 无正盈利) 或 上期Q1年化ROE<0; 干净组 = 其余",
             "- 前向收益不含成本; 93 期调仓网格 (相邻前向窗口重叠, 看均值方向)\n",
             "## ① 垃圾组 vs 干净组: 不同持有期的实际收益\n",
             "| 持有期 | 垃圾组均收益 | 干净组均收益 | 垃圾-干净(bp) |", "|---|---|---|---|"]
    for h in HORIZONS:
        j = np.mean(junk_by_h[h]) * 100 if junk_by_h[h] else np.nan
        c = np.mean(clean_by_h[h]) * 100 if clean_by_h[h] else np.nan
        lines.append(f"| {h} 交易日 (~{h/4.86:.1f}月) | {j:.2f}% | {c:.2f}% | {(j-c)*100:+.0f}bp |")
        print(f"[horizon {h}d] 垃圾 {j:.2f}% vs 干净 {c:.2f}% 差 {(j-c)*100:+.0f}bp")
    lines.append(f"\n垃圾组/干净组平均每期只数: {n_junk/len(picks_log):.1f} / {n_clean/len(picks_log):.1f}")

    # ---------- ② 因子 IC 期限衰减 ----------
    lines.append("\n## ② 6 因子 RankIC 随持有期的衰减 (引擎燃料半衰期)\n")
    fnames = list(st["cfg"]["factors"].keys())
    hdr = "| 因子 | " + " | ".join(f"{h}日" for h in HORIZONS) + " |"
    lines.append(hdr)
    lines.append("|" + "---|" * (len(HORIZONS) + 1))
    ret_cols = set(daily_ret.columns)
    for fname in fnames:
        fw = factors[fname]
        row = [fname]
        for h in HORIZONS:
            ics = []
            for t in rebal:
                if t not in fw.index:
                    continue
                idx = daily_ret.index
                p = idx.get_indexer([t])[0]
                if p < 0 or p + 1 + h > len(idx):
                    continue
                fv = fw.loc[t].dropna()
                fv = fv[[c for c in fv.index if c in ret_cols]]
                if len(fv) < 100:
                    continue
                seg = daily_ret.iloc[p + 1: p + 1 + h][list(fv.index)]
                fr = (1 + seg).prod() - 1
                valid = fr.dropna()
                fv2 = fv.reindex(valid.index).dropna()
                valid = valid.reindex(fv2.index)
                if len(fv2) < 100:
                    continue
                ics.append(fv2.rank().corr(valid.rank()))
            row.append(f"{np.mean(ics)*100:.1f}%" if ics else "-")
        lines.append("| " + " | ".join(row) + " |")
        print(f"[IC] {row}")

    out = os.path.join(REPO, "reports", "holding_horizon_research.md")
    with open(out, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n✅ 报告: {out}")


if __name__ == "__main__":
    main()

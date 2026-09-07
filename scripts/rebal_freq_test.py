#!/usr/bin/env python3
"""
调仓频率对照实验：周频(5) vs 双周(10) vs 月频(20)
==================================================
问题: 月度调仓是否太长? 提频到周度是否值得?
方法: 同一组 6 因子等权组合, 只变调仓间隔, 对比 年化/夏普/回撤/换手/成本
成本口径: 与 factor_eval 一致 (单边 0.15%, 每日摊销 COST*2/REBAL)

用法: python3 scripts/rebal_freq_test.py
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import factor_eval as fe  # noqa: E402

START, END = "2019-01-01", "2026-09-01"
OOS = pd.Timestamp("2023-01-01")  # 样本外起点
SIX = ["ret_20d_rev", "turnover_20", "ivol_60", "ln_mv", "ep_ttm", "sue_delta"]


def build_combo(factors, rebal, uni, list_dates):
    """6 因子等权 z-score 合成打分 (与生产 prod_6f_eq 同口径)"""
    rows = {}
    for t in rebal:
        if any(t not in factors[n].index for n in SIX):
            continue
        zs, ok = [], True
        for n in SIX:
            row = factors[n].loc[t].dropna()
            keep = [c for c in row.index if c in uni and list_dates.get(c) is not None
                    and t >= pd.Timestamp(list_dates[c]) + pd.Timedelta(days=120)]
            row = row[keep]
            if len(row) < 50:
                ok = False
                break
            z = (row - row.mean()) / (row.std() if row.std() > 0 else 1.0)
            zs.append(z)
        if ok:
            rows[t] = pd.concat(zs, axis=1).sum(axis=1)
    return pd.DataFrame(rows).T.sort_index()


def q5_turnover(combo, rebal, uni, list_dates, top_n=30):
    """TopN 持仓的期际换手率: 1 - 与上期持仓重叠比例"""
    prev_set, tos = None, []
    for t in combo.index:
        row = combo.loc[t].dropna()
        keep = [c for c in row.index if c in uni and list_dates.get(c) is not None
                and t >= pd.Timestamp(list_dates[c]) + pd.Timedelta(days=120)]
        cur = set(row[keep].nlargest(top_n).index)
        if prev_set is not None and len(prev_set):
            tos.append(1 - len(cur & prev_set) / len(cur | prev_set) * len(cur) / max(len(cur), 1))
        prev_set = cur
    return np.mean(tos) if tos else np.nan


def main():
    conn = fe.get_conn()
    uni, list_dates = fe.load_universe_filter(conn)
    factors = fe.load_factors(conn, START, END)
    missing = [n for n in SIX if n not in factors]
    if missing:
        raise SystemExit(f"缺因子: {missing}")
    factors = {n: factors[n] for n in SIX}
    daily_ret = fe.load_daily_returns(conn, "2018-11-01", END)
    conn.close()

    d = daily_ret.index
    d = d[(d >= pd.Timestamp(START)) & (d <= pd.Timestamp(END))]

    print(f"{'频率':<6}{'年化':>8}{'夏普':>7}{'回撤':>8}{'IC均值':>8}{'Top30换手/期':>14}{'年成本':>8}"
          f"{'样本外年化':>12}{'样本外夏普':>12}")
    print("-" * 100)
    out_lines = ["# 调仓频率对照实验 (6因子等权, 2019-2026)\n",
                 "| 频率 | 年化 | 夏普 | 回撤 | IC均值 | Top30单期换手 | 年化成本 | 样本外年化 | 样本外夏普 |",
                 "|---|---|---|---|---|---|---|---|---|"]

    for interval, label in [(5, "周频"), (10, "双周"), (20, "月频")]:
        fe.REBAL = interval  # evaluate_single 内部按模块常量摊销成本/取持有期
        fwd = fe.fwd_from_daily(daily_ret, horizon=interval)
        rebal = list(d[::interval])
        combo = build_combo(factors, rebal, uni, list_dates)
        r = fe.evaluate_single("combo", combo, fwd, daily_ret, rebal, uni, list_dates)
        q = r["q_navs"]
        if q is None or q.empty:
            print(f"{label}: 无结果")
            continue
        q5 = q[fe.QUANTILES]
        s = fe.nav_stats(q5)
        oos_nav = q5[q5.index >= OOS]
        oos_ret = oos_nav.pct_change().dropna()
        years = len(oos_ret) / 244
        oos_ann = (oos_nav.iloc[-1] / oos_nav.iloc[0]) ** (1 / years) - 1 if years > 0.5 else np.nan
        oos_vol = oos_ret.std() * np.sqrt(244)
        oos_sharpe = oos_ann / oos_vol if oos_vol > 0 else np.nan
        to = q5_turnover(combo, rebal, uni, list_dates)
        periods_per_year = 244 / interval
        # 单期换手 (买+卖双边=2×单向重叠变化), 成本 = 双边换手×单边费率
        ann_cost = to * 2 * periods_per_year * fe.COST
        ics = r["ics"]
        print(f"{label:<6}{s['ann_ret']:>8.1%}{s['sharpe']:>7.2f}{s['max_dd']:>8.1%}"
              f"{ics.mean():>8.1%}{to:>14.1%}{ann_cost:>8.2%}{oos_ann:>12.1%}{oos_sharpe:>12.2f}")
        out_lines.append(f"| {label}({interval}日) | {s['ann_ret']:.1%} | {s['sharpe']:.2f} | {s['max_dd']:.1%} "
                         f"| {ics.mean():.1%} | {to:.1%} | {ann_cost:.2%} | {oos_ann:.1%} | {oos_sharpe:.2f} |")

    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "reports", "rebal_freq_test.md"), "w") as f:
        f.write("\n".join(out_lines) + "\n")
    print("\n报告: reports/rebal_freq_test.md")


if __name__ == "__main__":
    main()

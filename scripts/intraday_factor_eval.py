#!/usr/bin/env python3
"""隔夜-日内结构因子: 单因子评估 + 正交性
====================================================================================
评估 8 个新因子 (compute_intraday_factors.py 已方向化入库):
  IC/ICIR/IC正率 · 五层分层年化 (Q1/Q5) · 多空 · Q5 夏普/回撤 · 样本内(2016-2022)/外(2023+) 分段
正交性: 新因子 × 现有 6 因子的截面 Spearman 相关 (调仓日均值)

用法: python3 scripts/intraday_factor_eval.py
"""
import json
import os
import sys

import numpy as np
import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import factor_eval as fe  # noqa: E402
import strategy_lib as sl  # noqa: E402

NEW = ["on_mom_20", "id_mom_20", "on_share_20", "id_on_amp_20",
       "on_vol_20", "on_skew_20", "on_rev_5", "tug_20"]
OLD = ["turnover_20", "ivol_60", "ret_20d_rev", "ln_mv", "ep_ttm", "sue_delta"]
START, END = "2016-01-01", pd.Timestamp.today().strftime("%Y-%m-%d")
SPLIT = pd.Timestamp("2023-01-01")


def orthogonality(fw, rebal, uni, list_dates):
    """新因子 × 旧因子 截面 rank 相关 (期均值)"""
    acc = {n: {m: [] for m in OLD} for n in NEW}
    for t in rebal:
        rows = {}
        for name, src in [(n, fw["new"]) for n in NEW] + [(m, fw["old"]) for m in OLD]:
            f = src.get(name)
            if f is None or t not in f.index:
                continue
            r = f.loc[t].dropna()
            r = r[[c for c in r.index if c in uni and list_dates.get(c) is not None
                   and t > pd.Timestamp(list_dates[c]) + pd.Timedelta(days=120)]]
            if len(r) >= 50:
                rows[name] = r.rank()
        if len(rows) < 3:
            continue
        for n in NEW:
            if n not in rows:
                continue
            for m in OLD:
                if m not in rows:
                    continue
                common = rows[n].index.intersection(rows[m].index)
                if len(common) >= 50:
                    acc[n][m].append(rows[n][common].corr(rows[m][common]))
    return {n: {m: (np.mean(v) if v else np.nan) for m, v in d.items()} for n, d in acc.items()}


def main():
    conn = fe.get_conn()
    uni, list_dates = fe.load_universe_filter(conn)
    fw_new, fw_old = {}, {}
    for n in NEW:
        fw_new[n] = sl.load_factor(conn, n, START, END)
    for m in OLD:
        fw_old[m] = sl.load_factor(conn, m, START, END)
    daily_ret = fe.load_daily_returns(conn, "2015-10-01", END)
    fwd = fe.fwd_from_daily(daily_ret)
    rebal = fe.rebalance_dates(daily_ret.index, START, END)
    conn.close()
    print(f"股票池 {len(uni)} | 调仓 {len(rebal)} 期 | 新因子 {len(NEW)} 个")

    lines = [f"# 隔夜-日内结构因子: 单因子评估 ({START} ~ {END})\n",
             "- 5 层等权分组, 单边成本 0.15% 按持有期分摊 (与 factor_eval 同口径)",
             "- 方向已由样本内 IC 符号确定 (见 intraday_factors_ic.md)\n",
             "| 因子 | IC均值 | ICIR | IC正率 | Q1年化 | Q3年化 | Q5年化 | 多空 | Q5夏普 | Q5回撤 |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    out_json = {}
    for n in NEW:
        r = fe.evaluate_single(n, fw_new[n], fwd, daily_ret, rebal, uni, list_dates)
        ics, q = r["ics"], r["q_navs"]
        if q is None or q.empty:
            lines.append(f"| {n} | 无分层结果 | | | | | | | | |")
            continue
        s1, s3, s5 = fe.nav_stats(q[1]), fe.nav_stats(q[3]), fe.nav_stats(q[5])
        ls = fe.nav_stats(q[5] / q[1])
        m, sd = ics.mean(), ics.std()
        ir = m / sd if sd and sd > 0 else np.nan
        out_json[n] = {"ic": float(m), "icir": float(ir), "ic_pos": float((ics > 0).mean()),
                       "q1": float(s1["ann_ret"]), "q3": float(s3["ann_ret"]), "q5": float(s5["ann_ret"]),
                       "ls": float(ls["ann_ret"]), "q5_sharpe": float(s5["sharpe"]),
                       "q5_dd": float(s5["max_dd"])}
        lines.append(f"| {n} | {m*100:.1f}% | {ir:.2f} | {(ics>0).mean()*100:.0f}% "
                     f"| {s1['ann_ret']*100:.1f}% | {s3['ann_ret']*100:.1f}% | {s5['ann_ret']*100:.1f}% "
                     f"| {ls['ann_ret']*100:.1f}% | {s5['sharpe']:.2f} | {s5['max_dd']*100:.1f}% |")
        print(f"{n:14s} IC {m*100:5.1f}% ICIR {ir:5.2f} Q5 {s5['ann_ret']*100:6.1f}% "
              f"多空 {ls['ann_ret']*100:6.1f}% Q5夏普 {s5['sharpe']:5.2f}")

    # 参考: 现有 6 因子同口径 (对照)
    lines.append("\n## 对照: 现有 6 因子 (同口径)\n")
    lines.append("| 因子 | IC均值 | ICIR | Q5年化 | 多空 | Q5夏普 |")
    lines.append("|---|---|---|---|---|---|")
    for m in OLD:
        r = fe.evaluate_single(m, fw_old[m], fwd, daily_ret, rebal, uni, list_dates)
        ics, q = r["ics"], r["q_navs"]
        if q is None or q.empty:
            continue
        s5 = fe.nav_stats(q[5])
        ls = fe.nav_stats(q[5] / q[1])
        m_ic, sd = ics.mean(), ics.std()
        lines.append(f"| {m} | {m_ic*100:.1f}% | {m_ic/sd:.2f} | {s5['ann_ret']*100:.1f}% "
                     f"| {ls['ann_ret']*100:.1f}% | {s5['sharpe']:.2f} |")
        print(f"[旧] {m:14s} IC {m_ic*100:5.1f}% Q5 {s5['ann_ret']*100:6.1f}% 多空 {ls['ann_ret']*100:6.1f}%")

    # 样本内/外
    lines.append("\n## 样本内(2016-2022) / 样本外(2023+) 稳定性\n")
    lines.append("| 因子 | 内IC | 外IC | 内Q5年化 | 外Q5年化 | 外Q5夏普 |")
    lines.append("|---|---|---|---|---|---|")
    for n in NEW:
        r = fe.evaluate_single(n, fw_new[n], fwd, daily_ret, rebal, uni, list_dates)
        ics, q = r["ics"], r["q_navs"]
        if q is None or q.empty:
            continue
        i_ic = ics[ics.index < SPLIT].mean()
        o_ic = ics[ics.index >= SPLIT].mean()
        i5 = fe.nav_stats(q[5][q[5].index < SPLIT])
        o5 = fe.nav_stats(q[5][q[5].index >= SPLIT])
        lines.append(f"| {n} | {i_ic*100:.1f}% | {o_ic*100:.1f}% | {i5['ann_ret']*100:.1f}% "
                     f"| {o5['ann_ret']*100:.1f}% | {o5['sharpe']:.2f} |")

    # 正交性
    print("\n正交性 (新 × 旧, 截面 rank 相关) ...")
    orth = orthogonality({"new": fw_new, "old": fw_old}, rebal, uni, list_dates)
    lines.append("\n## 正交性: 与现有 6 因子的截面 rank 相关 (期均值)\n")
    lines.append("| 因子 | " + " | ".join(OLD) + " | 最大|相关| |")
    lines.append("|---" * (len(OLD) + 2) + "|")
    for n in NEW:
        vals = [orth[n][m] for m in OLD]
        lines.append(f"| {n} | " + " | ".join(f"{v:+.2f}" for v in vals) +
                     f" | {max(abs(v) for v in vals):.2f} |")
    out_json["orthogonality"] = orth

    out_md = os.path.join(REPO, "reports", "intraday_factor_eval.md")
    with open(out_md, "w") as f:
        f.write("\n".join(lines) + "\n")
    out_js = os.path.join(REPO, "outputs", "intraday_factor_eval.json")
    os.makedirs(os.path.dirname(out_js), exist_ok=True)
    with open(out_js, "w") as f:
        json.dump(out_json, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n✅ 报告: {out_md}\n✅ JSON: {out_js}")


if __name__ == "__main__":
    main()

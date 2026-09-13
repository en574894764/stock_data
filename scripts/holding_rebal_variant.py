#!/usr/bin/env python3
"""持有期 20 vs 40 天变体对照 (P0#6)
====================================================================================
依据 holding_horizon_research 证据: 慢因子 (turnover_20/ivol_60/ln_mv) 的 IC 在
40-60 天比 20 天更强, ret_20d_rev 峰值 40 天 → 持有期拉长到 40 天或可降换手成本、稳净值。

做法: 复用 backtest_report.replay, 通过 monkeypatch fe.REBAL 改变调仓频率=持有期。
  REBAL=20 → 每20交易日调仓持20日 (月度) ; REBAL=40 → 每40交易日调仓持40日 (双月)
  两策略: prod_6f_eq@topn=30 (baseline) × prod_lgbm_neu (LGBM+中性化)
  end 提前 40 交易日, 保证两种 REBAL 的最后调仓日都有完整前向数据 (公平对照)

用法: python3 scripts/holding_rebal_variant.py [--out path]
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

START = "2019-01-01"
STRATEGIES = ["prod_6f_eq@topn=30", "prod_lgbm_neu"]
REBALS = [20, 40]


def stats(nav: pd.Series) -> dict:
    ret = nav.pct_change().dropna()
    if len(ret) < 60:
        return {}
    years = len(ret) / 244
    ann = (nav.iloc[-1] / nav.iloc[0]) ** (1 / years) - 1
    vol = ret.std() * np.sqrt(244)
    dd = (nav / nav.cummax() - 1).min()
    return {"ann": ann, "vol": vol, "sharpe": ann / vol if vol > 0 else np.nan, "dd": dd}


def fmt(v):
    return "-" if v is None or not np.isfinite(v) else f"{v*100:.1f}%"


def run_one(conn, st, rebal_val):
    old = fe.REBAL
    fe.REBAL = rebal_val
    try:
        uni, list_dates = fe.load_universe_filter(conn)
        # 端到端: 因子含未来 lgbm_score 依赖今日数据, 提前 40 交易日保证前向完整
        end = (pd.Timestamp.today() - pd.Timedelta(days=rebal_val * 2)).strftime("%Y-%m-%d")
        load_start = (pd.Timestamp(START) - pd.Timedelta(days=rebal_val)).strftime("%Y-%m-%d")
        daily_ret = fe.load_daily_returns(conn, load_start, end)
        rebal = fe.rebalance_dates(daily_ret.index, START, end)
        factors = {n: sl.load_factor(conn, n, START, end) for n in st["cfg"]["factors"]}
        r = replay(st, factors, daily_ret, uni, list_dates, rebal)
        return r["nav"], len(rebal)
    finally:
        fe.REBAL = old


def main():
    conn = fe.get_conn()
    strategies = sl.load_strategies(conn, STRATEGIES)
    conn.close()

    lines = [f"# 持有期 20 vs 40 天变体对照 ({START} ~ 今日-40交易日)\n",
             "- REBAL=20: 每20交易日调仓持20日 (月度) | REBAL=40: 每40交易日调仓持40日 (双月)",
             "- 复用 backtest_report.replay, monkeypatch fe.REBAL; end 提前保证前向完整\n",
             "| 策略 | 持有期 | 年化 | 夏普 | 最大回撤 | 调仓期数 |", "|---|---|---|---|---|---|"]
    results = {}
    for st in strategies:
        for rb in REBALS:
            conn = fe.get_conn()
            nav, n_rebal = run_one(conn, st, rb)
            conn.close()
            s = stats(nav)
            results[(st["label"], rb)] = nav
            lines.append(f"| {st['label']} | {rb}天 | {fmt(s['ann'])} | {s['sharpe']:.2f} "
                         f"| {fmt(s['dd'])} | {n_rebal} |")
            print(f"[{st['label']} {rb}天] 年化 {fmt(s['ann'])} 夏普 {s['sharpe']:.2f} 回撤 {fmt(s['dd'])}")

    # 年度收益 (lgbm 20 vs 40)
    lines.append("\n## LGBM+中性化: 分年收益 (20天 vs 40天)\n")
    lines.append("| 年份 | 20天 | 40天 |")
    lines.append("|---|---|---|")
    for st in strategies:
        if st["label"].startswith("LGBM"):
            n20, n40 = results[(st["label"], 20)], results[(st["label"], 40)]
            for y in sorted(set(n20.index.year) & set(n40.index.year)):
                a20 = n20[n20.index.year == y].iloc[-1] / n20[n20.index.year == y].iloc[0] - 1
                a40 = n40[n40.index.year == y].iloc[-1] / n40[n40.index.year == y].iloc[0] - 1
                lines.append(f"| {y} | {fmt(a20)} | {fmt(a40)} |")

    report = "\n".join(lines)
    print("\n" + report)
    out = os.path.join(REPO, "reports", "holding_rebal_variant.md")
    with open(out, "w") as f:
        f.write(report + "\n")
    print(f"\n✅ 报告: {out}")


if __name__ == "__main__":
    main()

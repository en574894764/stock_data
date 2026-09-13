#!/usr/bin/env python3
"""选股区窗口扫描 (P0#1)
====================================================================================
路线图问题 #1: TopN 选股落在收益衰减带 —— 最极端 z 分叠加股 (超跌×高换手×小盘) 的
20 日收益反不如次极端区 (最极端前5% 年化 22.6% vs 次极端 31.7%, -9.2pp/年)。

做法: 复用 backtest_report.replay 的 window 支持, 扫描"跳过最极端 K%"的多种截断,
      找最优窗口。window=[lo,hi] 语义: 只在 rank 百分位 [lo,hi] 内取 top_n。

用法: python3 scripts/selection_window_scan.py [--strategy prod_6f_eq prod_lgbm_neu]
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
# 窗口扫描集: None=Top30 基准; [0,hi]="跳过最极端 (100-hi)%"; [lo,hi]="跳过最极端+下界"
WINDOWS = [
    ("Top30 基准", None),
    ("跳过最极端1%", [0, 99]),
    ("跳过最极端2%", [0, 98]),
    ("跳过最极端3%", [0, 97]),
    ("跳过最极端5%", [0, 95]),
    ("跳过最极端7%", [0, 93]),
    ("跳过最极端10%", [0, 90]),
    ("P90-98 区", [90, 98]),
    ("P92-98 区", [92, 98]),
]


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


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", action="append", default=None)
    args = ap.parse_args()
    specs = args.strategy or ["prod_6f_eq@topn=30", "prod_lgbm_neu"]

    conn = fe.get_conn()
    uni, list_dates = fe.load_universe_filter(conn)
    strategies = sl.load_strategies(conn, specs)
    end = pd.Timestamp.today().strftime("%Y-%m-%d")
    load_start = (pd.Timestamp(START) - pd.Timedelta(days=40)).strftime("%Y-%m-%d")
    daily_ret = fe.load_daily_returns(conn, load_start, end)
    rebal = fe.rebalance_dates(daily_ret.index, START, end)
    print(f"股票池 {len(uni)} | 调仓 {len(rebal)} 期 ({rebal[0].date()} ~ {rebal[-1].date()})")

    lines = [f"# 选股区窗口扫描 ({START} ~ {end})\n",
             "- 每个策略×窗口跑 replay (同引擎同成本同选股, 仅 window 不同)\n"]
    for st in strategies:
        factors = {n: sl.load_factor(conn, n, START, end) for n in st["cfg"]["factors"]}
        lines.append(f"\n## {st['label']}\n")
        lines.append("| 窗口 | 年化 | 夏普 | 最大回撤 | 说明 |")
        lines.append("|---|---|---|---|---|")
        print(f"\n[{st['label']}]")
        for label, win in WINDOWS:
            cfg = dict(st["cfg"])
            cfg["window"] = win
            r = replay({"cfg": cfg}, factors, daily_ret, uni, list_dates, rebal)
            s = stats(r["nav"])
            note = "基准" if win is None else f"rank∈[{win[0]}%,{win[1]}%]取Top{cfg.get('top_n',30)}"
            lines.append(f"| {label} | {fmt(s['ann'])} | {s['sharpe']:.2f} | {fmt(s['dd'])} | {note} |")
            print(f"  {label:12s} 年化 {fmt(s['ann'])} 夏普 {s['sharpe']:.2f} 回撤 {fmt(s['dd'])}")

    conn.close()
    report = "\n".join(lines)
    out = os.path.join(REPO, "reports", "selection_window_scan.md")
    with open(out, "w") as f:
        f.write(report + "\n")
    print(f"\n✅ 报告: {out}")


if __name__ == "__main__":
    main()

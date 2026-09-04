#!/usr/bin/env python3
"""
多策略组合回测器 (多策略 × 多标的)
====================================
消费统一输出契约的日收益序列 (各引擎产出的 *_returns.parquet, 列=子策略),
按目标权重加权合成组合日收益, 输出绩效 + 相关矩阵 + 各策略贡献。

子策略 = 资产: 可以是时序引擎的单标的策略 (backtest_ts)、截面引擎的因子组合
(factor_eval Q5)、或任何产出日收益序列的东西。

用法:
    # 1. 消费时序引擎产物 (backtest_ts --out 产出 *_returns.parquet)
    python3 scripts/combo_backtest.py \
        --returns reports/ts_trend_200d_returns.parquet reports/ts_ma_cross_returns.parquet \
        --weights 0.6,0.4 --out reports/combo_demo.md

    # 2. 内置模式: 直接在进程内跑多个时序策略再组合 (免先跑 backtest_ts)
    python3 scripts/combo_backtest.py \
        --ts 510300:trend:200 510500:trend:200 159915:ma_cross:20,60 \
        --out reports/combo_demo.md

    # 3. 等权 (默认): 所有不带 --weights 的输入均分权重
"""
import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backtest_ts import run_ts, calc_stats, ENGINE  # noqa: E402

YEAR_BARS = 244


def load_returns(paths: list) -> pd.DataFrame:
    """多个 *_returns.parquet 合并对齐 (外连接取并集, 缺失日填 NaN→0 前先看覆盖)."""
    dfs = []
    for p in paths:
        df = pd.read_parquet(p)
        if isinstance(df, pd.Series):
            df = df.to_frame(name=os.path.basename(p).replace("_returns.parquet", ""))
        dfs.append(df)
    out = pd.concat(dfs, axis=1)
    out.index = pd.to_datetime(out.index)
    return out.sort_index()


def run_combo(returns: pd.DataFrame, weights=None, name_prefix="") -> dict:
    """加权合成. weights: 与列对齐的数组; 日度再平衡 (恒定权重近似, 组合研究的标准做法)."""
    rets = returns.fillna(0.0)
    if weights is None:
        weights = np.full(rets.shape[1], 1.0 / rets.shape[1])
    w = pd.Series(weights, index=rets.columns)
    port = (rets * w).sum(axis=1)

    # 各子策略统计
    sub_stats = {}
    for c in rets.columns:
        s = calc_stats(rets[c])
        s["weight"] = w[c]
        s["contrib"] = calc_stats(rets[c] * w[c])["ann_ret"]  # 权重×子策略年化贡献
        sub_stats[c] = s

    # 相关矩阵 (子策略间)
    corr = rets.corr() if rets.shape[1] > 1 else None

    # 分年度收益 (组合)
    yearly = port.groupby(port.index.year).apply(lambda x: (1 + x).prod() - 1)

    return {
        "portfolio_returns": port,
        "stats": calc_stats(port),
        "sub_stats": pd.DataFrame(sub_stats).T,
        "corr": corr,
        "yearly": yearly,
        "weights": w,
    }


def fmt_pct(v):
    return "-" if v is None or (isinstance(v, float) and np.isnan(v)) else f"{v*100:.1f}%"


def render_report(res: dict, title: str) -> str:
    s = res["stats"]
    lines = [f"# 组合回测报告: {title}\n"]
    lines.append(f"- 子策略数: {len(res['weights'])} | 加权方式: 日度再平衡 (恒定权重)")
    lines.append(f"- 组合绩效: 年化 {fmt_pct(s['ann_ret'])} | 波动 {fmt_pct(s['ann_vol'])} | "
                 f"夏普 {s['sharpe']:.2f} | 最大回撤 {fmt_pct(s['max_dd'])} | 累计 {fmt_pct(s['total'])}\n")

    lines.append("## 子策略明细\n")
    ss = res["sub_stats"]
    lines.append("| 子策略 | 权重 | 年化 | 波动 | 夏普 | 回撤 | 年化贡献 |")
    lines.append("|---|---|---|---|---|---|---|")
    for name, row in ss.iterrows():
        lines.append(f"| {name} | {row['weight']*100:.0f}% | {fmt_pct(row['ann_ret'])} | "
                     f"{fmt_pct(row['ann_vol'])} | {row['sharpe']:.2f} | {fmt_pct(row['max_dd'])} | "
                     f"{fmt_pct(row['contrib'])} |")

    if res["corr"] is not None:
        lines.append("\n## 子策略相关矩阵\n")
        lines.append("| | " + " | ".join(res["corr"].columns) + " |")
        lines.append("|---" * (len(res["corr"]) + 1) + "|")
        for idx, row in res["corr"].iterrows():
            lines.append(f"| {idx} | " + " | ".join(f"{v:.2f}" for v in row) + " |")

    lines.append("\n## 分年度收益\n")
    lines.append("| 年份 | 组合收益 |")
    lines.append("|---|---|")
    for y, r in res["yearly"].items():
        lines.append(f"| {y} | {fmt_pct(r)} |")

    return "\n".join(lines) + "\n"


def parse_ts_spec(spec: str):
    """'510300:trend:200' → (symbol, strategy, {window:200}); 参数段可省略."""
    parts = spec.split(":")
    symbol, strategy = parts[0], parts[1]
    params = {}
    if len(parts) > 2:
        for kv in parts[2].split(","):
            k, v = kv.split("=")
            params[k] = float(v)
    return symbol, strategy, params


def main():
    ap = argparse.ArgumentParser(description="多策略×多标的组合回测")
    ap.add_argument("--returns", nargs="*", default=[], help="各引擎产出的 *_returns.parquet 路径")
    ap.add_argument("--ts", nargs="*", default=[], help="内置时序策略: symbol:strategy[:k=v,k=v] (可多个)")
    ap.add_argument("--weights", default=None, help="逗号分隔权重 (与输入列序一致); 默认等权")
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--end", default="2026-09-01")
    ap.add_argument("--fees", type=float, default=0.0015)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    if not args.returns and not args.ts:
        ap.error("至少提供 --returns 或 --ts 之一")

    frames, holdings = [], []
    if args.ts:
        for spec in args.ts:
            symbol, strategy, params = parse_ts_spec(spec)
            r = run_ts(symbol, strategy, args.start, args.end, params, fees=args.fees)
            col = f"{symbol}:{strategy}"
            frames.append(r["daily_returns"].rename(col))
            holdings.append(r["holdings"])
            s = r["stats"]
            print(f"{col}: 年化 {s['ann_ret']:.1%} 夏普 {s['sharpe']:.2f} 回撤 {s['max_dd']:.1%}")

    if args.returns:
        for p in args.returns:
            df = pd.read_parquet(p)
            if isinstance(df, pd.Series):
                df = df.to_frame(name=os.path.basename(p).replace("_returns.parquet", ""))
            for c in df.columns:
                frames.append(df[c].rename(c if len(args.returns) == 1 else f"{os.path.basename(p)[:-17]}:{c}"))

    returns = pd.concat(frames, axis=1)
    returns.index = pd.to_datetime(returns.index)
    returns = returns.sort_index()
    # 覆盖提示: 起点不同的子策略, 前期 NaN 按 0 处理但打印警告
    starts = returns.apply(lambda c: c.first_valid_index())
    if starts.nunique() > 1:
        print("⚠️ 子策略起点不一致 (NaN 期按 0 收益参与加权):")
        for c, d in starts.items():
            print(f"  {c}: {str(d)[:10] if d is not None else '全空'}")

    weights = None
    if args.weights:
        vals = [float(x) for x in args.weights.split(",")]
        if len(vals) != returns.shape[1]:
            ap.error(f"权重数 {len(vals)} ≠ 子策略列数 {returns.shape[1]}")
        weights = vals

    res = run_combo(returns, weights)
    title = f"{returns.shape[1]} 子策略 · " + ("等权" if weights is None else "自定义权重")
    report = render_report(res, title)
    print("\n" + report)

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w") as f:
            f.write(report)
        print(f"报告已写入: {args.out}")
        rp = args.out.replace(".md", "_returns.parquet")
        res["portfolio_returns"].to_frame("portfolio").to_parquet(rp)
        print(f"组合日收益: {rp}")
        if holdings:
            hp = args.out.replace(".md", "_holdings.csv")
            pd.concat(holdings).to_csv(hp, index=False)
            print(f"持仓快照: {hp}")


if __name__ == "__main__":
    main()

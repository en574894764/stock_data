#!/usr/bin/env python3
"""价值投资路径的因子论证引擎 (A股)

构造价值因子体系 + 质量/规模因子, 复用 factor_eval 的底层引擎做 IC/IR + 分层回测:
  - 价值四因子: EP(1/pe_ttm)  BP(1/pb)  DP(股息率dv_ttm)  SP(1/ps_ttm)
  - 质量因子: roe_lf
  - 规模因子: ln_mv (注意方向: -ln(总市值), 值大=小盘)
  - 组合: 纯价值(EP+BP+DP+SP 等权)  vs  价值+质量(价值4+ROE)

输出: 终端表格 + outputs/value_factor_eval.json
"""
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.factor_eval import (
    get_conn, load_universe_filter, load_daily_returns, fwd_from_daily,
    rebalance_dates, evaluate_single, nav_stats,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
START, END = "2016-01-01", "2026-09-01"
REBAL = 20


def load_factor_parquet(name):
    w = pd.read_parquet(os.path.join(ROOT, "factor_cache", f"{name}.parquet"))
    w.index = pd.to_datetime(w.index)
    return w.sort_index(axis=1)


def build_value_factors(conn, start):
    """从 daily_basic 构造 BP / DP / SP 宽表 (index=datetime, col=ts_code)"""
    sql = (f"SELECT trade_date, ts_code, pb, dv_ttm, ps_ttm FROM daily_basic "
           f"WHERE trade_date >= '{start}'")
    db = pd.read_sql(sql, conn)
    db["bp"] = np.where(db["pb"] > 0, 1.0 / db["pb"], np.nan)
    db["dp"] = db["dv_ttm"] / 100.0   # tushare dv_ttm 单位 %, 转小数
    db["sp"] = np.where(db["ps_ttm"] > 0, 1.0 / db["ps_ttm"], np.nan)
    out = {}
    for col in ["bp", "dp", "sp"]:
        w = db.pivot_table(index="trade_date", columns="ts_code", values=col, aggfunc="last")
        w.index = pd.to_datetime(w.index)
        out[col] = w.sort_index().sort_index(axis=1)
    return out


def zscore(row):
    row = row.dropna().astype(float)
    return (row - row.mean()) / (row.std() if row.std() > 0 else 1.0)


def build_combo(combo_names, factors, rebal, uni, list_dates):
    """截面 z-score 等权合成组合因子宽表"""
    rows = {}
    for t in rebal:
        zs = []
        ok = True
        for name in combo_names:
            f = factors[name]
            if t not in f.index:
                ok = False
                break
            row = f.loc[t]
            keep = [c for c in row.index if c in uni and list_dates.get(c) is not None
                    and t >= pd.Timestamp(list_dates[c]) + pd.Timedelta(days=120)]
            row = row[keep]
            if len(row) < 50:
                ok = False
                break
            zs.append(zscore(row))
        if not ok or not zs:
            continue
        rows[t] = pd.concat(zs, axis=1).sum(axis=1)
    w = pd.DataFrame(rows).T.sort_index()
    return w


def fmt(v, pct=True):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "-"
    return f"{v*100:.1f}%" if pct else f"{v:.2f}"


def main():
    conn = get_conn()
    uni, list_dates = load_universe_filter(conn)
    print(f"股票池: {len(uni)} 只 (沪深非ST)")

    factors = {}
    for name in ["ep_ttm", "roe_lf", "ln_mv"]:
        factors[name] = load_factor_parquet(name)
    factors.update(build_value_factors(conn, START))
    print(f"因子: {list(factors.keys())}")

    load_start = (pd.Timestamp(START) - pd.Timedelta(days=40)).strftime("%Y-%m-%d")
    daily_ret = load_daily_returns(conn, load_start, END)
    fwd = fwd_from_daily(daily_ret)
    rebal = rebalance_dates(daily_ret.index, START, END)
    print(f"调仓日: {len(rebal)} 期 ({rebal[0].date()} ~ {rebal[-1].date()})")

    # 组合
    factors["value4"] = build_combo(["ep_ttm", "bp", "dp", "sp"], factors, rebal, uni, list_dates)
    factors["value_quality"] = build_combo(
        ["ep_ttm", "bp", "dp", "sp", "roe_lf"], factors, rebal, uni, list_dates)

    results = {}
    print("\n=== 单因子 / 组合: IC/IR 与分层回测 ===\n")
    print(f"{'因子':<14}{'IC均值':>8}{'ICstd':>8}{'IR':>7}{'IC正率':>8}{'Q1年化':>9}{'Q3年化':>9}{'Q5年化':>9}{'多空':>9}{'Q5夏普':>7}{'Q5回撤':>9}")
    for name in ["ep_ttm", "bp", "dp", "sp", "roe_lf", "ln_mv", "value4", "value_quality"]:
        if name not in factors or factors[name].empty:
            continue
        r = evaluate_single(name, factors[name], fwd, daily_ret, rebal, uni, list_dates)
        ics = r["ics"]
        q = r["q_navs"]
        if q is None or q.empty:
            print(f"{name}: 无分层结果")
            continue
        s1, s5 = nav_stats(q[1]), nav_stats(q[5])
        ls = nav_stats(q[5] / q[1])
        ic_mean, ic_std = ics.mean(), ics.std()
        ir = ic_mean / ic_std if ic_std and ic_std > 0 else np.nan
        pos = (ics > 0).mean()
        results[name] = {
            "ic_mean": float(ic_mean), "ic_std": float(ic_std), "ir": float(ir) if not np.isnan(ir) else None,
            "ic_pos": float(pos),
            "q1": float(s1["ann_ret"]), "q3": float(nav_stats(q[3])["ann_ret"]), "q5": float(s5["ann_ret"]),
            "ls": float(ls["ann_ret"]), "q5_sharpe": float(s5["sharpe"]), "q5_dd": float(s5["max_dd"]),
            "q5_total": float(s5["total"]),
        }
        print(f"{name:<14}{fmt(ic_mean):>8}{fmt(ic_std):>8}{fmt(ir, False):>7}{fmt(pos):>8}"
              f"{fmt(s1['ann_ret']):>9}{fmt(nav_stats(q[3])['ann_ret']):>9}{fmt(s5['ann_ret']):>9}"
              f"{fmt(ls['ann_ret']):>9}{fmt(s5['sharpe'], False):>7}{fmt(s5['max_dd']):>9}")

    # 单调性 (value4 组合的 5 组净值)
    v4 = evaluate_single("value4", factors["value4"], fwd, daily_ret, rebal, uni, list_dates)
    q5_navs = v4["q_navs"]
    if q5_navs is not None:
        print("\n=== 纯价值组合 value4 的 5 层年化 (验证单调性) ===")
        for qnum in range(1, 6):
            st = nav_stats(q5_navs[qnum])
            print(f"  Q{qnum}: 年化 {fmt(st['ann_ret'])} | 夏普 {fmt(st['sharpe'], False)} | 回撤 {fmt(st['max_dd'])}")

    # 基准
    bench_df = pd.read_sql(
        f"SELECT trade_date, pct_chg FROM index_daily WHERE symbol='000300.SH' "
        f"AND trade_date >= '{load_start}' AND trade_date <= '{END}'", conn)
    bench_df["trade_date"] = pd.to_datetime(bench_df["trade_date"])
    bench = bench_df.set_index("trade_date")["pct_chg"].sort_index() / 100.0
    bench_nav = (1 + bench.reindex(v4["q_navs"].index).fillna(0)).cumprod()
    b5 = nav_stats(bench_nav)
    results["bench_000300"] = {
        "ann_ret": float(b5["ann_ret"]), "sharpe": float(b5["sharpe"]), "max_dd": float(b5["max_dd"]),
    }
    print(f"\n基准沪深300 同期({START}~{END}): 年化 {fmt(b5['ann_ret'])} | 夏普 {fmt(b5['sharpe'], False)} | 最大回撤 {fmt(b5['max_dd'])}")

    # 样本内/外 (value4 与 value_quality)
    print("\n=== 样本内(2016-2022) / 样本外(2023-2026) 分段 ===")
    split = "2023-01-01"
    seg_results = {}
    for name in ["value4", "value_quality", "dp"]:
        r = evaluate_single(name, factors[name], fwd, daily_ret, rebal, uni, list_dates)
        seg_results[name] = {}
        for label, lo, hi in [("in", START, split), ("out", split, END)]:
            seg = r["q_navs"][5][(r["q_navs"].index >= pd.Timestamp(lo)) & (r["q_navs"].index < pd.Timestamp(hi))]
            st = nav_stats(seg) if len(seg) > 60 else {"ann_ret": np.nan, "sharpe": np.nan, "max_dd": np.nan}
            seg_results[name][label] = {"ann_ret": float(st["ann_ret"]), "sharpe": float(st["sharpe"]), "max_dd": float(st["max_dd"])}
            print(f"  {name:<14} {label}: 年化 {fmt(st['ann_ret'])} | 夏普 {fmt(st['sharpe'], False)} | 回撤 {fmt(st['max_dd'])}")

    results["segments"] = seg_results

    # 存 JSON
    os.makedirs(os.path.join(ROOT, "outputs"), exist_ok=True)
    out = os.path.join(ROOT, "outputs", "value_factor_eval.json")
    with open(out, "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    print(f"\nJSON 已写入: {out}")
    conn.close()


if __name__ == "__main__":
    main()

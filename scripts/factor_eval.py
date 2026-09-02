#!/usr/bin/env python3
"""因子有效性检验引擎

  IC/IR · 5组分层回测 · 多空价差 · 因子相关性矩阵 · 等权组合 · 样本内外分段

方法:
  - 调仓频率: 20 个交易日 (月度)
  - 信号: T 日因子值 → T+1 收盘建仓 → 持有 20 日 (前向收益 T+1 → T+21)
  - IC: 截面 Spearman rank IC
  - 分层: 因子值五等分组, 等权持有
  - 股票池: 沪深 A 股, 剔除 .BJ, 剔除当前 ST, 上市满 120 自然日
  - 成本: 单边 0.15% (含滑点), 调仓时扣除

用法:
  python3 scripts/factor_eval.py --start 2019-01-01 --end 2026-09-01
  python3 scripts/factor_eval.py --combo-only   # 只跑组合
"""
import argparse
import os
import sys

import numpy as np
import pandas as pd
import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

COST = 0.0015          # 单边成本
REBAL = 20             # 调仓间隔(交易日)
QUANTILES = 5


def get_conn():
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "/tmp"),
        port=os.environ.get("PGPORT", "5432"),
        dbname=os.environ.get("PGDATABASE", "investassist"),
        user=os.environ.get("PGUSER", "james"),
        password=os.environ.get("PGPASSWORD", ""),
    )


def load_universe_filter(conn) -> set:
    """剔除 .BJ、当前 ST、退市变体; 返回可用 ts_code 集合"""
    sql = ("SELECT ts_code, name, list_date FROM stocks "
           "WHERE (ts_code LIKE '%%.SZ' OR ts_code LIKE '%%.SH') AND list_date IS NOT NULL")
    df = pd.read_sql(sql, conn)
    ok = df[(~df["name"].str.contains("ST", na=False)) & (~df["ts_code"].str.startswith("T"))]
    return set(ok["ts_code"]), dict(zip(ok["ts_code"], pd.to_datetime(ok["list_date"])))


def load_factors(conn, start, end) -> dict[str, pd.DataFrame]:
    sql = (f"SELECT factor_name, trade_date, ts_code, value FROM factor_value "
           f"WHERE trade_date >= '{start}' AND trade_date <= '{end}'")
    df = pd.read_sql(sql, conn)
    out = {}
    for name, g in df.groupby("factor_name"):
        out[name] = g.pivot_table(index="trade_date", columns="ts_code", values="value", aggfunc="last").sort_index()
    return out


def load_fwd_returns(conn, start, end, horizon=REBAL) -> pd.DataFrame:
    """T+1 → T+horizon 前向收益 (pct_chg 复权口径)"""
    sql = ("SELECT ts_code, trade_date, pct_chg FROM daily_quote "
           f"WHERE trade_date >= '{start}' AND trade_date <= '{end}' "
           "AND (ts_code LIKE '%.SZ' OR ts_code LIKE '%.SH')")
    df = pd.read_sql(sql, conn)
    df["pct_chg"] = pd.to_numeric(df["pct_chg"], errors="coerce")
    df["ret"] = df["pct_chg"] / 100.0
    wide = df.pivot_table(index="trade_date", columns="ts_code", values="ret", aggfunc="last").sort_index()
    logret = np.log1p(wide.clip(-0.95, 10))
    cum = logret.cumsum()
    # fwd(T) = T+1 收盘买入 → T+1+horizon 收盘卖出
    fwd = np.exp(cum.shift(-(1 + horizon)) - cum.shift(-1)) - 1.0
    return fwd


def load_daily_returns(conn, start, end) -> pd.DataFrame:
    """全区间日收益 (回测净值用)"""
    sql = ("SELECT ts_code, trade_date, pct_chg FROM daily_quote "
           f"WHERE trade_date >= '{start}' AND trade_date <= '{end}' "
           "AND (ts_code LIKE '%.SZ' OR ts_code LIKE '%.SH')")
    df = pd.read_sql(sql, conn)
    df["pct_chg"] = pd.to_numeric(df["pct_chg"], errors="coerce")
    wide = df.pivot_table(index="trade_date", columns="ts_code", values="pct_chg", aggfunc="last").sort_index() / 100.0
    return wide


def rebalance_dates(dates: pd.Index, start, end) -> list:
    """从区间起点每 REBAL 个交易日取一个调仓日"""
    d = pd.DatetimeIndex(dates)
    d = d[(d >= pd.Timestamp(start)) & (d <= pd.Timestamp(end))]
    return list(d[::REBAL])


def cross_rank_ic(factor_row: pd.Series, fwd_row: pd.Series) -> float:
    common = factor_row.dropna().index.intersection(fwd_row.dropna().index)
    if len(common) < 30:
        return np.nan
    f = factor_row[common].astype(float)
    r = fwd_row[common].astype(float)
    if f.nunique() < 5 or r.nunique() < 5:
        return np.nan
    return f.rank().corr(r.rank())


def evaluate_single(name: str, factor: pd.DataFrame, fwd: pd.DataFrame, daily_ret: pd.DataFrame,
                    rebal_dates: list, uni: set, list_dates: dict, hold_stocks=True) -> dict:
    res = {"name": name, "ics": [], "q_navs": None}
    ics = []
    for t in rebal_dates:
        if t not in factor.index or t not in fwd.index:
            continue
        ic = cross_rank_ic(factor.loc[t], fwd.loc[t])
        if not np.isnan(ic):
            ics.append((t, ic))
    res["ics"] = pd.Series(dict(ics))

    # 分层回测
    navs = {q: [1.0] for q in range(1, QUANTILES + 1)}
    nav_dates = []
    for t in rebal_dates:
        if t not in factor.index:
            continue
        row = factor.loc[t].dropna()
        row = row[[c for c in row.index if c in uni and
                   (t - pd.Timedelta(days=0)) > pd.Timestamp(list_dates.get(c, pd.Timestamp("1900-01-01"))) + pd.Timedelta(days=120)]]
        # 上市满 120 自然日
        keep = [c for c in row.index if list_dates.get(c) is not None and
                t >= pd.Timestamp(list_dates[c]) + pd.Timedelta(days=120)]
        row = row[keep]
        if len(row) < 50:
            for q in navs: navs[q].append(navs[q][-1])
            nav_dates.append(t)
            continue
        try:
            qcut = pd.qcut(row.rank(method="first"), QUANTILES, labels=False) + 1
        except ValueError:
            for q in navs: navs[q].append(navs[q][-1])
            nav_dates.append(t)
            continue
        pos_dates = [d for d in daily_ret.index if d > t][:REBAL]
        if not pos_dates:
            break
        for d in pos_dates:
            nav_dates.append(d)
            for q in range(1, QUANTILES + 1):
                stocks = qcut[qcut == q].index.intersection(daily_ret.columns)
                r = daily_ret.loc[d, stocks].dropna() if d in daily_ret.index else pd.Series(dtype=float)
                day_ret = r.mean() if len(r) else 0.0
                navs[q].append(navs[q][-1] * (1 + day_ret - COST * 2 / REBAL))
    qdf = pd.DataFrame({q: navs[q][:len(nav_dates)] for q in navs}, index=nav_dates[:len(nav_dates)])
    qdf = qdf[~qdf.index.duplicated(keep="last")]
    res["q_navs"] = qdf
    return res


def nav_stats(nav: pd.Series) -> dict:
    if len(nav) < 252:
        return {"ann_ret": np.nan, "ann_vol": np.nan, "sharpe": np.nan, "max_dd": np.nan, "total": nav.iloc[-1] - 1 if len(nav) else np.nan}
    ret = nav.pct_change().dropna()
    years = len(ret) / 244
    ann_ret = (nav.iloc[-1] / nav.iloc[0]) ** (1 / years) - 1
    ann_vol = ret.std() * np.sqrt(244)
    sharpe = ann_ret / ann_vol if ann_vol > 0 else np.nan
    dd = (nav / nav.cummax() - 1).min()
    return {"ann_ret": ann_ret, "ann_vol": ann_vol, "sharpe": sharpe, "max_dd": dd, "total": nav.iloc[-1] - 1}


def fmt(v, pct=True):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "-"
    return f"{v*100:.1f}%" if pct else f"{v:.2f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--end", default="2026-09-01")
    ap.add_argument("--combo-only", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    conn = get_conn()
    uni, list_dates = load_universe_filter(conn)
    print(f"股票池: {len(uni)} 只 (沪深非ST)")

    # 数据载入从 start 前 400 天开始 (滚动因子已入库, 这里仅为前向收益完整性)
    load_start = (pd.Timestamp(args.start) - pd.Timedelta(days=40)).strftime("%Y-%m-%d")
    factors = load_factors(conn, args.start, args.end)
    print(f"因子: {list(factors.keys())}")
    fwd = load_fwd_returns(conn, load_start, args.end)
    daily_ret = load_daily_returns(conn, load_start, args.end)
    # 统一 index 为 DatetimeIndex: psycopg2 返回 datetime.date, 与 Timestamp 混用比较会 TypeError / 永不相等
    factors = {k: v.set_axis(pd.to_datetime(v.index)) for k, v in factors.items()}
    fwd = fwd.set_axis(pd.to_datetime(fwd.index))
    daily_ret = daily_ret.set_axis(pd.to_datetime(daily_ret.index))
    rebal = rebalance_dates(daily_ret.index, args.start, args.end)
    print(f"调仓日: {len(rebal)} 期 ({rebal[0].date()} ~ {rebal[-1].date()})")

    lines = []
    lines.append(f"# 因子有效性检验报告 ({args.start} ~ {args.end})\n")
    lines.append(f"- 调仓: 每 {REBAL} 交易日, 等权, 单边成本 {COST*100:.2f}%")
    lines.append(f"- 股票池: 沪深 A 股非 ST, 上市满 120 自然日, 每期截面 ≥50 只\n")

    results = {}
    lines.append("## 1. 单因子 IC/IR 与分层回测\n")
    lines.append("| 因子 | IC均值 | IC标准差 | IR | IC正率 | Q1年化 | Q3年化 | Q5年化 | 多空年化 | Q5夏普 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    ic_series = {}
    for name in sorted(factors.keys()):
        r = evaluate_single(name, factors[name], fwd, daily_ret, rebal, uni, list_dates)
        results[name] = r
        ics = r["ics"]
        ic_series[name] = ics
        q = r["q_navs"]
        if q is None or q.empty:
            continue
        s1, s5 = nav_stats(q[1]), nav_stats(q[QUANTILES])
        ls_nav = q[QUANTILES] / q[1]
        ls = nav_stats(ls_nav)
        ic_mean = ics.mean() if len(ics) else np.nan
        ic_std = ics.std() if len(ics) else np.nan
        ir = ic_mean / ic_std if ic_std and ic_std > 0 else np.nan
        pos_rate = (ics > 0).mean() if len(ics) else np.nan
        lines.append(f"| {name} | {fmt(ic_mean)} | {fmt(ic_std)} | {fmt(ir,False)} | {fmt(pos_rate)} "
                     f"| {fmt(s1['ann_ret'])} | {fmt(nav_stats(q[3])['ann_ret'])} | {fmt(s5['ann_ret'])} "
                     f"| {fmt(ls['ann_ret'])} | {fmt(s5['sharpe'],False)} |")

    # 相关性矩阵 (IC 序列相关)
    lines.append("\n## 2. 因子 IC 相关性矩阵\n")
    icdf = pd.DataFrame(ic_series)
    corr = icdf.corr(method="spearman")
    lines.append("| | " + " | ".join(corr.columns) + " |")
    lines.append("|---" * (len(corr) + 1) + "|")
    for i, rowname in enumerate(corr.index):
        lines.append(f"| {rowname} | " + " | ".join(f"{corr.iloc[i, j]:.2f}" for j in range(len(corr.columns))) + " |")

    # 等权组合
    lines.append("\n## 3. 等权多因子组合 (截面 z-score 合成)\n")
    common_dates = sorted(set.intersection(*[set(f.index) for f in factors.values()]) & set(daily_ret.index))
    combo_rows = {}
    for t in common_dates:
        zs = []
        for name, f in factors.items():
            row = f.loc[t].dropna()
            keep = [c for c in row.index if c in uni and list_dates.get(c) is not None
                    and t >= pd.Timestamp(list_dates[c]) + pd.Timedelta(days=120)]
            row = row[keep]
            if len(row) < 50:
                zs = None; break
            z = (row - row.mean()) / (row.std() if row.std() > 0 else 1.0)
            zs.append(z)
        if zs is None:
            continue
        combo = pd.concat(zs, axis=1).mean(axis=1)
        combo_rows[t] = combo
    combo_factor = pd.DataFrame(combo_rows).T.sort_index()

    r = evaluate_single("combo_eq", combo_factor, fwd, daily_ret, rebal, uni, list_dates)
    results["combo_eq"] = r
    q = r["q_navs"]
    s1, s5 = nav_stats(q[1]), nav_stats(q[QUANTILES])
    ls = nav_stats(q[QUANTILES] / q[1])
    ics = r["ics"]
    lines.append(f"组合 IC均值 {fmt(ics.mean())} | IR {fmt(ics.mean()/ics.std() if len(ics)>1 and ics.std()>0 else np.nan, False)} | "
                 f"IC正率 {fmt((ics>0).mean())}")
    lines.append(f"Q1(最差组) 年化 {fmt(s1['ann_ret'])} | Q5(最好组) 年化 {fmt(s5['ann_ret'])} | 多空年化 {fmt(ls['ann_ret'])}\n")

    # 样本内/外分段
    lines.append("## 4. 样本内 / 样本外分段 (组合 Q5)\n")
    split = "2023-01-01"
    lines.append("| 区间 | 年化 | 波动 | 夏普 | 最大回撤 |")
    lines.append("|---|---|---|---|---|")
    for label, lo, hi in [("样本内 2019-2022", args.start, split), ("样本外 2023-2026", split, args.end)]:
        seg = q[QUANTILES][(q.index >= pd.Timestamp(lo)) & (q.index < pd.Timestamp(hi))]
        if len(seg) > 60:
            st = nav_stats(seg)
            lines.append(f"| {label} | {fmt(st['ann_ret'])} | {fmt(st['ann_vol'])} | {fmt(st['sharpe'],False)} | {fmt(st['max_dd'])} |")

    # 基准对比
    sql = f"SELECT trade_date, pct_chg FROM index_daily WHERE symbol='000300.SH' AND trade_date >= '{load_start}' AND trade_date <= '{args.end}'"
    bench_df = pd.read_sql(sql, conn)
    bench_df["trade_date"] = pd.to_datetime(bench_df["trade_date"])
    bench = bench_df.set_index("trade_date")["pct_chg"].sort_index() / 100.0
    bench_nav = (1 + bench.reindex(q.index).fillna(0)).cumprod()
    b5 = nav_stats(bench_nav)
    lines.append(f"\n基准沪深300 同期: 年化 {fmt(b5['ann_ret'])} | 夏普 {fmt(b5['sharpe'],False)} | 最大回撤 {fmt(b5['max_dd'])}")

    report = "\n".join(lines)
    print("\n" + report)
    out = args.out or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   "reports", f"factor_eval_{pd.Timestamp(args.start).strftime('%Y%m%d')}_{pd.Timestamp(args.end).strftime('%Y%m%d')}.md")
    with open(out, "w") as f:
        f.write(report)
    print(f"\n报告已写入: {out}")
    conn.close()


if __name__ == "__main__":
    main()

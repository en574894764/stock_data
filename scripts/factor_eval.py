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


CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "factor_cache")


def _pivot_unique(df, index, columns, values):
    """pivot 优先 (快); 数据有重复时退回 groupby-last 语义。
    两轴排序: 列序必须确定 (pivot_table 是 sorted), 否则 rank(method='first')
    对并列值的处理依赖列顺序 → 分层结果不可复现"""
    try:
        w = df.pivot(index=index, columns=columns, values=values)
    except ValueError:
        w = df.pivot_table(index=index, columns=columns, values=values, aggfunc="last")
    return w.sort_index().sort_index(axis=1)


def load_factors(conn, start, end) -> dict[str, pd.DataFrame]:
    """因子宽表: parquet 全量缓存 + 增量补齐 (PG 为权威, 缓存可再生, index 统一 datetime64)"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT factor_name FROM factor_value ORDER BY factor_name")
    names = [r[0] for r in cur.fetchall()]
    cur.close()
    lo, hi = pd.Timestamp(start), pd.Timestamp(end)
    out = {}
    for name in names:
        f = os.path.join(CACHE_DIR, f"{name}.parquet")
        if os.path.exists(f):
            wide = pd.read_parquet(f)
            cur = conn.cursor()
            cur.execute("SELECT MAX(trade_date) FROM factor_value WHERE factor_name = %s", (name,))
            pg_max = cur.fetchone()[0]
            cur.close()
            if pg_max is not None and pd.Timestamp(pg_max) > wide.index.max():
                df = pd.read_sql(
                    "SELECT trade_date, ts_code, value FROM factor_value "
                    f"WHERE factor_name = '{name}' AND trade_date > '{wide.index.max().date()}'", conn)
                if len(df):
                    inc = _pivot_unique(df, "trade_date", "ts_code", "value")
                    inc.index = pd.to_datetime(inc.index)
                    wide = pd.concat([wide, inc]).sort_index().sort_index(axis=1)
                    wide.to_parquet(f)
        else:
            df = pd.read_sql(
                f"SELECT trade_date, ts_code, value FROM factor_value WHERE factor_name = '{name}'", conn)
            wide = _pivot_unique(df, "trade_date", "ts_code", "value")
            wide.index = pd.to_datetime(wide.index)
            wide.to_parquet(f)
        w = wide.loc[(wide.index >= lo) & (wide.index <= hi)]
        out[name] = w.sort_index(axis=1)
    return out


def load_daily_returns(conn, start, end) -> pd.DataFrame:
    """全区间日收益 (回测净值用): parquet 缓存, (ts_code, trade_date) 由 PK 保证唯一"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    f = os.path.join(CACHE_DIR, "_daily_ret.parquet")
    lo, hi = pd.Timestamp(start), pd.Timestamp(end)
    if os.path.exists(f):
        wide = pd.read_parquet(f)
        cur = conn.cursor()
        cur.execute("SELECT MAX(trade_date) FROM daily_quote WHERE ts_code LIKE '%%.SZ' OR ts_code LIKE '%%.SH'")
        pg_max = cur.fetchone()[0]
        cur.close()
        if pg_max is not None and pd.Timestamp(pg_max) > wide.index.max():
            df = pd.read_sql(
                "SELECT ts_code, trade_date, pct_chg FROM daily_quote "
                f"WHERE (ts_code LIKE '%.SZ' OR ts_code LIKE '%.SH') AND trade_date > '{wide.index.max().date()}'",
                conn)
            if len(df):
                df["pct_chg"] = pd.to_numeric(df["pct_chg"], errors="coerce")
                inc = _pivot_unique(df, "trade_date", "ts_code", "pct_chg").sort_index() / 100.0
                inc.index = pd.to_datetime(inc.index)
                wide = pd.concat([wide, inc]).sort_index().sort_index(axis=1)
                wide.to_parquet(f)
    else:
        df = pd.read_sql(
            "SELECT ts_code, trade_date, pct_chg FROM daily_quote "
            "WHERE trade_date >= '2015-01-01' AND (ts_code LIKE '%.SZ' OR ts_code LIKE '%.SH')", conn)
        df["pct_chg"] = pd.to_numeric(df["pct_chg"], errors="coerce")
        wide = _pivot_unique(df, "trade_date", "ts_code", "pct_chg") / 100.0
        wide.index = pd.to_datetime(wide.index)
        wide.to_parquet(f)
    return wide.loc[(wide.index >= lo) & (wide.index <= hi)].sort_index(axis=1)


def fwd_from_daily(daily_wide: pd.DataFrame, horizon=REBAL) -> pd.DataFrame:
    """T+1 → T+horizon 前向收益 (pct_chg 复权口径); 从日收益矩阵派生, 不再重复查库"""
    logret = np.log1p(daily_wide.clip(-0.95, 10))
    cum = logret.cumsum()
    return np.exp(cum.shift(-(1 + horizon)) - cum.shift(-1)) - 1.0


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

    # 分层回测 (向量化): 截面 rank+qcut 逐调仓日, 持有期收益用矩阵切片一次算完
    # NOTE 2026-09-04: 本向量化版顺带修复了旧逐日版的 off-by-one bug ——
    #   旧版 navs 列表带初始 [1.0] 与 nav_dates 错位 1, DataFrame 构造时初始值占住首日,
    #   整条净值序列滞后一天且最后一个交易日收益被丢弃 (7.7 年年化偏差 ~0.1-0.4pp)。
    #   本版 blocks 不含初始值, 收益序列与日期一一对应。IC 类指标不经 navs, 不受影响。
    dr_idx, dr_cols = daily_ret.index, daily_ret.columns
    blocks = {q: [] for q in range(1, QUANTILES + 1)}   # 每组每期的日收益数组
    nav_dates = []
    for t in rebal_dates:
        if t not in factor.index:
            continue
        row = factor.loc[t].dropna()
        keep = [c for c in row.index if c in uni and list_dates.get(c) is not None
                and t > pd.Timestamp(list_dates[c]) + pd.Timedelta(days=120)]
        row = row[keep]
        qcut = None
        if len(row) >= 50:
            try:
                qcut = pd.qcut(row.rank(method="first"), QUANTILES, labels=False) + 1
            except ValueError:
                qcut = None
        pos_dates = [d for d in dr_idx if d > t][:REBAL]
        if not pos_dates:
            break
        if qcut is None:
            # 截面不足/qcut 失败: 该期净值持平 (复刻原逐日版行为)
            nav_dates.append(t)
            for q in blocks:
                blocks[q].append(np.zeros(1))
            continue
        nav_dates.extend(pos_dates)
        row_pos = dr_idx.get_indexer(pos_dates)
        stocks = qcut.index.intersection(dr_cols)
        col_pos = dr_cols.get_indexer(stocks)
        m = daily_ret.iloc[row_pos, col_pos].to_numpy(dtype=np.float64)   # 持有期 × 入选股
        q_labels = qcut.loc[stocks].to_numpy()
        for q in range(1, QUANTILES + 1):
            sub = m[:, q_labels == q]
            if sub.shape[1]:
                with np.errstate(invalid="ignore"):
                    dr = np.nanmean(sub, axis=1)
                dr = np.where(np.isfinite(dr), dr, 0.0)
            else:
                dr = np.zeros(len(pos_dates))
            blocks[q].append(dr - COST * 2 / REBAL)

    if not nav_dates:
        return res
    r_idx = pd.DatetimeIndex(nav_dates)
    qdf = pd.DataFrame({q: np.concatenate(blocks[q]) for q in range(1, QUANTILES + 1)}, index=r_idx)
    qdf = (1.0 + qdf).cumprod()
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
    # parquet 缓存层已统一 index 为 datetime64; fwd 从日收益矩阵派生 (不再重复查库)
    daily_ret = load_daily_returns(conn, load_start, args.end)
    fwd = fwd_from_daily(daily_ret)
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
    lines.append("\n## 3. 多因子组合 (截面 z-score 合成): 等权 vs 滚动IC加权 vs 正交化等权\n")
    lines.append("- IC 加权: 每期用截至上一调仓日的 12 期 IC 均值作权重 (无前视), IC≤0 的因子权重归零")
    lines.append("- 正交化: 按 12 期滚动 IC 降序, 逐因子对已入选因子截面回归取残差 (消除冗余信息), 残差再标准化后等权\n")
    icdf = pd.DataFrame(ic_series)  # index=调仓日, col=因子
    combo_qs = {}
    for mode in ["equal", "ic_weight", "orth"]:
        combo_rows = {}
        for t in rebal:
            if any(t not in f.index for f in factors.values()):
                continue
            if mode == "ic_weight":
                past = icdf[icdf.index < t].tail(12)
                w = past.mean().clip(lower=0)
                if w.sum() <= 0:
                    continue
                w = w / w.sum()
            elif mode == "orth":
                past = icdf[icdf.index < t].tail(12)
                icm = past.mean()
                order = [n for n in icm.sort_values(ascending=False).index if n in factors and icm[n] > 0]
            else:
                w = pd.Series(1.0, index=list(factors.keys()))

            if mode == "orth":
                # 顺序正交化: IC 强者先占位, 后来者只保留残差信息, 残差再标准化
                zs, done = [], []
                for name in order:
                    row = factors[name].loc[t].dropna()
                    keep = [c for c in row.index if c in uni and list_dates.get(c) is not None
                            and t >= pd.Timestamp(list_dates[c]) + pd.Timedelta(days=120)]
                    row = row[keep]
                    if len(row) < 50:
                        continue
                    z = (row - row.mean()) / (row.std() if row.std() > 0 else 1.0)
                    for prev in done:
                        common = z.index.intersection(prev.dropna().index)
                        if len(common) < 50:
                            continue
                        beta = (z[common] * prev[common]).sum() / (prev[common] ** 2).sum()
                        z = z - beta * prev
                    z = (z - z.mean()) / (z.std() if z.std() > 0 else 1.0)
                    zs.append(z)
                    done.append(z)
                if len(zs) < 2:
                    continue
                combo_rows[t] = pd.concat(zs, axis=1).sum(axis=1, min_count=1)
            else:
                zs = []
                for name, f in factors.items():
                    if w.get(name, 0) <= 0:
                        continue
                    row = f.loc[t].dropna()
                    keep = [c for c in row.index if c in uni and list_dates.get(c) is not None
                            and t >= pd.Timestamp(list_dates[c]) + pd.Timedelta(days=120)]
                    row = row[keep]
                    if len(row) < 50:
                        zs = None; break
                    z = (row - row.mean()) / (row.std() if row.std() > 0 else 1.0)
                    zs.append(z * w[name])
                if zs is None:
                    continue
                combo_rows[t] = pd.concat(zs, axis=1).sum(axis=1)
        combo_factor = pd.DataFrame(combo_rows).T.sort_index()

        r = evaluate_single(f"combo_{mode}", combo_factor, fwd, daily_ret, rebal, uni, list_dates)
        results[f"combo_{mode}"] = r
        q = r["q_navs"]
        combo_qs[mode] = q
        if q is None or q.empty:
            continue
        s1, s5 = nav_stats(q[1]), nav_stats(q[QUANTILES])
        ls = nav_stats(q[QUANTILES] / q[1])
        ics = r["ics"]
        label = {"equal": "等权", "ic_weight": "IC加权", "orth": "正交化等权"}[mode]
        lines.append(f"**{label}**: IC均值 {fmt(ics.mean())} | IR {fmt(ics.mean()/ics.std() if len(ics)>1 and ics.std()>0 else np.nan, False)} | "
                     f"IC正率 {fmt((ics>0).mean())}")
        lines.append(f"Q1(最差组) 年化 {fmt(s1['ann_ret'])} | Q5(最好组) 年化 {fmt(s5['ann_ret'])} | 多空年化 {fmt(ls['ann_ret'])}\n")

    # IC 加权最新权重快照
    w_now = icdf.tail(12).mean().clip(lower=0)
    w_now = (w_now / w_now.sum()).sort_values(ascending=False) if w_now.sum() > 0 else w_now
    lines.append("IC 加权最新权重: " + ", ".join(f"{k} {v*100:.0f}%" for k, v in w_now.items() if v > 0) + "\n")

    # 样本内/外分段 (正交化组合 Q5; 若无则用等权)
    seg_q = combo_qs.get("orth") if combo_qs.get("orth") is not None else combo_qs.get("equal")
    lines.append("\n## 4. 样本内 / 样本外分段 (正交化组合 Q5)\n")
    split = "2023-01-01"
    lines.append("| 区间 | 年化 | 波动 | 夏普 | 最大回撤 |")
    lines.append("|---|---|---|---|---|")
    for label, lo, hi in [("样本内 2019-2022", args.start, split), ("样本外 2023-2026", split, args.end)]:
        seg = seg_q[QUANTILES][(seg_q.index >= pd.Timestamp(lo)) & (seg_q.index < pd.Timestamp(hi))]
        if len(seg) > 60:
            st = nav_stats(seg)
            lines.append(f"| {label} | {fmt(st['ann_ret'])} | {fmt(st['ann_vol'])} | {fmt(st['sharpe'],False)} | {fmt(st['max_dd'])} |")

    # 基准对比
    sql = f"SELECT trade_date, pct_chg FROM index_daily WHERE symbol='000300.SH' AND trade_date >= '{load_start}' AND trade_date <= '{args.end}'"
    bench_df = pd.read_sql(sql, conn)
    bench_df["trade_date"] = pd.to_datetime(bench_df["trade_date"])
    bench = bench_df.set_index("trade_date")["pct_chg"].sort_index() / 100.0
    bench_nav = (1 + bench.reindex(seg_q.index).fillna(0)).cumprod()
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

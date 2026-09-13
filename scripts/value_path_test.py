#!/usr/bin/env python3
"""价值投资路径实证 (A股): 选股 / 择时 / 长期持有

A.  双门限 Top50 组合 (价值前40% ∩ ROE前40%, 按 value+roe z 合成取 Top50)
    vs 纯价值 Top50 vs 纯质量 Top50 (月度调仓, 含成本)
B1. 个股 PB 自身 3 年滚动分位 5 组 → 未来 1 年收益 (什么时候买这只股票)
B2. 全市场 PB 中位数 5 年滚动分位 → 沪深300 未来 1 年收益 (什么时候入市)
B3. 高 ROE 组内 PB 低分位 vs 高分位 → 未来 1 年收益 (好公司也要好价格吗)
C1. 双门限组合 年度调仓 vs 月度调仓 (长期持有 vs 频繁再平衡)
C2. 2016 买入 ROE Top50 持有 10 年: 年化收益 vs 平均 ROE (收益率≈ROE 假说)

输出: 终端表格 + outputs/value_path_test.json
"""
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.factor_eval import (
    get_conn, load_universe_filter, load_daily_returns, rebalance_dates, nav_stats,
)
from scripts.value_factor_eval import build_value_factors, zscore

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
START, END = "2016-01-01", "2026-09-01"
COST = 0.0015


def load_factor_parquet(name):
    w = pd.read_parquet(os.path.join(ROOT, "factor_cache", f"{name}.parquet"))
    w.index = pd.to_datetime(w.index)
    return w.sort_index(axis=1)


def clean_row(row, t, uni, list_dates):
    keep = [c for c in row.index if c in uni and list_dates.get(c) is not None
            and t > pd.Timestamp(list_dates[c]) + pd.Timedelta(days=120)]
    return row[keep]


def build_scores(factors, rebal, uni, list_dates):
    """每期各分数序列: value_z / roe_z / 双门限合成分"""
    value_rows, roe_rows, combo_rows = {}, {}, {}
    for t in rebal:
        if t not in factors["bp"].index or t not in factors["roe_lf"].index:
            continue
        vz, rz = None, None
        zs = []
        for name in ["ep_ttm", "bp", "dp", "sp"]:
            if t not in factors[name].index:
                zs = None
                break
            r = clean_row(factors[name].loc[t], t, uni, list_dates)
            if len(r) < 50:
                zs = None
                break
            zs.append(zscore(r))
        if zs:
            vz = pd.concat(zs, axis=1).sum(axis=1)
        rr = clean_row(factors["roe_lf"].loc[t], t, uni, list_dates)
        if len(rr) >= 50:
            rz = zscore(rr)
        if vz is not None and rz is not None:
            common = vz.index.intersection(rz.index)
            vz_c, rz_c = vz[common], rz[common]
            # 双门限: 价值前 40% ∩ 质量前 40%, 门内按 value_z+roe_z 排序
            v_ok = vz_c >= vz_c.quantile(0.60)
            r_ok = rz_c >= rz_c.quantile(0.60)
            both = (v_ok & r_ok)
            combo = (vz_c + rz_c).where(both)
            value_rows[t] = vz_c
            roe_rows[t] = rz_c
            combo_rows[t] = combo
    return value_rows, roe_rows, combo_rows


def select_backtest(score_rows, daily_ret, rebal, topn=50, hold=20):
    """Top-N 等权组合回测; 返回 (nav Series, picks_log)"""
    dr_idx, dr_cols = daily_ret.index, daily_ret.columns
    rets, dates, picks_log = [], [], {}
    for t in rebal:
        if t not in score_rows:
            continue
        row = score_rows[t].dropna()
        if len(row) < topn:
            continue
        picks = row.sort_values(ascending=False).head(topn).index.intersection(dr_cols)
        picks_log[t] = list(picks)
        pos_dates = [d for d in dr_idx if d > t][:hold]
        if not pos_dates:
            break
        m = daily_ret.loc[pos_dates, picks]
        with np.errstate(invalid="ignore"):
            dr = np.nanmean(m.to_numpy(dtype=np.float64), axis=1)
        # 成本按调仓一次折算, 每日分摊 (与引擎口径一致)
        dr = np.where(np.isfinite(dr), dr, 0.0) - COST * 2 / len(pos_dates)
        rets.append(dr)
        dates.extend(pos_dates)
    if not dates:
        return None, picks_log
    nav = pd.Series(np.concatenate(rets), index=pd.DatetimeIndex(dates))
    nav = (1.0 + nav.groupby(level=0).mean()).cumprod()
    return nav, picks_log


def timing_nav(nav, exposure):
    """按估值分位日度仓位缩放组合收益 (现金零收益), nav→日收益→仓位加权"""
    ret = nav.pct_change().fillna(0.0)
    pos = pd.Series(np.nan, index=ret.index)
    for t, w in exposure.items():
        pos.loc[pos.index >= pd.Timestamp(t), :] = w if isinstance(w, object) else w
    pos = pos.ffill().fillna(1.0)
    timed = (1.0 + ret * pos).cumprod()
    return timed


def main():
    conn = get_conn()
    uni, list_dates = load_universe_filter(conn)
    print(f"股票池: {len(uni)} 只 (沪深非ST)")

    factors = {"ep_ttm": load_factor_parquet("ep_ttm"),
               "roe_lf": load_factor_parquet("roe_lf")}
    factors.update(build_value_factors(conn, START))
    print("因子载入完成")

    load_start = (pd.Timestamp(START) - pd.Timedelta(days=40)).strftime("%Y-%m-%d")
    daily_ret = load_daily_returns(conn, load_start, END)
    rebal_m = rebalance_dates(daily_ret.index, START, END, )
    rebal_m = rebalance_dates(daily_ret.index, START, END)  # 月度 (引擎默认 20)
    from scripts.factor_eval import REBAL as _R
    rebal_y = rebalance_dates(daily_ret.index, START, END, interval=250) if False else None
    # rebalance_dates 不支持 interval 参数, 手动做年度
    d = pd.DatetimeIndex(daily_ret.index)
    d = d[(d >= pd.Timestamp(START)) & (d <= pd.Timestamp(END))]
    rebal_y = list(d[::250])
    print(f"调仓日: 月度 {len(rebal_m)} 期 / 年度 {len(rebal_y)} 期")

    vr, rr, cr = build_scores(factors, rebal_m, uni, list_dates)
    print(f"评分期数: value {len(vr)}, roe {len(rr)}, combo {len(cr)}")

    results = {}
    print("\n=== A. Top50 组合 (月度调仓, 单边成本 0.15%) ===")
    print(f"{'组合':<22}{'年化':>8}{'夏普':>7}{'回撤':>9}{'累计':>9}")
    for name, rows, hold in [
            ("纯价值 Top50", vr, 20), ("纯质量(ROE) Top50", rr, 20), ("双门限 Top50", cr, 20)]:
        nav, _ = select_backtest(rows, daily_ret, rebal_m, topn=50, hold=hold)
        if nav is None:
            continue
        st = nav_stats(nav)
        results[name] = {k: float(v) for k, v in st.items()}
        print(f"{name:<22}{st['ann_ret']*100:>7.1f}%{st['sharpe']:>7.2f}{st['max_dd']*100:>8.1f}%{st['total']*100:>8.0f}%")

    # 双门限年度调仓 (C1)
    vr_y, rr_y, cr_y = build_scores(factors, rebal_y, uni, list_dates)
    print("\n=== C1. 双门限 Top50: 月度 vs 年度调仓 ===")
    for label, rows, hold in [("月度(20日)", cr, 20), ("年度(250日)", cr_y, 250)]:
        nav, _ = select_backtest(rows, daily_ret, rebal_y if hold == 250 else rebal_m,
                                 topn=50, hold=hold)
        if nav is None:
            continue
        st = nav_stats(nav)
        results[f"双门限_{label}"] = {k: float(v) for k, v in st.items()}
        print(f"  {label:<12} 年化 {st['ann_ret']*100:.1f}% | 夏普 {st['sharpe']:.2f} | 回撤 {st['max_dd']*100:.1f}%")

    # ---- B1/B3: 个股 PB 自身 3 年滚动分位 ----
    print("\n=== B1. 个股 PB 自身 3年滚动分位 → 未来 1 年收益 (5 组) ===")
    bp = factors["bp"]
    idx = bp.index
    sample_ts = [t for t in list(idx[::63]) if t >= pd.Timestamp("2019-01-01")]
    fwd1y = fwd_from_daily(daily_ret, 252)
    g_rets = {g: [] for g in range(1, 6)}
    hi_roe_lowpb, hi_roe_highpb, hi_roe_mid = [], [], []
    for t in sample_ts:
        if t not in fwd1y.index:
            continue
        hist = bp.loc[:t].tail(750)
        cur = bp.loc[t]
        valid = hist.notna().sum()
        pct = (hist < cur).sum() / valid.replace(0, np.nan)  # bp 分位: 值高=便宜
        pct = pct.dropna()
        pct = pct[pct.index.intersection(fwd1y.columns).intersection(uni)]
        if len(pct) < 200:
            continue
        fwd = fwd1y.loc[t]
        common = pct.index.intersection(fwd.dropna().index)
        pv, fv = pct[common], fwd[common]
        try:
            q = pd.qcut(pv.rank(method="first"), 5, labels=False) + 1
        except ValueError:
            continue
        for g in range(1, 6):
            g_rets[g].append(float(fv[q == g].mean()))
        # B3: 高 ROE 组内的 PB 分位效应
        if t in factors["roe_lf"].index:
            roe_row = clean_row(factors["roe_lf"].loc[t], t, uni, list_dates).dropna()
            hi = roe_row[roe_row >= roe_row.quantile(0.70)].index
            sub = pv[hi.intersection(pv.index)]
            sub_f = fv[sub.index]
            if len(sub) > 50:
                lo = sub <= sub.quantile(0.30)
                hiq = sub >= sub.quantile(0.70)
                hi_roe_lowpb.append(float(sub_f[lo].mean()))
                hi_roe_highpb.append(float(sub_f[hiq].mean()))
    b1 = {f"Q{g}": float(np.mean(v)) for g, v in g_rets.items() if v}
    results["B1_pb_pct_quintiles_fwd1y"] = b1
    for g in range(1, 6):
        tag = "对自身最贵" if g == 1 else ("对自身最便宜" if g == 5 else "")
        print(f"  BP分位Q{g} ({tag}): 未来1年平均收益 {b1.get(f'Q{g}', float('nan'))*100:.1f}%")
    b3 = {"highROE_lowPB": float(np.mean(hi_roe_lowpb)),
          "highROE_highPB": float(np.mean(hi_roe_highpb))}
    results["B3_highROE_pb_timing"] = b3
    print(f"  B3 高ROE股内: PB低分位(便宜时买) 未来1年 {b3['highROE_lowPB']*100:.1f}% "
          f"vs PB高分位(贵时买) {b3['highROE_highPB']*100:.1f}%")

    # ---- B2: 全市场 PB 中位数 5 年滚动分位 → 沪深300 未来 1 年 ----
    print("\n=== B2. 全市场 PB 中位数 5年滚动分位 → 未来 1 年指数收益 ===")
    mkt = pd.read_sql(
        "SELECT trade_date, percentile_cont(0.5) WITHIN GROUP (ORDER BY pb) AS med_pb "
        "FROM daily_basic WHERE pb > 0 GROUP BY trade_date ORDER BY trade_date", conn)
    mkt["trade_date"] = pd.to_datetime(mkt["trade_date"])
    mkt = mkt.set_index("trade_date")["med_pb"]
    vals = mkt.to_numpy()
    idxs = mkt.index
    pct5 = {}
    for i in range(len(vals)):
        if i < 1250 or np.isnan(vals[i]):
            continue
        pct5[idxs[i]] = float((vals[i - 1250:i] < vals[i]).mean())
    pct_s = pd.Series(pct5).sort_index()

    bench = pd.read_sql(
        f"SELECT trade_date, pct_chg FROM index_daily WHERE symbol='000300.SH' "
        f"AND trade_date >= '{load_start}' AND trade_date <= '{END}'", conn)
    bench["trade_date"] = pd.to_datetime(bench["trade_date"])
    bench_ret = bench.set_index("trade_date")["pct_chg"].sort_index() / 100.0
    bench_fwd1y = pd.Series(
        (bench_ret.rolling(252).apply(lambda x: float((1 + x).prod()), raw=False) - 1).shift(-252),
        index=bench_ret.index)

    buckets = {"低分位(<30%)": [], "中分位(30-70%)": [], "高分位(>70%)": []}
    for t in sample_ts:
        near = pct_s.index[pct_s.index <= t]
        if not len(near):
            continue
        p = pct_s.loc[near[-1]]
        # 指数未来 1 年收益, 从 t+1 起算 (跳过 NaN: 序列首尾无满窗)
        fut = bench_fwd1y.dropna()
        fut = fut.loc[fut.index > t]
        if not len(fut):
            continue
        r = float(fut.iloc[0])
        if p < 0.30:
            buckets["低分位(<30%)"].append(r)
        elif p <= 0.70:
            buckets["中分位(30-70%)"].append(r)
        else:
            buckets["高分位(>70%)"].append(r)
    b2 = {}
    for k, v in buckets.items():
        if v:
            b2[k] = {"n": len(v), "fwd1y_mean": float(np.mean(v)), "win_rate": float(np.mean([x > 0 for x in v]))}
            print(f"  {k}: 样本 {len(v)} 期 | 未来1年平均 {np.mean(v)*100:.1f}% | 胜率 {np.mean([x>0 for x in v])*100:.0f}%")
    results["B2_market_pb_pct_vs_hs300_fwd1y"] = b2

    # 择时组合: 双门限 Top50 × 市场估值分位仓位
    nav_combo, _ = select_backtest(cr, daily_ret, rebal_m, topn=50, hold=20)
    if nav_combo is not None:
        ret_c = nav_combo.pct_change().fillna(0.0)
        exposure = []
        for d_ in ret_c.index:
            near = pct_s.index[pct_s.index <= d_]
            p = pct_s.loc[near[-1]] if len(near) else 0.5
            exposure.append(1.0 if p < 0.30 else (0.6 if p <= 0.70 else 0.3))
        expo_s = pd.Series(exposure, index=ret_c.index)
        timed = (1.0 + ret_c * expo_s).cumprod()
        st_t, st_f = nav_stats(timed), nav_stats(nav_combo)
        results["择时_市场PB分位仓位"] = {k: float(v) for k, v in st_t.items()}
        print(f"\n  择时版(低分位满仓/中60%/高30%): 年化 {st_t['ann_ret']*100:.1f}% 夏普 {st_t['sharpe']:.2f} "
              f"回撤 {st_t['max_dd']*100:.1f}% | 满仓版: 年化 {st_f['ann_ret']*100:.1f}% 回撤 {st_f['max_dd']*100:.1f}%")

    # ---- C2: 2016 买入 ROE Top50 持有 10 年 ----
    print("\n=== C2. 2016-09 买入 ROE Top50 持有至 2026-09 (不调仓) ===")
    first_y = rebal_y[0] if rebal_y else None
    if first_y is not None:
        row = clean_row(factors["roe_lf"].loc[first_y], first_y, uni, list_dates).dropna() \
            if first_y in factors["roe_lf"].index else None
        if row is not None and len(row) >= 50:
            picks = row.sort_values(ascending=False).head(50).index.intersection(daily_ret.columns)
            m = daily_ret.loc[daily_ret.index > first_y, picks]
            with np.errstate(invalid="ignore"):
                dr = np.nanmean(m.to_numpy(dtype=np.float64), axis=1)
            dr = np.where(np.isfinite(dr), dr, 0.0)
            nav_h = pd.Series((1 + pd.Series(dr, index=m.index)).cumprod(), index=m.index)
            st = nav_stats(nav_h)
            # 这 50 只股票 2016-2026 平均 roe_lf (roe_lf 为单季未年化口径, ×4 近似年化)
            roe_w = factors["roe_lf"].reindex(columns=picks).loc[first_y:]
            roe_yearly = roe_w.groupby(roe_w.index.year).median().median(axis=1)
            avg_roe = float(roe_yearly.mean()) * 4
            buy_roe_med = float(factors["roe_lf"].loc[first_y, picks].median()) * 4
            print(f"  年化 {st['ann_ret']*100:.1f}% | 累计 {st['total']*100:.0f}% | 回撤 {st['max_dd']*100:.1f}% "
                  f"| 期间平均年化ROE(中位近似) {avg_roe:.1f}%")
            results["C2_hold10y_roe_top50"] = {
                "ann_ret": float(st["ann_ret"]), "sharpe": float(st["sharpe"]),
                "max_dd": float(st["max_dd"]), "total": float(st["total"]),
                "avg_roe_annualized": avg_roe, "buy_roe_annualized": buy_roe_med,
                "n": int(len(picks)), "first_date": str(first_y.date()),
            }

    # 基准
    bench_nav = (1 + bench_ret.reindex(nav_combo.index if nav_combo is not None else bench_ret.index)
                 .fillna(0)).cumprod()
    b5 = nav_stats(bench_nav)
    results["bench_hs300"] = {k: float(v) for k, v in b5.items()}
    print(f"\n基准沪深300 ({START}~{END}): 年化 {b5['ann_ret']*100:.1f}% | 夏普 {b5['sharpe']:.2f} | 回撤 {b5['max_dd']*100:.1f}%")

    os.makedirs(os.path.join(ROOT, "outputs"), exist_ok=True)
    out = os.path.join(ROOT, "outputs", "value_path_test.json")
    with open(out, "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    print(f"\nJSON: {out}")
    conn.close()


def fwd_from_daily(daily_wide, horizon):
    from scripts.factor_eval import fwd_from_daily as _f
    return _f(daily_wide, horizon)


if __name__ == "__main__":
    main()

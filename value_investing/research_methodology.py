#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
方法论调研脚本：用真实数据回答三个问题
─────────────────────────────────────────────────────────
  Q1 利润/资产负债率「成色」：扣非占比、ROE/负债率分布、极端值
  Q2 三年外推是否 solid：历史回测不同增速口径对「未来 3 年利润」的预测误差
  Q3 趋势判断是否置信：趋势打分对未来 20 日收益的截面预测力（IC + 分桶胜率）

输出：控制台打印关键数字，供撰写 methodology 报告用。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import valuelib as vl

pd.set_option("display.width", 220)
pd.set_option("display.unicode.east_asian_width", True)


def load_screened(cfg=None, base_year=2025):
    """合格 list 代码"""
    cfg = cfg or vl.load_config()
    raw = vl.fetch_screen_universe(cfg, base_year)
    res = vl.apply_screen(raw, cfg, base_year)
    return res


# ── Q1 成色 ──────────────────────────────────────────────────────────────
def q1_quality(conn):
    cfg = vl.load_config()
    res = load_screened(cfg, 2025)
    codes = res["ts_code"].tolist()

    # 归母净利(income) + 扣非净利/ROE/负债率(financial_indicator)
    inc = vl.query(
        "SELECT ts_code, n_income_attr_p, n_income FROM income "
        "WHERE report_type='4' AND report_year=2025 AND ts_code=ANY(%s)",
        (codes,), conn=conn)
    fi = vl.query(
        "SELECT ts_code, roe, roe_dt, debt_to_assets, profit_dedt, netprofit_yoy "
        "FROM financial_indicator WHERE report_type='4' AND report_year=2025 AND ts_code=ANY(%s)",
        (codes,), conn=conn)

    d = inc.merge(fi, on="ts_code", how="outer")
    d["扣非占比"] = d["profit_dedt"] / d["n_income_attr_p"]

    print("=" * 70)
    print("Q1 成色（合格 list，FY2025 年报，n=%d）" % len(d))
    print("=" * 70)
    print("ROE 分布:  ", d["roe"].describe(percentiles=[.05, .25, .5, .75, .95]).round(1).to_dict())
    print("负债率分布:", d["debt_to_assets"].describe(percentiles=[.05, .25, .5, .75, .95]).round(1).to_dict())
    print("扣非占比分布:", d["扣非占比"].describe(percentiles=[.05, .25, .5, .75, .95]).round(2).to_dict())
    print()
    print("扣非占比 < 70%% 的只数：%d（利润含一次性/非经常收益嫌疑）" % (d["扣非占比"] < 0.7).sum())
    print("扣非占比 < 100%% 的只数：%d（扣非低于归母，正常）" % (d["扣非占比"] < 1.0).sum())
    print("扣非为负 的只数：%d" % (d["profit_dedt"] <= 0).sum())
    print("ROE > 30%% 的只数：%d（高 ROE 可持续性存疑）" % (d["roe"] > 30).sum())
    print("ROE > 50%% 的只数：%d（极端）" % (d["roe"] > 50).sum())
    print("负债率 40~50%% 的只数：%d（贴近阈值，边界脆弱）" % ((d["debt_to_assets"] > 40) & (d["debt_to_assets"] <= 50)).sum())
    print("净利同比为负 的只数：%d" % (d["netprofit_yoy"] < 0).sum())
    # 扣非占比最差 10 名
    print("\n扣非占比最低 10 名（利润成色最差）:")
    top = d.nsmallest(10, "扣非占比")[["ts_code", "roe", "debt_to_assets", "扣非占比"]]
    print(top.to_string(index=False))
    return d


# ── Q2 外推验证 ──────────────────────────────────────────────────────────
def q2_projection(conn):
    cfg = vl.load_config()
    res = load_screened(cfg, 2025)
    codes = res["ts_code"].tolist()

    # 拉 2009~2025 年报归母净利
    hist = vl.query(
        "SELECT ts_code, report_year, n_income_attr_p FROM income "
        "WHERE report_type='4' AND report_year BETWEEN 2009 AND 2025 AND ts_code=ANY(%s)",
        (codes,), conn=conn)

    prof = {ts: dict(zip(g["report_year"].astype(int), g["n_income_attr_p"]))
            for ts, g in hist.groupby("ts_code")}

    def cagr(vals):
        v = [float(x) for x in vals]
        return (v[-1] / v[0]) ** (1.0 / (len(v) - 1)) - 1

    clamp = lambda g: min(max(g, -0.10), 0.30)

    rows = []
    for base in range(2013, 2022):  # 基年 2013..2021，实际看 base+3 ≤ 2024
        for ts, s in prof.items():
            nb = s.get(base)
            na = s.get(base + 3)
            if nb is None or na is None or nb <= 0 or na <= 0:
                continue
            pts5 = [s.get(base - k) for k in range(4, -1, -1)]
            pts3 = [s.get(base - k) for k in range(2, -1, -1)]
            actual3 = (na / nb) ** (1 / 3) - 1  # 实际年化 3 年增速

            g5 = cagr(pts5) if all(p is not None and p > 0 for p in pts5) else None
            g3 = cagr(pts3) if all(p is not None and p > 0 for p in pts3) else None

            row = dict(base=base, ts_code=ts, actual3=actual3)
            for name, g in [("g5", g5), ("g3", g3)]:
                if g is not None:
                    gc = clamp(g)
                    row[name] = gc
            rows.append(row)

    df = pd.DataFrame(rows)
    print("=" * 70)
    print("Q2 三年外推验证（合格 list，基年 2013..2021，样本 %d 条）" % len(df))
    print("=" * 70)

    # 预测 vs 实际：pred3 年利润比 = (1+g)^3 / (1+actual3)^3 - 1
    def err_of(gcol):
        e = (1 + df[gcol]) ** 3 / (1 + df["actual3"]) ** 3 - 1
        return e

    # 实际年化增速分布
    print("实际 3 年增速分布:", df["actual3"].describe(percentiles=[.05, .25, .5, .75, .95]).round(2).to_dict())
    print()

    methods = {
        "CAGR5(截断)": lambda: err_of("g5"),
        "CAGR3(截断)": lambda: err_of("g3"),
        "默认+5%": lambda: (1.05 ** 3) / (1 + df["actual3"]) ** 3 - 1,
        "零增长": lambda: 1 / (1 + df["actual3"]) ** 3 - 1,
    }
    for name, f in methods.items():
        e = f()
        e = e.replace([np.inf, -np.inf], np.nan).dropna()
        mape = (e.abs()).median()
        within30 = (e.abs() < 0.30).mean()
        within50 = (e.abs() < 0.50).mean()
        # 方向命中：预测增速符号 vs 实际符号
        if name.startswith("CAGR"):
            col = "g5" if "5" in name else "g3"
            sign_ok = (np.sign(df[col].fillna(0)) == np.sign(df["actual3"])).mean()
        else:
            sign_ok = np.nan
        print(f"{name:<12} 样本{len(e):>5}  中位|误差| {mape:>6.1%}  ≤30%占比 {within30:>5.1%}  ≤50%占比 {within50:>5.1%}  方向命中 {sign_ok:>5.1%}")

    # 分层：实际增速落在极端(>30% 或 <0)时，各口径表现
    print()
    for label, mask in [("实际增速>30%(高成长)", df["actual3"] > 0.30),
                        ("实际增速<0(衰退)", df["actual3"] < 0),
                        ("实际增速0~15%(常态)", (df["actual3"] >= 0) & (df["actual3"] <= 0.15))]:
        sub = df[mask]
        if sub.empty:
            continue
        e5 = err_of("g5").loc[sub.index].abs().median()
        e0 = (1 / (1 + sub["actual3"]) ** 3 - 1).abs().median()
        print(f"  {label:<20} n={len(sub):>5}  CAGR5中位误差 {e5:>6.1%}  vs 零增长中位误差 {e0:>6.1%}")
    return df


# ── Q3 趋势验证 ──────────────────────────────────────────────────────────
def q3_trend(conn):
    cfg = vl.load_config()
    res = load_screened(cfg, 2025)
    codes = res["ts_code"].tolist()

    px = vl.query(
        "SELECT ts_code, trade_date, close FROM daily_quote "
        "WHERE trade_date >= '2022-06-01' AND ts_code=ANY(%s) ORDER BY ts_code, trade_date",
        (codes,), conn=conn)
    px["trade_date"] = pd.to_datetime(px["trade_date"])
    wide = px.pivot(index="trade_date", columns="ts_code", values="close").sort_index()

    close = wide
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    ret20 = close / close.shift(20) - 1
    ma20_slope = ma20 / ma20.shift(5) - 1

    c1 = (close > ma20).astype(int)
    c2 = (ma20 > ma60).astype(int)
    c3 = (ret20 > 0).astype(int)
    c4 = (ma20_slope > 0).astype(int)
    score = c1 + c2 + c3 + c4

    fwd20 = close.shift(-20) / close - 1

    # 快照：每 20 个交易日取一次
    n = len(close)
    snapshots = list(range(80, n - 20, 20))

    buckets = {i: [] for i in range(5)}
    ics = []
    for i in snapshots:
        s = score.iloc[i]
        f = fwd20.iloc[i]
        m = s.notna() & f.notna()
        if m.sum() < 50:
            continue
        si, fi = s[m], f[m]
        for k in range(5):
            buckets[k].extend(fi[si == k].tolist())
        # 截面 IC（Pearson 打分 vs 未来收益）
        ics.append(np.corrcoef(si.astype(float), fi)[0, 1])

    print("=" * 70)
    print("Q3 趋势打分 → 未来 20 日收益（合格 list，快照 %d 个，样本池）" % len(snapshots))
    print("=" * 70)
    print(f"截面 IC 均值 {np.nanmean(ics):+.3f} / 中位 {np.nanmedian(ics):+.3f}"
          f" / 正值占比 {np.mean([x > 0 for x in ics if not np.isnan(x)]):.0%}")
    print()
    print("分桶（0=下降强 .. 4=上升强）:")
    print(f"{'分':>2} {'样本数':>8} {'平均20日收益':>12} {'中位':>8} {'胜率':>8}")
    for k in range(5):
        v = [x for x in buckets[k] if not np.isnan(x)]
        if not v:
            continue
        arr = np.array(v)
        print(f"{k:>2} {len(v):>8} {arr.mean():>+11.2%} {np.median(arr):>+7.2%} {(arr>0).mean():>7.1%}")

    # 条件独立性检查：四条件两两相关（说明打分并非 4 个独立证据）
    flat = pd.DataFrame({
        "c1_价>MA20": c1.stack(), "c2_MA20>MA60": c2.stack(),
        "c3_近20日>0": c3.stack(), "c4_MA20斜率>0": c4.stack()})
    print("\n四条件两两相关（相关系数）:")
    print(flat.corr().round(2).to_string())
    return dict(ics=ics, buckets=buckets)


if __name__ == "__main__":
    conn = vl.connect()
    try:
        q1_quality(conn)
        print("\n\n")
        q2_projection(conn)
        print("\n\n")
        q3_trend(conn)
    finally:
        conn.close()

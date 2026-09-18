#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
追问三题：
  Q2  利润外推在「连续三年 ROE 达标」子集里是否更准
  Q3  多种趋势信号的预测力横向实测（IC / 分位价差）
"""

import numpy as np
import pandas as pd

import valuelib as vl

pd.set_option("display.width", 220)
pd.set_option("display.unicode.east_asian_width", True)


def get_3yr_roe_codes(conn, cfg, base_year=2025):
    """连续三年 ROE≥阈值 且 负债<50% 的代码集合"""
    fi = vl.query(
        "SELECT ts_code, report_year, roe FROM financial_indicator "
        "WHERE report_type='4' AND report_year IN (2023,2024,2025)", conn=conn)
    w = fi.pivot_table(index="ts_code", columns="report_year", values="roe")
    ok = (w[2023] >= cfg["screen"]["roe_min"]) & (w[2024] >= cfg["screen"]["roe_min"]) & (w[2025] >= cfg["screen"]["roe_min"])
    return set(ok[ok].index), w


def q2_subset(conn, cfg):
    """外推验证：全样本 vs 三年ROE子集"""
    res = vl.apply_screen(vl.fetch_screen_universe(cfg, 2025), cfg, 2025)
    codes = res["ts_code"].tolist()
    c3, _ = get_3yr_roe_codes(conn, cfg)

    hist = vl.query(
        "SELECT ts_code, report_year, n_income_attr_p FROM income "
        "WHERE report_type='4' AND report_year BETWEEN 2009 AND 2025 AND ts_code=ANY(%s)",
        (codes,), conn=conn)
    prof = {ts: dict(zip(g["report_year"].astype(int), g["n_income_attr_p"])) for ts, g in hist.groupby("ts_code")}

    def cagr(v):
        v = [float(x) for x in v]
        return (v[-1] / v[0]) ** (1 / (len(v) - 1)) - 1

    def clamp(g):
        return min(max(g, -0.10), 0.30)

    rows = []
    for base in range(2013, 2022):
        for ts, s in prof.items():
            nb, na = s.get(base), s.get(base + 3)
            if nb is None or na is None or nb <= 0 or na <= 0:
                continue
            pts5 = [s.get(base - k) for k in range(4, -1, -1)]
            if all(p is not None and p > 0 for p in pts5):
                g = clamp(cagr(pts5))
                rows.append(dict(base=base, ts_code=ts, actual3=(na / nb) ** (1 / 3) - 1,
                                 g5=g, in3=ts in c3))
    df = pd.DataFrame(rows)

    def metric(sub):
        e = ((1 + sub["g5"]) ** 3 / (1 + sub["actual3"]) ** 3 - 1).abs()
        return dict(n=len(sub), 中位误差=e.median(), 均值误差=e.mean(),
                    le30=(e < 0.30).mean(), le50=(e < 0.50).mean(),
                    方向命中=(np.sign(sub["g5"]) == np.sign(sub["actual3"])).mean(),
                    实际3年增速中位=sub["actual3"].median())

    print("=" * 70)
    print("Q2 外推误差：全样本 vs 三年ROE子集（基年 2013~2021）")
    print("=" * 70)
    full = metric(df)
    sub = metric(df[df["in3"]])
    comp = metric(df[~df["in3"]])
    for name, m in [("全样本(595)", full), ("三年ROE子集", sub), ("非三年ROE(对照)", comp)]:
        print(f"{name:<12} n={m['n']:>5}  中位误差 {m['中位误差']:>6.1%}  均值 {m['均值误差']:>6.1%}"
              f"  ≤30% {m['le30']:>5.1%}  ≤50% {m['le50']:>5.1%}"
              f"  方向命中 {m['方向命中']:>5.1%}  实际增速中位 {m['实际3年增速中位']:>5.1%}")
    return df


def q3_trend_compare(conn, cfg):
    """多信号 → 未来 20/60 日收益的截面 IC 与分位价差"""
    res = vl.apply_screen(vl.fetch_screen_universe(cfg, 2025), cfg, 2025)
    codes = res["ts_code"].tolist()

    px = vl.query(
        "SELECT ts_code, trade_date, close FROM daily_quote "
        "WHERE trade_date >= '2021-12-01' AND ts_code=ANY(%s) ORDER BY ts_code, trade_date",
        (codes,), conn=conn)
    px["trade_date"] = pd.to_datetime(px["trade_date"])
    wide = px.pivot(index="trade_date", columns="ts_code", values="close").sort_index()

    close = wide
    ret20 = close / close.shift(20) - 1
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    ma20slope = ma20 / ma20.shift(5) - 1
    ma_align = ((close > ma20).astype(int) + (ma20 > ma60).astype(int)
                + (ret20 > 0).astype(int) + (ma20slope > 0).astype(int))

    # 60日动量 t-score（时间序列动量）
    r60 = close / close.shift(60) - 1
    vol60 = close.pct_change().rolling(60).std() * np.sqrt(252)
    mom_t60 = r60 / vol60

    mom_12_1 = close.shift(21) / close.shift(252) - 1
    prox_52w = close / close.rolling(252).max() - 1

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    macd_hist = dif - dif.ewm(span=9, adjust=False).mean()

    signals = {
        "MA四条件(当前)": ma_align,
        "20日动量": ret20,
        "60日动量t-score": mom_t60,
        "12-1月动量": mom_12_1,
        "距52周高点": prox_52w,
        "MACD柱": macd_hist,
    }
    fwd20 = close.shift(-20) / close - 1
    fwd60 = close.shift(-60) / close - 1

    n = len(close)
    snaps = list(range(300, n - 60, 20))

    print("\n" + "=" * 70)
    print("Q3 各趋势信号 → 未来收益 截面 rank-IC（快照 %d 个，合格list）" % len(snaps))
    print("=" * 70)
    print(f"{'信号':<16}{'IC(20日)':>10}{'ICIR':>8}{'正值比':>8}{'IC(60日)':>10}{'ICIR':>8}{'L/S价差20日':>12}")

    for name, sig in signals.items():
        ic20, ic60 = [], []
        for i in snaps:
            s20 = sig.iloc[i]
            s60 = sig.iloc[i] if i + 60 < n else sig.iloc[i]
            f20 = fwd20.iloc[i]
            f60 = fwd60.iloc[i]
            m20 = s20.notna() & f20.notna()
            m60 = s60.notna() & f60.notna()
            if m20.sum() > 50:
                ic20.append(s20[m20].rank().corr(f20[m20].rank()))
            if m60.sum() > 50:
                ic60.append(s60[m60].rank().corr(f60[m60].rank()))
        ic20 = np.array([x for x in ic20 if not np.isnan(x)])
        ic60 = np.array([x for x in ic60 if not np.isnan(x)])

        # 分位价差：top20% - bottom20% 的 20日收益
        spreads = []
        for i in snaps:
            s = sig.iloc[i]
            f = fwd20.iloc[i]
            m = s.notna() & f.notna()
            if m.sum() < 60:
                continue
            si, fi = s[m], f[m]
            q = pd.qcut(si.rank(method="first"), 5, labels=False)
            top = fi[q == 4].mean()
            bot = fi[q == 0].mean()
            spreads.append(top - bot)
        ls = np.nanmean(spreads) if spreads else np.nan

        icir = lambda a: a.mean() / a.std() if len(a) and a.std() > 0 else np.nan
        print(f"{name:<16}{ic20.mean():>+9.3f}{icir(ic20):>8.2f}{(ic20>0).mean():>7.0%}"
              f"{ic60.mean():>+9.3f}{icir(ic60):>8.2f}{ls:>+11.1%}")
    return dict(wide=wide, signals=signals)


if __name__ == "__main__":
    conn = vl.connect()
    try:
        cfg = vl.load_config()
        q2_subset(conn, cfg)
        q3_trend_compare(conn, cfg)
    finally:
        conn.close()

#!/usr/bin/env python3
"""择时叠加层系统测试: 在 TopN30 个股截面策略之上测试 6 种仓位调节方案
====================================================================================
背景: timing_eval (2026-09-05) 对 Q5 否决过 MA200 择时, 但当时 ①信号用沪深300 (beta 错配)
②无缓冲带 (震荡期反复打脸) ③只有方向信号。本脚本补全测法重判。

方案 (m_t = 当日仓位乘数, 全部用 T-1 信息, 无前视):
  baseline   : 永远满仓 1.0
  ma300_0    : 沪深300 < MA200 → 0 (复刻旧否决, 换 TopN30 载体)
  ma300_band : 300 < MA200×0.97 → 0.5; > ×1.03 → 1.0; 区间内保持 (缓冲带+迟滞)
  ma1000_band: 中证1000 同规则 (信号与策略 beta 匹配)
  vol_strat  : 波动率目标 (Moreira-Muir 2017): m = clip(0.25 / 策略自身EWMA20波动, 0.4, 1)
  vol_1000   : m = clip(0.25 / 中证1000 EWMA20波动, 0.4, 1)
  combo      : ma1000_band × vol_strat 两者取积

口径: 仓位乘数作用于策略日收益 (日度调整, 为理论上界; 月内调仓实操略弱); 空出仓位收益记 0

用法: python3 scripts/timing_overlay_test.py --start 2019-01-01 [--topn 30]
输出: reports/timing_overlay_test.md
"""
import argparse
import os
import sys

import numpy as np
import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import factor_eval as fe  # noqa: E402
import strategy_lib as sl  # noqa: E402
from backtest_report import replay  # noqa: E402

VOL_TARGET = 0.25
VOL_FLOOR = 0.4
BAND = 0.03
LOW_POS = 0.5


def fmt(v):
    return "-" if v is None or not np.isfinite(v) else f"{v*100:.1f}%"


def stats(nav: pd.Series) -> dict:
    ret = nav.pct_change().dropna()
    if len(ret) < 60:
        return {}
    years = len(ret) / 244
    ann = (nav.iloc[-1] / nav.iloc[0]) ** (1 / years) - 1
    vol = ret.std() * np.sqrt(244)
    dd = (nav / nav.cummax() - 1).min()
    return {"ann": ann, "vol": vol, "sharpe": ann / vol if vol > 0 else np.nan, "dd": dd,
            "total": nav.iloc[-1] / nav.iloc[0] - 1}


def load_index_close(conn, symbol, start, end) -> pd.Series:
    df = pd.read_sql(f"SELECT trade_date, close FROM index_daily WHERE symbol='{symbol}' "
                     f"AND trade_date >= '{start}' AND trade_date <= '{end}'", conn)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df.set_index("trade_date")["close"].sort_index()


def ma_series(close: pd.Series, n=200) -> pd.Series:
    return close.rolling(n).mean()


def overlay_positions(idx, close, strat_ret, kind) -> pd.Series:
    """T-1 信息 → T 日仓位乘数"""
    ma = ma_series(close)
    vol = strat_ret.ewm(span=20).std() * np.sqrt(244) if strat_ret is not None else None
    volx = close.pct_change().ewm(span=20).std() * np.sqrt(244)
    pos = {}
    cur = 1.0
    for t in idx:
        prev = close.index[close.index < t]
        if len(prev) == 0:
            pos[t] = 1.0
            continue
        pt = prev[-1]
        if kind == "baseline":
            cur = 1.0
        elif kind == "ma300_0":
            cur = 1.0 if close[pt] > ma[pt] else 0.0
        elif kind in ("ma300_band", "ma1000_band"):
            c, m = close[pt], ma[pt]
            if pd.notna(m):
                if c < m * (1 - BAND):
                    cur = LOW_POS
                elif c > m * (1 + BAND):
                    cur = 1.0
                # 区间内: 保持 (迟滞)
        elif kind == "vol_strat":
            v = vol.asof(pt) if len(vol) else np.nan
            cur = float(np.clip(VOL_TARGET / v, VOL_FLOOR, 1.0)) if pd.notna(v) and v > 0 else 1.0
        elif kind == "vol_1000":
            v = volx.asof(pt)
            cur = float(np.clip(VOL_TARGET / v, VOL_FLOOR, 1.0)) if pd.notna(v) and v > 0 else 1.0
        pos[t] = cur
    return pd.Series(pos)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--topn", type=int, default=30)
    args = ap.parse_args()
    end = pd.Timestamp.today().strftime("%Y-%m-%d")
    conn = fe.get_conn()

    # 策略日收益 (TopN 个股截面)
    uni, list_dates = fe.load_universe_filter(conn)
    st = sl.load_strategies(conn, [f"prod_6f_eq@topn={args.topn}"])[0]
    factors = {n: sl.load_factor(conn, n, args.start, end) for n in st["cfg"]["factors"]}
    load_start = (pd.Timestamp(args.start) - pd.Timedelta(days=40)).strftime("%Y-%m-%d")
    daily_ret = fe.load_daily_returns(conn, load_start, end)
    rebal = fe.rebalance_dates(daily_ret.index, args.start, end)
    r = replay(st, factors, daily_ret, uni, list_dates, rebal)
    strat_ret = r["nav"].pct_change().fillna(0)

    close300 = load_index_close(conn, "000300.SH", "2018-01-01", end)
    close1000 = load_index_close(conn, "000852.SH", "2018-01-01", end)
    conn.close()
    idx = strat_ret.index

    variants = [
        ("baseline 满仓", "baseline", close300),
        ("MA300 无带→清仓 (旧否决复刻)", "ma300_0", close300),
        ("MA300 ±3%带→半仓 (迟滞)", "ma300_band", close300),
        ("MA中证1000 ±3%带→半仓", "ma1000_band", close1000),
        ("波动率目标-策略自身 (25%)", "vol_strat", close300),
        ("波动率目标-中证1000 (25%)", "vol_1000", close1000),
    ]
    lines = [f"# 择时叠加层测试 (TopN{args.topn} 个股截面, {args.start} ~ {end})\n",
             f"- 仓位乘数: T-1 信息 → T 日, 日度调整 (理论上界); 空仓收益记 0; 单边成本已含在策略净值",
             f"- 参数: 缓冲带 ±{BAND*100:.0f}% | 低位仓位 {LOW_POS} | 波动目标 {VOL_TARGET*100:.0f}% | 下限 {VOL_FLOOR}\n",
             "| 方案 | 年化 | 夏普 | 最大回撤 | 平均仓位 |", "|---|---|---|---|---|"]
    results = {}
    for label, kind, closer in variants:
        pos = overlay_positions(idx, closer, strat_ret, kind).reindex(idx).ffill().fillna(1.0)
        if kind == "vol_strat" and False:
            pass
        nav = (1 + strat_ret * pos).cumprod()
        s = stats(nav)
        results[label] = (nav, pos, s)
        lines.append(f"| {label} | {fmt(s['ann'])} | {s['sharpe']:.2f} | {fmt(s['dd'])} | {pos.mean():.2f} |")
        print(f"[{label}] 年化 {fmt(s['ann'])} 夏普 {s['sharpe']:.2f} 回撤 {fmt(s['dd'])} 平均仓位 {pos.mean():.2f}")

    # combo: ma1000_band × vol_strat
    pos_ma = overlay_positions(idx, close1000, strat_ret, "ma1000_band").reindex(idx).ffill().fillna(1.0)
    pos_v = overlay_positions(idx, close300, strat_ret, "vol_strat").reindex(idx).ffill().fillna(1.0)
    pos_c = (pos_ma * pos_v).clip(lower=0.2)
    nav_c = (1 + strat_ret * pos_c).cumprod()
    s = stats(nav_c)
    results["combo MA1000×vol"] = (nav_c, pos_c, s)
    lines.append(f"| combo: MA1000带 × 波动率目标 | {fmt(s['ann'])} | {s['sharpe']:.2f} | {fmt(s['dd'])} | {pos_c.mean():.2f} |")
    print(f"[combo] 年化 {fmt(s['ann'])} 夏普 {s['sharpe']:.2f} 回撤 {fmt(s['dd'])} 平均仓位 {pos_c.mean():.2f}")

    # 年度分解: baseline vs 两个最有希望的
    lines.append("\n## 年度对照\n")
    lines.append("| 年份 | 满仓 | MA1000带 | 波动率目标 | combo |")
    lines.append("|---|---|---|---|---|")
    keys = ["baseline 满仓", "MA中证1000 ±3%带→半仓", "波动率目标-策略自身 (25%)", "combo MA1000×vol"]
    for y in sorted(set(idx.year)):
        row = [str(y)]
        for k in keys:
            nav = results[k][0]
            seg = nav[nav.index.year == y]
            row.append(fmt(seg.iloc[-1] / seg.iloc[0] - 1) if len(seg) > 20 else "-")
        lines.append("| " + " | ".join(row) + " |")

    # 回撤最深的 60 天: baseline vs combo
    bnav = results["baseline 满仓"][0]
    dd = bnav / bnav.cummax() - 1
    trough = dd.idxmin()
    lines.append(f"\n满仓版最大回撤谷底: {trough.date()} ({dd.min()*100:.1f}%); "
                 f"combo 同日回撤 {(nav_c/trough_date_nav(nav_c, trough)-1)*100:.1f}%" if False else
                 f"\n满仓版最大回撤谷底: {trough.date()} ({dd.min()*100:.1f}%)")

    out = os.path.join(REPO, "reports", "timing_overlay_test.md")
    with open(out, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n✅ 报告: {out}")


def trough_date_nav(nav, trough):
    seg = nav[nav.index <= trough]
    return seg.iloc[0] if len(seg) else nav.iloc[0]


if __name__ == "__main__":
    main()

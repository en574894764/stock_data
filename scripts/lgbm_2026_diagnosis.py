#!/usr/bin/env python3
"""2026 年 LGBM+中性化 亏损细查 (P0#5 模拟盘前风控诊断)
====================================================================================
背景: lgbm_synth_test 2026 年 (7期) LGBM中性 -13.6% vs LGBM原始 +29.5%, 但 IC 9.2% 尚可。
疑问: IC 正常但净值亏 → 疑似 Top30 尾部集中踩雷, 而非整体排序失效。

做法: 复用 replay 拿 prod_lgbm_neu 的逐期持仓与收益, 聚焦 2026:
  ① 每期收益 vs 同期中证1000, 定位亏损期
  ② 每期 Top30 里亏损个股数/最大单票跌幅 (尾部踩雷指标)
  ③ 最差期的持仓明细 + 个股区间收益

用法: python3 scripts/lgbm_2026_diagnosis.py
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
YEAR = 2026


def pct(v):
    return "-" if v is None or not np.isfinite(v) else f"{v*100:+.1f}%"


def main():
    conn = fe.get_conn()
    st = sl.load_strategies(conn, ["prod_lgbm_neu"])[0]
    uni, list_dates = fe.load_universe_filter(conn)
    end = pd.Timestamp.today().strftime("%Y-%m-%d")
    load_start = (pd.Timestamp(START) - pd.Timedelta(days=40)).strftime("%Y-%m-%d")
    daily_ret = fe.load_daily_returns(conn, load_start, end)
    rebal = fe.rebalance_dates(daily_ret.index, START, end)
    factors = {n: sl.load_factor(conn, n, START, end) for n in st["cfg"]["factors"]}

    # 基准中证1000
    cur = conn.cursor()
    cur.execute("SELECT trade_date, pct_chg FROM index_daily WHERE symbol='000852.SH' "
                "AND trade_date >= %s", (f"{YEAR}-01-01",))
    bdf = pd.DataFrame(cur.fetchall(), columns=["trade_date", "pct_chg"])
    cur.close()
    bdf["pct_chg"] = pd.to_numeric(bdf["pct_chg"], errors="coerce") / 100.0
    bdf["trade_date"] = pd.to_datetime(bdf["trade_date"])
    bench_ret = bdf.set_index("trade_date")["pct_chg"].sort_index()

    r = replay(st, factors, daily_ret, uni, list_dates, rebal)
    periods = r["periods"].copy()
    periods["year"] = periods["exec"].dt.year
    periods = periods[periods["year"] == YEAR]
    conn.close()

    lines = [f"# {YEAR} 年 LGBM+市值中性化 逐期诊断\n",
             f"- 调仓 {len(periods)} 期 | 目标: 定位 IC 正常但净值 -13.6% 的根因 (尾部踩雷 vs 系统性)\n",
             "| 调仓日 | 执行日 | 持仓 | 换手 | 期收益 | 同期中证1000 | 超额 |",
             "|---|---|---|---|---|---|---|"]

    dr_idx = daily_ret.index
    worst_period = None
    worst_ret = 0.0
    for _, p in periods.iterrows():
        t, exd = pd.Timestamp(p["rebal"]), pd.Timestamp(p["exec"])
        bseg = bench_ret[(bench_ret.index >= exd) & (bench_ret.index <= exd + pd.Timedelta(days=fe.REBAL))]
        bret = (1 + bseg).prod() - 1 if len(bseg) else np.nan
        lines.append(f"| {t.date()} | {exd.date()} | {int(p['n'])} | {p['turnover']*100:.0f}% "
                     f"| {pct(p['period_ret'])} | {pct(bret)} | {pct(p['period_ret']-bret)} |")
        if p["period_ret"] < worst_ret:
            worst_ret, worst_period = p["period_ret"], (t, exd)

    # 尾部踩雷指标: 每期持仓内个股区间收益, 数亏损>15% 的票
    lines.append("\n## 尾部踩雷指标 (Top30 内单票区间收益分布)\n")
    lines.append("| 执行日 | 亏损>15%票数 | 最大单票跌幅 | 最差票 |")
    lines.append("|---|---|---|---|")
    for _, p in periods.iterrows():
        exd = pd.Timestamp(p["exec"])
        pos_dates = [d for d in dr_idx if d > p["rebal"]][:fe.REBAL]
        # 当期持仓: 该调仓日的买入集合 (从 trades 重建)
        seg_trades = r["trades"][r["trades"]["exec_date"] == exd]
        buys = seg_trades[seg_trades["action"] == "BUY"]["ts_code"].tolist()
        if not buys or not pos_dates:
            continue
        row_pos = dr_idx.get_indexer(pos_dates)
        m = daily_ret.iloc[row_pos, daily_ret.columns.get_indexer(buys)].to_numpy(dtype=np.float64)
        with np.errstate(invalid="ignore"):
            fwd = np.nanprod(1 + m, axis=0) - 1
        fwd = pd.Series(fwd, index=buys).dropna()
        losers = (fwd < -0.15).sum()
        worst_code = fwd.idxmin() if len(fwd) else "-"
        worst_v = fwd.min() if len(fwd) else np.nan
        lines.append(f"| {exd.date()} | {losers} | {pct(worst_v)} | {worst_code} |")

    # 最差期持仓明细
    if worst_period is not None:
        t, exd = worst_period
        pos_dates = [d for d in dr_idx if d > t][:fe.REBAL]
        seg_trades = r["trades"][r["trades"]["exec_date"] == exd]
        buys = seg_trades[seg_trades["action"] == "BUY"]["ts_code"].tolist()
        row_pos = dr_idx.get_indexer(pos_dates)
        m = daily_ret.iloc[row_pos, daily_ret.columns.get_indexer(buys)].to_numpy(dtype=np.float64)
        with np.errstate(invalid="ignore"):
            fwd = np.nanprod(1 + m, axis=0) - 1
        fwd = pd.Series(fwd, index=buys).sort_values()
        lines.append(f"\n## 最差期 {exd.date()} 持仓明细 (期收益 {pct(worst_ret)}, 按个股区间收益升序)\n")
        lines.append("| 代码 | 区间收益 |")
        lines.append("|---|---|")
        for c, v in fwd.items():
            lines.append(f"| {c} | {pct(v)} |")

    report = "\n".join(lines)
    print(report)
    out = os.path.join(REPO, "reports", "lgbm_2026_diagnosis.md")
    with open(out, "w") as f:
        f.write(report + "\n")
    print(f"\n✅ 报告: {out}")


if __name__ == "__main__":
    main()

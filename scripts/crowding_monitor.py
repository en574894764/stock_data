#!/usr/bin/env python3
"""因子拥挤度监控 → reports/crowding_monitor.md

拥挤度代理指标 (全部基于已有 factor_value + daily_basic, 无新数据源):
  1. 多空换手价差   多头组(Q5)均值换手 - 空头组(Q1)均值换手, 取过去 20 日均值
                  多头被持续追买 → 价差异常高 → 拥挤
  2. 多头换手分位   多头组换手率在过去 3 年的滚动分位
  3. 因子动量       因子多空净值过去 60 日收益 (动量极高后常见反转)
  4. 多头组特质波动 多头组日收益横截面 std 的 20 日均值 (微观结构恶化信号)

解读: 分位 > 0.9 = 红色 (拥挤/过热), 0.7~0.9 = 黄色 (升温), 其余绿色。
拥挤本身不是卖出信号, 是"该因子收益可能透支"的预警, 用于调整暴露。

用法:
  python3 scripts/crowding_monitor.py            # 全历史扫描 (慢, 一次性)
  python3 scripts/crowding_monitor.py --days 60  # 只看最近 60 日 (pipeline 挂钩用)
"""
import argparse
import os
import sys
from datetime import date

import numpy as np
import pandas as pd
import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compute_factors import get_conn  # noqa: E402

QUANTILES = 5
COMBO_FACTORS = ["ret_20d_rev", "turnover_20", "ivol_60", "ln_mv", "ep_ttm", "sue_delta"]  # 6 因子最优配置


def load_matrix(conn, table, col, start):
    sql = (f"SELECT ts_code, trade_date, {col} FROM {table} "
           f"WHERE trade_date >= '{start}' AND (ts_code LIKE '%.SZ' OR ts_code LIKE '%.SH')")
    df = pd.read_sql(sql, conn)
    df[col] = pd.to_numeric(df[col], errors="coerce")
    wide = df.pivot_table(index="trade_date", columns="ts_code", values=col, aggfunc="last")
    return wide.sort_index()


def load_factor(conn, name, start):
    sql = (f"SELECT trade_date, ts_code, value FROM factor_value "
           f"WHERE factor_name = '{name}' AND trade_date >= '{start}'")
    df = pd.read_sql(sql, conn)
    wide = df.pivot_table(index="trade_date", columns="ts_code", values="value", aggfunc="last")
    return wide.sort_index()


def q_groups(factor_row, q=QUANTILES):
    """因子截面五分组, 返回 (空头组, 多头组) 股票集合"""
    row = factor_row.dropna()
    if len(row) < 50:
        return None, None
    try:
        qcut = pd.qcut(row.rank(method="first"), q, labels=False) + 1
    except ValueError:
        return None, None
    return qcut[qcut == 1].index, qcut[qcut == q].index


def rolling_pctile(s: pd.Series, window=750) -> pd.Series:
    """滚动分位: 当前值在过去 window 日中的位置"""
    return s.rolling(window, min_periods=250).apply(lambda x: (x[-1] >= x).mean(), raw=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None, help="只输出最近 N 日监控 (默认全历史)")
    ap.add_argument("--start", default="2019-01-01")
    args = ap.parse_args()

    conn = get_conn()
    end = date.today().strftime("%Y-%m-%d")

    print("加载数据...")
    turn = load_matrix(conn, "daily_basic", "turnover_rate", args.start)  # 日换手 (%)
    ret = load_matrix(conn, "daily_quote", "pct_chg", args.start) / 100.0  # 日收益

    factors = {n: load_factor(conn, n, args.start) for n in COMBO_FACTORS}
    common = sorted(set(ret.index) & set(turn.index))

    # 逐日: 每因子多空组换手/收益
    # 固定扫最近 ~900 日: 滚动分位窗口 750 日 + 余量, 全历史分位与近期分位差异有限
    rows = []
    for t in common[-900:]:
        rec = {"date": t}
        for name, f in factors.items():
            if t not in f.index:
                continue
            lo, hi = q_groups(f.loc[t])
            if lo is None:
                continue
            t_turn = turn.loc[t] if t in turn.index else None
            t_ret = ret.loc[t] if t in ret.index else None
            # 因子截面含退市股, 先与行情矩阵列交集
            if t_turn is not None:
                lo_t, hi_t = lo.intersection(t_turn.index), hi.intersection(t_turn.index)
                rec[f"{name}_lo_turn"] = t_turn[lo_t].dropna().mean()
                rec[f"{name}_hi_turn"] = t_turn[hi_t].dropna().mean()
            if t_ret is not None:
                lo_r, hi_r = lo.intersection(t_ret.index), hi.intersection(t_ret.index)
                rec[f"{name}_ls_ret"] = t_ret[hi_r].dropna().mean() - t_ret[lo_r].dropna().mean()
                rec[f"{name}_hi_idio"] = t_ret[hi_r].dropna().std()
        rows.append(rec)
    daily = pd.DataFrame(rows).set_index("date").sort_index()

    if daily.empty:
        print("无数据"); return

    # 拥挤度指标
    lines = ["# 因子拥挤度监控\n"]
    lines.append(f"- 范围: {daily.index[0]} ~ {daily.index[-1]}, 6 因子最优配置 (月度调仓口径的日频代理)\n")
    lines.append("| 因子 | 多空换手价差(20d) | 多空换手价差分位 | 多头换手分位 | 因子动量60d | 动量分位 | 状态 |")
    lines.append("|---|---|---|---|---|---|---|")

    alerts = []
    for name in COMBO_FACTORS:
        spread = (daily[f"{name}_hi_turn"] - daily[f"{name}_lo_turn"]).rolling(20, min_periods=15).mean()
        spread_pct = rolling_pctile(spread)
        hi_turn_pct = rolling_pctile(daily[f"{name}_hi_turn"].rolling(20, min_periods=15).mean())
        mom = (1 + daily[f"{name}_ls_ret"].fillna(0)).cumprod()
        mom60 = mom.pct_change(60)
        mom_pct = rolling_pctile(mom60)

        cur_spread = spread.iloc[-1]
        cur_spread_pct = spread_pct.iloc[-1]
        cur_hi_pct = hi_turn_pct.iloc[-1]
        cur_mom = mom60.iloc[-1]
        cur_mom_pct = mom_pct.iloc[-1]

        # 状态判定: 任一维度 > 0.9 红, > 0.7 黄
        pcts = [x for x in (cur_spread_pct, cur_hi_pct, cur_mom_pct) if not np.isnan(x)]
        worst = max(pcts) if pcts else np.nan
        status = "🔴 红" if worst > 0.9 else ("🟡 黄" if worst > 0.7 else "🟢 绿")

        def f3(v, pct=True):
            if v is None or (isinstance(v, float) and np.isnan(v)):
                return "-"
            return f"{v*100:.0f}%" if pct else f"{v:.2f}"
        lines.append(f"| {name} | {f3(cur_spread, False)} | {f3(cur_spread_pct)} | {f3(cur_hi_pct)} "
                     f"| {f3(cur_mom)} | {f3(cur_mom_pct)} | {status} |")
        if worst > 0.9:
            alerts.append(f"{name} 拥挤度红色 ({worst*100:.0f}% 分位)")
        elif worst > 0.7:
            alerts.append(f"{name} 拥挤度升温 ({worst*100:.0f}% 分位)")

    lines.append("\n> 解读: 分位基于过去 ~3 年滚动窗口。红 = 收益可能透支, 考虑降暴露; 黄 = 升温观察; 绿 = 正常。")
    lines.append("> 拥挤不是卖出信号本身, 是风险预算的调节器。\n")
    if alerts:
        lines.append("**当前预警**: " + "; ".join(alerts))
    else:
        lines.append("**当前预警**: 无")

    report = "\n".join(lines)
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports", "crowding_monitor.md")
    with open(out, "w") as f:
        f.write(report)
    print(report)
    print(f"\n报告已写入: {out}")
    conn.close()


if __name__ == "__main__":
    main()

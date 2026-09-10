#!/usr/bin/env python3
"""确认式做多测试: "上涨趋势确认才做多, 不确认/无依据 → 空仓"
====================================================================================
与 timing_overlay_test 的区别: 那轮测的是"默认满仓, 信号不好才减仓"(防守型);
本脚本测 James 提出的进攻型哲学——**默认空仓, 确认了才做多**。null 状态 = 0 仓位。

确认定义 (全部 T-1 信息, 无前视; 信号无历史/无依据时按"不确认"处理 → 0):
  confirm_ma300   : 沪深300 > MA200 才做多 (纯形态, 日常版)
  confirm_ma1000  : 中证1000 > MA200 才做多 (与策略 beta 匹配)
  confirm_mom12   : 中证1000 过去 252 日动量 > 0 才做多 (Ilmanen 绝对动量, 月度决策)
  confirm_high252 : 中证1000 收盘在 252 日高点 10% 以内才做多 (创新高确认, 最强确认)
  confirm_dual    : MA200 之上 且 12 月动量为正 (双重确认)
  confirm_strat   : 策略自身净值 > 自身 MA200 才做多 (对策略自身的趋势确认)

另附条件收益表: 策略日收益按信号状态分组, 直接回答"未确认的日子里策略在赚还是亏"。

用法: python3 scripts/confirm_gate_test.py
输出: reports/confirm_gate_test.md
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

WARMUP = "2017-01-01"   # replay 起点 (为 strat_ma 提供净值历史)
START = "2019-01-01"    # 统计起点 (与 timing_overlay_test 对齐)
TOPN = 30
HIGH_PCT = 0.10


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
    return {"ann": ann, "vol": vol, "sharpe": ann / vol if vol > 0 else np.nan, "dd": dd}


def load_index_close(conn, symbol, start, end) -> pd.Series:
    df = pd.read_sql(f"SELECT trade_date, close FROM index_daily WHERE symbol='{symbol}' "
                     f"AND trade_date >= '{start}' AND trade_date <= '{end}'", conn)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df.set_index("trade_date")["close"].sort_index()


# ---------- 确认信号 (Series → Series[0/1], 定义在指数日历上) ----------

def gate_ma(close: pd.Series, n=200) -> pd.Series:
    ma = close.rolling(n).mean()
    g = (close > ma).astype(float)
    g[ma.isna()] = 0.0  # 无依据 → 不做多
    return g


def gate_mom12_monthly(close: pd.Series) -> pd.Series:
    """月度决策: 上月末动量状态在本月生效 (Ilmanen 绝对动量)"""
    mom = close / close.shift(252) - 1
    month_last = close.groupby(close.index.to_period("M")).apply(lambda x: x.index[-1])
    decisions = pd.Series({dt: (1.0 if (pd.notna(mom[dt]) and mom[dt] > 0) else 0.0)
                           for dt in month_last}).sort_index()
    # 月末决策从次日起生效: reindex 到日历后 ffill 再 shift(1)
    daily = decisions.reindex(close.index).ffill()
    g = daily.shift(1)
    g.iloc[0] = 0.0
    return g.fillna(0.0)


def gate_high252(close: pd.Series, pct=HIGH_PCT) -> pd.Series:
    rollmax = close.rolling(252, min_periods=60).max()
    g = (close >= rollmax * (1 - pct)).astype(float)
    g[rollmax.isna()] = 0.0
    return g


def apply_gate(strat_ret: pd.Series, gate: pd.Series) -> pd.Series:
    """gate 定义在指数日历, T-1 已知 → T 日生效: 先 shift 再对齐策略日历"""
    g = gate.shift(1).reindex(strat_ret.index).ffill().fillna(0.0)
    return g


def main():
    end = pd.Timestamp.today().strftime("%Y-%m-%d")
    conn = fe.get_conn()

    # 主路径: rebal 网格锚 2019-01 (与 timing_overlay_test 及全部历史报告可比)
    uni, list_dates = fe.load_universe_filter(conn)
    st = sl.load_strategies(conn, [f"prod_6f_eq@topn={TOPN}"])[0]
    factors = {n: sl.load_factor(conn, n, START, end) for n in st["cfg"]["factors"]}
    load_start = (pd.Timestamp(START) - pd.Timedelta(days=40)).strftime("%Y-%m-%d")
    daily_ret = fe.load_daily_returns(conn, load_start, end)
    rebal = fe.rebalance_dates(daily_ret.index, START, end)
    r = replay(st, factors, daily_ret, uni, list_dates, rebal)
    strat_ret = r["nav"].pct_change().fillna(0)

    # 热身路径 (rebal 锚 2017): 仅用于 ①策略自身趋势门的净值历史 ②网格锚点敏感性注记
    factors_w = {n: sl.load_factor(conn, n, WARMUP, end) for n in st["cfg"]["factors"]}
    load_start_w = (pd.Timestamp(WARMUP) - pd.Timedelta(days=40)).strftime("%Y-%m-%d")
    daily_ret_w = fe.load_daily_returns(conn, load_start_w, end)
    rebal_w = fe.rebalance_dates(daily_ret_w.index, WARMUP, end)
    r_w = replay(st, factors_w, daily_ret_w, uni, list_dates, rebal_w)

    close300 = load_index_close(conn, "000300.SH", "2016-06-01", end)
    close1000 = load_index_close(conn, "000852.SH", "2016-06-01", end)
    conn.close()
    idx = strat_ret.index

    # 策略自身净值 MA 门 (用 2017 起的热身净值作信号源, 2019 内无冷启动缺口)
    nav_w = r_w["nav"]
    nav_ma = nav_w.rolling(200).mean()
    gate_strat = (nav_w > nav_ma).astype(float)
    gate_strat[nav_ma.isna()] = 0.0

    variants = [
        ("baseline 满仓", None),
        ("确认: 沪深300>MA200 才做多", gate_ma(close300)),
        ("确认: 中证1000>MA200 才做多", gate_ma(close1000)),
        ("确认: 12月动量>0 才做多 (月度)", gate_mom12_monthly(close1000)),
        ("确认: 52周高点10%以内才做多", gate_high252(close1000)),
        ("确认: MA200之上 且 12月动量>0 (双确认)", (gate_ma(close1000) * gate_mom12_monthly(close1000)).clip(upper=1)),
        ("确认: 策略净值>自身MA200 才做多", gate_strat),
    ]
    lines = [f"# 确认式做多测试 (TopN{TOPN} 个股截面, {START} ~ {end})\n",
             "- 哲学: 默认空仓, 趋势确认才做多; 信号无历史/无依据 → 0 仓位 (严格按\"不确认不做多\")",
             "- 信号 T-1 已知 → T 日生效; 指数数据从 2016-06 载入保证信号在统计窗口内无冷启动缺口",
             "- 策略净值含单边成本; 仓位门翻转的指数级交易成本未计 (各门翻转频率见下表)",
             "- 主路径 rebal 网格锚 2019-01 (与历史报告可比); 网格锚点敏感性见文末注记\n",
             "| 方案 | 年化 | 夏普 | 最大回撤 | 平均仓位 | 翻转次数/年 |", "|---|---|---|---|---|---|"]
    results = {}
    for label, gate in variants:
        if gate is None:
            pos = pd.Series(1.0, index=idx)
        else:
            pos = apply_gate(strat_ret, gate) if "策略净值" not in label else \
                gate.shift(1).reindex(idx).ffill().fillna(0.0)
        nav = (1 + strat_ret * pos).cumprod()
        s = stats(nav)
        flips = (pos.diff().fillna(0) != 0).sum() / (len(pos) / 244)
        results[label] = (nav, pos, s)
        lines.append(f"| {label} | {fmt(s['ann'])} | {s['sharpe']:.2f} | {fmt(s['dd'])} | "
                     f"{pos.mean():.2f} | {flips:.0f} |")
        print(f"[{label}] 年化 {fmt(s['ann'])} 夏普 {s['sharpe']:.2f} 回撤 {fmt(s['dd'])} "
              f"仓位 {pos.mean():.2f} 翻转 {flips:.0f}/年")

    # ---------- 条件收益表: 未确认的日子里策略在赚还是亏 ----------
    lines.append("\n## 条件收益: 按 T-1 信号状态分组的策略日收益\n")
    lines.append("| 信号 | 状态 | 天数 | 日均收益(bp) | 年化(日均x244) | 日胜率 | 累计贡献 |")
    lines.append("|---|---|---|---|---|---|---|")
    sig_defs = [("中证1000>MA200", gate_ma(close1000)),
                ("12月动量>0 (月度)", gate_mom12_monthly(close1000)),
                ("52周高点10%以内", gate_high252(close1000)),
                ("策略净值>自身MA200", gate_strat)]
    for name, gate in sig_defs:
        g = gate.shift(1).reindex(idx).ffill().fillna(0.0) if "策略净值" not in name else \
            gate.shift(1).reindex(idx).ffill().fillna(0.0)
        for state, tag in [(1.0, "确认(做多)"), (0.0, "未确认(不做多)")]:
            mask = g == state
            if mask.sum() == 0:
                continue
            sub = strat_ret[mask]
            cum = (1 + sub).prod() - 1
            lines.append(f"| {name} | {tag} | {mask.sum()} | {sub.mean()*1e4:.1f} | "
                         f"{fmt(sub.mean()*244)} | {(sub>0).mean()*100:.0f}% | {fmt(cum)} |")
            print(f"[cond] {name} {tag}: {mask.sum()}天 均值{sub.mean()*1e4:.1f}bp "
                  f"胜率{(sub>0).mean()*100:.0f}% 累计{cum*100:.0f}%")

    # 年度对照: baseline vs 三个代表
    lines.append("\n## 年度对照\n")
    keys = ["baseline 满仓", "确认: 中证1000>MA200 才做多",
            "确认: 12月动量>0 才做多 (月度)", "确认: 52周高点10%以内才做多"]
    short = ["满仓", "MA200门", "动量门", "高点门"]
    lines.append("| 年份 | " + " | ".join(short) + " |")
    lines.append("|" + "---|" * (len(keys) + 1))
    for y in sorted(set(idx.year)):
        row = [str(y)]
        for k in keys:
            nav = results[k][0]
            seg = nav[nav.index.year == y]
            row.append(fmt(seg.iloc[-1] / seg.iloc[0] - 1) if len(seg) > 20 else "-")
        lines.append("| " + " | ".join(row) + " |")

    # 网格锚点敏感性注记 (主路径 vs 热身路径, 2019 起切片)
    ret_w = r_w["nav"].pct_change().fillna(0)
    ret_w = ret_w[ret_w.index >= START]
    s_w = stats((1 + ret_w).cumprod())
    s_m = results["baseline 满仓"][2]
    lines.append(f"\n## 注记: 调仓网格锚点敏感性\n")
    lines.append(f"- 同一策略同一参数, 仅 20 日调仓网格锚点不同 (2019-01 vs 2017-01): "
                 f"年化 {fmt(s_m['ann'])} vs {fmt(s_w['ann'])}, 夏普 {s_m['sharpe']:.2f} vs {s_w['sharpe']:.2f}, "
                 f"回撤 {fmt(s_m['dd'])} vs {fmt(s_w['dd'])}")
    lines.append(f"- 结论: 策略点估计存在 ±数个点的网格路径不确定性, 印证\"多锚点平均\"加固项的必要性 (路线图 P2)")

    out = os.path.join(REPO, "reports", "confirm_gate_test.md")
    with open(out, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n✅ 报告: {out}")


if __name__ == "__main__":
    main()

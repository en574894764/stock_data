#!/usr/bin/env python3
"""ETF 动量轮动回测 (B 路线正规形态: 少数标的·ETF 池·月度轮动)
====================================================================================
承接 alpha_vs_timing.md 的"核心+卫星"结论: 卫星层 = 宽基/行业/跨境/黄金 ETF 池动量轮动

策略:
  - 池: 高流动性 ETF ~16 只 (宽基×6 行业×6 跨境×3 黄金×1), 上市满 120 交易日后进入
  - 信号: 过去 N 日收益率 (动量), N 默认 120
  - 调仓: 每 20 交易日, 持有 Top K (默认 3) 等权
  - 变体: --abs-mom 加绝对动量过滤 (Top1 动量<=0 → 全仓货币 ETF 511990)
  - 成本: 单边 0.1% (ETF 免印花税, 佣金+点差)

对照: 510300 买入持有 / 池等权 / 511990 货币

用法:
  python3 scripts/etf_rotation.py                       # 默认 120 日动量 Top3
  python3 scripts/etf_rotation.py --mom 60 --top 5      # 参数扫描
  python3 scripts/etf_rotation.py --abs-mom             # 双动量版
输出: reports/etf_rotation.md (多组参数追加对照表)
"""
import argparse
import os
import sys

import numpy as np
import pandas as pd
import psycopg2

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

COST = 0.001          # 单边 10bp (ETF 免印花税)
REBAL = 20            # 月度
CASH = "511990"       # 货币 ETF (华宝添益)

# 池: 宽基 + 行业 + 跨境 + 黄金 (上市日期见 meta/etf_basic.csv)
POOL = {
    "510050": "上证50", "510300": "沪深300", "510500": "中证500", "512100": "中证1000",
    "159915": "创业板", "159949": "创业板50",
    "512880": "证券", "512660": "军工", "512010": "医药", "510630": "消费",
    "512690": "酒", "512480": "半导体",
    "513100": "纳指100", "513500": "标普500", "159920": "恒生", "513050": "中概互联",
    "518880": "黄金",
}
MIN_HISTORY = 120     # 上市满 120 交易日才入池


def get_conn():
    return psycopg2.connect(host="/tmp", dbname="investassist", user="james")


def load_etf_returns(conn, codes, start, end) -> pd.DataFrame:
    codes = tuple(codes)
    ph = ",".join(["%s"] * len(codes))
    df = pd.read_sql(f"SELECT code, trade_date, pct_chg FROM etf_quote "
                     f"WHERE code IN ({ph}) AND trade_date >= %s AND trade_date <= %s",
                     conn, params=codes + (start, end))
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df["pct_chg"] = pd.to_numeric(df["pct_chg"], errors="coerce") / 100.0
    wide = df.pivot(index="trade_date", columns="code", values="pct_chg").sort_index()
    return wide


def load_list_dates(conn) -> dict:
    """各 ETF 首个交易日"""
    codes = tuple(POOL)
    ph = ",".join(["%s"] * len(codes))
    df = pd.read_sql(f"SELECT code, MIN(trade_date) FROM etf_quote WHERE code IN ({ph}) "
                     "GROUP BY code", conn, params=codes)
    return {r[0]: pd.Timestamp(r[1]) for r in df.itertuples(index=False)}


def run(wide: pd.DataFrame, list_dates: dict, mom: int, top: int, abs_mom: bool,
        start="2017-01-01", reverse=False) -> dict:
    idx = wide.index
    rebal = [d for d in idx if d >= pd.Timestamp(start)][::REBAL]
    cum = (1 + wide.fillna(0)).cumprod()
    nav, dates = [1.0], [rebal[0]]
    holdings_hist = []
    prev_sel, prev_w = [], {}
    cash_on = 0
    for t in rebal[1:]:
        # 当日可用池: 上市满 MIN_HISTORY 且有动量数据
        avail = [c for c in wide.columns
                 if list_dates.get(c) is not None and t > list_dates[c] + pd.Timedelta(days=MIN_HISTORY)
                 and pd.notna(cum[c].loc[t]) and t in wide.index]
        if len(avail) < top + 2:
            continue
        momv = {c: cum[c].loc[t] / cum[c].shift(mom).loc[t] - 1 if pd.notna(cum[c].shift(mom).loc[t]) else -9
                for c in avail}
        sgn = -1.0 if reverse else 1.0
        ranked = sorted(momv.items(), key=lambda x: -sgn * x[1])
        sel = [c for c, _ in ranked[:top]]
        if abs_mom and ranked and ranked[0][1] <= 0:
            sel = [CASH] if CASH in wide.columns else []
            cash_on += 1
        pos_dates = [d for d in idx if d > t][:REBAL]
        if not pos_dates:
            break
        w = {c: 1.0 / len(sel) for c in sel} if sel else {}
        turnover = 0.5 * sum(abs(w.get(c, 0) - prev_w.get(c, 0)) for c in set(w) | set(prev_w))
        sub = wide.loc[pos_dates, sel] if sel else pd.DataFrame(0.0, index=pos_dates, columns=[])
        pr = sub.mean(axis=1).fillna(0).to_numpy()
        if len(pr):
            pr[0] -= turnover * 2 * COST
        for d, r in zip(pos_dates, pr):
            nav.append(nav[-1] * (1 + r))
            dates.append(d)
        holdings_hist.append({"date": t, "sel": sel, "names": [POOL.get(c, c) for c in sel],
                              "turnover": turnover})
        prev_w = w
    nav_s = pd.Series(nav, index=pd.DatetimeIndex(dates)).groupby(level=0).last().sort_index()
    return {"nav": nav_s, "turnover": np.mean([h["turnover"] for h in holdings_hist]) if holdings_hist else 0,
            "holdings": holdings_hist, "cash_periods": cash_on}


def stats(nav: pd.Series) -> dict:
    if len(nav) < 252:
        return {}
    ret = nav.pct_change().dropna()
    years = len(ret) / 244
    ann = nav.iloc[-1] / nav.iloc[0] ** (1 / years) if False else (nav.iloc[-1] / nav.iloc[0]) ** (1 / years) - 1
    vol = ret.std() * np.sqrt(244)
    dd = (nav / nav.cummax() - 1).min()
    return {"ann": ann, "vol": vol, "sharpe": ann / vol if vol > 0 else np.nan, "dd": dd,
            "total": nav.iloc[-1] / nav.iloc[0] - 1}


def fmt(v):
    return "-" if v is None or (isinstance(v, float) and not np.isfinite(v)) else f"{v*100:.1f}%"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2017-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--mom", type=int, action="append", default=None, help="动量窗口, 可重复")
    ap.add_argument("--top", type=int, action="append", default=None, help="持仓数, 可重复")
    ap.add_argument("--abs-mom", action="store_true")
    ap.add_argument("--reverse", action="store_true", help="反转变体: 买跌得最多的 (A 股反转效应)")
    args = ap.parse_args()
    moms = args.mom or [60, 120, 252]
    tops = args.top or [3]

    conn = get_conn()
    end = args.end or pd.Timestamp.today().strftime("%Y-%m-%d")
    load_start = (pd.Timestamp(args.start) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
    wide = load_etf_returns(conn, list(POOL) + [CASH, "510880"], load_start, end)
    list_dates = load_list_dates(conn)
    conn.close()
    print(f"ETF 矩阵: {wide.shape[0]} 天 × {wide.shape[1]} 只 ({wide.index[0].date()} ~ {wide.index[-1].date()})")

    # 对照
    bench = {}
    for c in ["510300", "511990"]:
        if c in wide.columns:
            seg = wide[c].loc[wide.index >= pd.Timestamp(args.start)]
            bench[POOL.get(c, "货币")] = (1 + seg.fillna(0)).cumprod()
    avail_eq = [c for c in wide.columns if c in POOL and wide[c].loc[wide.index >= pd.Timestamp(args.start)].notna().mean() > 0.9]
    eq = (1 + wide[avail_eq].loc[wide.index >= pd.Timestamp(args.start)].fillna(0).mean(axis=1)).cumprod()

    lines = [f"# ETF 动量轮动回测 ({args.start} ~ {end})\n",
             f"- 池: {len(POOL)} 只 ({', '.join(POOL.values())})",
             f"- 调仓: {REBAL} 交易日 | 成本: 单边 {COST*100:.0f}bp | 入池条件: 上市满 {MIN_HISTORY} 日",
             f"- 绝对动量过滤: {'开 (Top1 动量<=0 → 货币 ' + CASH + ')' if args.abs_mom else '关'}\n",
             "| 策略 | 年化 | 波动 | 夏普 | 最大回撤 | 平均换手 |", "|---|---|---|---|---|---|"]

    results = {}
    for mom in moms:
        for top in tops:
            r = run(wide, list_dates, mom, top, args.abs_mom, args.start, args.reverse)
            s = stats(r["nav"])
            if not s:
                continue
            label = f"{'反转' if args.reverse else '动量'}{mom}日 Top{top}"
            results[label] = r
            lines.append(f"| {label} | {fmt(s['ann'])} | {fmt(s['vol'])} | {s['sharpe']:.2f} | "
                         f"{fmt(s['dd'])} | {fmt(r['turnover'])} |")
            print(f"[{label}] 年化 {fmt(s['ann'])} 夏普 {s['sharpe']:.2f} 回撤 {fmt(s['dd'])} "
                  f"换手 {fmt(r['turnover'])} 货币期 {r['cash_periods']}")
    for name, nav in bench.items():
        s = stats(nav)
        lines.append(f"| {name}买入持有 | {fmt(s['ann'])} | {fmt(s['vol'])} | {s['sharpe']:.2f} | {fmt(s['dd'])} | 0 |")
        print(f"[{name}持有] 年化 {fmt(s['ann'])} 夏普 {s['sharpe']:.2f} 回撤 {fmt(s['dd'])}")
    s = stats(eq)
    lines.append(f"| 池等权 | {fmt(s['ann'])} | {fmt(s['vol'])} | {s['sharpe']:.2f} | {fmt(s['dd'])} | 0 |")
    print(f"[池等权] 年化 {fmt(s['ann'])} 夏普 {s['sharpe']:.2f} 回撤 {fmt(s['dd'])}")

    # 最优参数的年度分解 + 近期持仓
    best = max(results.items(), key=lambda kv: stats(kv[1]["nav"]).get("sharpe", -9)) if results else None
    if best:
        label, r = best
        lines.append(f"\n## 最优参数 {label}: 年度分解\n")
        lines.append("| 年份 | 年化 | 最大回撤 |")
        lines.append("|---|---|---|")
        nav = r["nav"]
        ret = nav.pct_change().dropna()
        for y, g in ret.groupby(ret.index.year):
            seg = nav[nav.index.year == y]
            dd = (seg / seg.cummax() - 1).min()
            ann = (seg.iloc[-1] / seg.iloc[0]) ** (244 / len(g)) - 1 if len(g) > 20 else np.nan
            lines.append(f"| {y} | {fmt(ann)} | {fmt(dd)} |")
        lines.append("\n## 最近 6 期持仓\n")
        lines.append("| 调仓日 | 持仓 | 换手 |")
        lines.append("|---|---|---|")
        for h in r["holdings"][-6:]:
            lines.append(f"| {h['date'].date()} | {', '.join(h['names'])} | {fmt(h['turnover'])} |")
        # 与 300 的相关性 (日收益)
        common = nav.index.intersection(bench.get("沪深300", pd.Series(dtype=float)).index)
        if len(common) > 100:
            corr = nav.pct_change().loc[common].corr(bench["沪深300"].pct_change().loc[common])
            lines.append(f"\n与沪深300日收益相关: {corr:.2f}")

    out = os.path.join(REPO, "reports", "etf_rotation.md")
    with open(out, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n✅ 报告: {out}")


if __name__ == "__main__":
    main()

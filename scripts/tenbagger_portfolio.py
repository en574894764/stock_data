#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
十倍股组合构建器（高精度，多因子打分 + 约束 + 权重）

与扫描器（召回优先）互补：本脚本在全市场打分，取 Top20 形成持仓方案。
因子体系（截面 z-score, ±3 winsorize）：
  成长: g_npy(0.20) 净利增速 / g_rev(0.10) 营收增速 / accel(0.12) 增速加速度
  质量: roe(0.10) / gm(0.05) 毛利率
  趋势: mom12(0.14) 12月动量 / dist_high(0.10) 距52周高点
  估值容忍: peg(0.09) = -(PE/净利增速) 越低越好 —— 不是选便宜, 是防"贵得没道理"
  市值: size(0.05) 小市值倾斜
  事件加成: 近90天净利润断层 +0.5 (raw)
约束: 非ST / 上市>90天 / 市值30-2000亿 / 流动性(3月均额>5000万) / 单行业<=4只
权重: 正分数比例加权, 单票 cap 10% (溢出按比例再分配, 迭代3轮)

用法:
  python3 scripts/tenbagger_portfolio.py              # 输出今日持仓方案
  python3 scripts/tenbagger_portfolio.py --backtest   # 半年度再平衡历史回测 2016-12~2026-09
"""
import argparse
import json
import sys

import numpy as np
import pandas as pd
import psycopg2

sys.path.insert(0, "/Users/james/workspace/stock_data")
from scripts.daily_tenbagger_scanner import load_bulk, snapshot, latest_trade_date
from scripts.factor_eval import load_daily_returns, get_conn

W = dict(g_npy=0.16, g_rev=0.10, accel=0.12, roe=0.12, gm=0.05,
         mom12=0.12, dist_high=0.10, peg=0.12, size=0.06)
GAP_BONUS = 0.5
TOP_N, IND_CAP, W_CAP = 20, 4, 0.10
COST = 0.003  # 每期双边成本

# 市场状态层（已验证有效: 全市场 PB 分位对未来 1 年有预测力; 250 日线为趋势锚）
# 仓位 = pb档 × 趋势档，两档独立取乘积
def load_market_regime(conn):
    """全市场 PB 中位数的 5 年滚动分位 + 沪深300 的 250 日趋势。"""
    pb = pd.read_sql("""
        SELECT trade_date, percentile_cont(0.5) WITHIN GROUP (ORDER BY pb) AS pb_med
        FROM daily_basic WHERE pb > 0 AND trade_date >= '2011-01-01'
        GROUP BY trade_date ORDER BY trade_date
    """, conn)
    pb["trade_date"] = pd.to_datetime(pb["trade_date"])
    pb = pb.set_index("trade_date")["pb_med"].astype(float)
    # 5 年(1250 交易日)滚动分位
    pct = pb.rolling(1250, min_periods=500).apply(
        lambda x: (x.iloc[-1] > x).mean(), raw=False)
    idx = pd.read_sql(
        "SELECT trade_date, pct_chg FROM index_daily WHERE symbol='000300.SH' AND trade_date >= '2011-01-01' ORDER BY trade_date", conn)
    idx["trade_date"] = pd.to_datetime(idx["trade_date"])
    nav = (1 + idx.set_index("trade_date")["pct_chg"] / 100.0).cumprod()
    above_ma = nav > nav.rolling(250).mean()
    reg = pd.DataFrame({"pb_pct": pct, "above_ma": above_ma}).dropna()
    # 仓位规则（宽阈值、低频，避免线性规则反噬）
    pb_pos = pd.Series(1.0, index=reg.index)
    pb_pos[reg["pb_pct"] > 0.85] = 0.4
    pb_pos[(reg["pb_pct"] > 0.70) & (reg["pb_pct"] <= 0.85)] = 0.7
    tr_pos = pd.Series(1.0, index=reg.index)
    tr_pos[~reg["above_ma"]] = 0.5
    reg["pos"] = (pb_pos * tr_pos).clip(0.2, 1.0)
    return reg


def z(s):
    s = s.astype(float)
    sd = s.std()
    if sd == 0 or pd.isna(sd):
        return s * 0
    return ((s - s.mean()) / sd).clip(-3, 3)


def score_universe(df):
    """全市场打分。返回打分后的 df（含各因子与总分）。"""
    d = df.copy()
    d = d[(~d["name"].str.contains("ST", na=False)) &
          (d["list_age_y"] > 0.25) & d["mv_yi"].between(30, 2000) &
          (d["amt_wan"] >= 5000) & d["netprofit_yoy"].notna() & d["roe"].notna()]
    if len(d) < 50:
        return d
    npy = d["netprofit_yoy"].clip(-100, 300)
    ory = d["or_yoy"].clip(-50, 150)
    accel = (d["netprofit_yoy"] - d["prev_npy"]).clip(-150, 150)
    roe = d["roe"].clip(-10, 40)
    gm = d["grossprofit_margin"].clip(0, 90)
    mom = d["ret12m"].clip(-0.6, 3.0)
    dh = d["dist_high"].clip(0.3, 1.05)
    pe_c = d["pe_ttm"].clip(0, 300)
    peg = -(pe_c / d["netprofit_yoy"].clip(5, 300))  # 越小越好
    peg = peg.where(pe_c.notna() & (d["netprofit_yoy"] > 0))
    size = -np.log(d["mv_yi"])

    sc = (W["g_npy"] * z(npy) + W["g_rev"] * z(ory) + W["accel"] * z(accel) +
          W["roe"] * z(roe) + W["gm"] * z(gm) + W["mom12"] * z(mom) +
          W["dist_high"] * z(dh) + W["peg"] * z(peg.fillna(peg.median())) +
          W["size"] * z(size))
    d["score"] = sc + GAP_BONUS * d["gap90"].astype(float)
    for name, s in [("f_npy", npy), ("f_rev", ory), ("f_accel", accel), ("f_roe", roe),
                    ("f_mom", mom), ("f_dh", dh), ("f_peg", peg), ("f_size", size)]:
        d[name] = s
    return d


def select_portfolio(d, top_n=TOP_N, ind_cap=IND_CAP):
    """行业约束下的 Top-N。返回 (codes, weights Series)。"""
    d = d.sort_values("score", ascending=False)
    picks, ind_cnt = [], {}
    for code, r in d.iterrows():
        if len(picks) >= top_n:
            break
        ind = r["industry"] or "未知"
        if ind_cnt.get(ind, 0) >= ind_cap:
            continue
        picks.append(code)
        ind_cnt[ind] = ind_cnt.get(ind, 0) + 1
    sel = d.loc[picks]
    w = sel["score"].clip(lower=0)
    w = w / w.sum() if w.sum() > 0 else pd.Series(1.0 / len(sel), index=sel.index)
    for _ in range(3):  # cap 10% 迭代再分配
        over = w > W_CAP
        if not over.any():
            break
        excess = (w[over] - W_CAP).sum()
        w[over] = W_CAP
        under = ~over
        w[under] += excess * w[under] / w[under].sum()
    return picks, w, sel


def backtest(bulk, conn):
    daily_ret = load_daily_returns(conn, "2016-11-01", "2026-09-11")
    bench = pd.read_sql(
        "SELECT trade_date, pct_chg FROM index_daily WHERE symbol='000300.SH' AND trade_date >= '2016-11-01' ORDER BY trade_date",
        conn)
    bench["trade_date"] = pd.to_datetime(bench["trade_date"])
    bench_ret = bench.set_index("trade_date")["pct_chg"] / 100.0
    regime = load_market_regime(conn)

    # 季度再平衡日: 每年 3/6/9/12 月末前最后交易日
    year_ends = []
    for y in range(2016, 2027):
        for md in ["03-31", "06-30", "09-30", "12-31"]:
            t = pd.Timestamp(f"{y}-{md}")
            if t < pd.Timestamp("2016-12-01") or t > pd.Timestamp("2026-06-30"):
                continue
            cands = daily_ret.index[daily_ret.index <= t]
            if len(cands):
                year_ends.append(cands[-1])
    year_ends = sorted(set(year_ends))

    rets, rets_b, holdings_log = [], [], []
    tb20_held = set()
    TB20C = ["300308.SZ", "300502.SZ", "300394.SZ", "002463.SZ", "300476.SZ", "002916.SZ",
             "603986.SH", "300661.SZ", "002371.SZ", "603501.SH", "600809.SH", "000975.SZ",
             "601899.SH", "002714.SZ", "300548.SZ", "601100.SH", "000733.SZ", "603067.SH", "603738.SH"]
    for i, t in enumerate(year_ends[:-1]):
        d = score_universe(snapshot(bulk, conn, str(t.date())))
        if len(d) < 50:
            print(f"  {t.date()}: 快照不足, 跳过")
            continue
        picks, w, sel = select_portfolio(d)
        tb20_held |= set(picks) & set(TB20C)
        nxt = year_ends[i + 1]
        pos = daily_ret.index[(daily_ret.index > t) & (daily_ret.index <= nxt)]
        if len(pos) == 0:
            continue
        m = daily_ret.loc[pos, daily_ret.columns.intersection(picks)].fillna(0.0)
        wv = w.reindex(m.columns).fillna(0.0)
        port = (m * wv).sum(axis=1)
        # 市场状态仓位（每日查 regime，缺省沿用前值）
        p_reg = regime["pos"].reindex(regime.index[regime.index <= pos[-1]])
        daily_pos = p_reg.reindex(p_reg.index.union(pos)).ffill().reindex(pos).fillna(1.0)
        port = port * daily_pos.values
        port.iloc[0] = port.iloc[0] - COST
        rets.append(port)
        rets_b.append(bench_ret.loc[pos].fillna(0.0))
        avg_pos = float(daily_pos.mean())
        holdings_log.append(dict(date=str(t.date()), n=len(picks), pos=round(avg_pos, 2),
                                 top5=[f"{sel.loc[c,'name']}" for c in picks[:5]]))
        print(f"  {t.date()}: 持有 {len(picks)} 只 仓位~{avg_pos:.0%} -> {nxt.date()} 期收益 {(1+port).prod()-1:+.1%} "
              f"(基准 {(1+bench_ret.loc[pos].fillna(0)).prod()-1:+.1%})", flush=True)

    port = pd.concat(rets).sort_index()
    br = pd.concat(rets_b).sort_index()
    navp = (1 + port).cumprod()
    navb = (1 + br).cumprod()
    yrs = len(port) / 244
    ann = navp.iloc[-1] ** (1 / yrs) - 1
    ann_b = navb.iloc[-1] ** (1 / yrs) - 1
    sharpe = port.mean() / port.std() * np.sqrt(244)
    sharpe_b = br.mean() / br.std() * np.sqrt(244)
    dd = (navp / navp.cummax() - 1).min()
    dd_b = (navb / navb.cummax() - 1).min()
    yearly_p = (1 + port).groupby(port.index.year).prod() - 1
    yearly_b = (1 + br).groupby(br.index.year).prod() - 1
    result = dict(
        window=[str(port.index[0].date()), str(port.index[-1].date())],
        cagr=round(float(ann), 4), sharpe=round(float(sharpe), 2), max_dd=round(float(dd), 4),
        calmar=round(float(ann / abs(dd)), 2),
        bench=dict(cagr=round(float(ann_b), 4), sharpe=round(float(sharpe_b), 2), max_dd=round(float(dd_b), 4)),
        yearly={str(y): dict(port=round(float(yearly_p[y]), 4), bench=round(float(yearly_b.get(y, 0)), 4))
                for y in yearly_p.index},
        excess_win_years=int((yearly_p > yearly_b.reindex(yearly_p.index).fillna(-9)).sum()),
        n_years=len(yearly_p),
        tb20_ever_held=sorted(tb20_held),
        holdings_log=holdings_log,
    )
    json.dump(result, open("outputs/tenbagger_portfolio_backtest.json", "w"), ensure_ascii=False, indent=1)
    print("\n=== 组合回测 (半年度再平衡, 成本0.3%/期) ===")
    print(f"窗口 {result['window'][0]} ~ {result['window'][1]} ({yrs:.1f} 年)")
    print(f"组合: CAGR {ann:.1%} | 夏普 {sharpe:.2f} | 最大回撤 {dd:.1%} | 卡玛 {ann/abs(dd):.2f}")
    print(f"基准: CAGR {ann_b:.1%} | 夏普 {sharpe_b:.2f} | 最大回撤 {dd_b:.1%}")
    print(f"年度超额胜率: {result['excess_win_years']}/{result['n_years']}")
    print(f"曾持有过十倍股: {len(tb20_held)} 只")
    print("逐年:", json.dumps(result["yearly"], ensure_ascii=False))
    print("-> outputs/tenbagger_portfolio_backtest.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--date", default=None)
    args = ap.parse_args()

    conn = get_conn()
    bulk = load_bulk(conn)

    if args.backtest:
        backtest(bulk, conn)
        return

    asof = args.date or latest_trade_date(conn)
    print(f"组合构建日期: {asof}")
    d = score_universe(snapshot(bulk, conn, asof))
    print(f"可投股票池: {len(d)} 只")
    picks, w, sel = select_portfolio(d)
    out = sel[["name", "industry", "score", "mv_yi", "pe_ttm", "netprofit_yoy", "or_yoy",
               "roe", "grossprofit_margin", "ret12m", "dist_high", "gap90"]].copy()
    out.insert(0, "weight", (w * 100).round(1))
    out["score"] = out["score"].round(2)
    for c in ["mv_yi", "pe_ttm", "netprofit_yoy", "or_yoy", "roe", "grossprofit_margin"]:
        out[c] = out[c].round(1)
    out["ret12m"] = (out["ret12m"] * 100).round(0)
    out["dist_high"] = out["dist_high"].round(2)
    out.index.name = "ts_code"
    out.to_csv("outputs/tenbagger_portfolio_latest.csv", encoding="utf-8-sig")
    print("\n=== 今日持仓方案 (Top 20, 权重%) ===")
    print(out.to_string())
    print("\n-> outputs/tenbagger_portfolio_latest.csv")


if __name__ == "__main__":
    main()

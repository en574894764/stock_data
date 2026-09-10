#!/usr/bin/env python3
"""个股级趋势过滤测试: "先过滤下跌趋势股票, 再做组合" vs "只做上涨趋势股票"
====================================================================================
James 的两个方案 (择时三轮否决后的第四种形态: 不动仓位, 动股票池):
  方案一 (防守式): 剔除"明确下跌趋势"的股票, 其余正常做组合
  方案二 (进攻式): 只保留"上涨趋势"的股票 (非上涨不做多, 标的级)
  beta 论点: 下跌中的股票 beta 糟糕, 选得再好也没用 → 用数据检验

过滤发生在 score_cross 打分之前 (严格"先过滤→再打分→再选股"次序)。
趋势信号 (T 日收盘已知, 与因子同时刻, 无前视; 价格用收益矩阵重构的复权价):
  ma60 / ma120 : 收盘 > 自身 MA60 / MA120
  ret120       : 过去 120 日收益 > 0
  deep60/deep120: 收盘 < 0.9×MA60 / 0.9×MA120 (明确下跌 = 深度跌破均线)

方案一保留信号缺失的票 (只剔"明确"下跌); 方案二严格剔除缺失票 (非上涨不做多)。

诊断表 (beta 论点直接检验): 每期被过滤踢出 Top30 的票 vs 留下的票, 后 20 日实际收益对比。

用法: python3 scripts/trend_filter_test.py
输出: reports/trend_filter_test.md
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
LOOKBACK = "2018-01-01"   # 收益矩阵起点 (MA120 需要 120 根 K 线预热)
TOPN = 30
DEEP = 0.9


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


# ---------- 趋势信号矩阵 (收益矩阵 → 复权价指数 → MA/动量) ----------

def build_trend_mats(daily_ret: pd.DataFrame):
    px = (1 + daily_ret.fillna(0)).cumprod()
    return {
        "px": px,
        "ma60": px.rolling(60).mean(),
        "ma120": px.rolling(120).mean(),
        "ret120": px / px.shift(120) - 1,
    }


def make_allowed_fn(mats, kind):
    """返回 (date → allowed set) 或 None (不过滤)。keep_na: 信号缺失时是否保留"""
    px, ma60, ma120, ret120 = mats["px"], mats["ma60"], mats["ma120"], mats["ret120"]

    def allowed(t):
        if t not in px.index:
            return None
        if kind == "drop_deep60":          # 方案一: 剔除收盘 < 0.9×MA60 (明确下跌), 缺失保留
            m = px.loc[t] < DEEP * ma60.loc[t]
            return set(px.columns[~(m.fillna(False))])
        if kind == "drop_deep120":         # 方案一: 剔除收盘 < 0.9×MA120
            m = px.loc[t] < DEEP * ma120.loc[t]
            return set(px.columns[~(m.fillna(False))])
        if kind == "only_above_ma60":      # 方案二: 只做收盘 > MA60 的票, 缺失剔除 (非上涨不做多)
            m = (px.loc[t] > ma60.loc[t]).fillna(False)
            return set(px.columns[m])
        if kind == "only_above_ma120":     # 方案二: 只做收盘 > MA120 的票
            m = (px.loc[t] > ma120.loc[t]).fillna(False)
            return set(px.columns[m])
        if kind == "only_ret120_pos":      # 方案二: 只做 120 日收益 > 0 的票
            m = (ret120.loc[t] > 0).fillna(False)
            return set(px.columns[m])
        return None

    return allowed


def make_patched_score(allowed_fn, min_cross=50):
    """复刻 strategy_lib.score_cross, 在逐因子过滤时叠加 allowed 集 (先过滤后打分)。
    副作用记录: n_before/n_after/被踢票与留下票的满分序列 (供诊断)"""
    log = {"n_before": [], "n_after": [], "scores": {}}

    def patched(fwides, weights, t, uni, list_dates, _min_cross=50):
        allow = allowed_fn(t) if allowed_fn else None
        zs = {}
        for name, w in weights.items():
            fw = fwides[name]
            row = fw.loc[t].dropna() if t in fw.index else pd.Series(dtype=float)
            keep = [c for c in row.index if c in uni and list_dates.get(c) is not None
                    and t > pd.Timestamp(list_dates[c]) + pd.Timedelta(days=120)
                    and (allow is None or c in allow)]
            row = row[keep]
            if len(row) < min_cross:
                log["n_before"].append(0)
                return None, None
            sd = row.std()
            zs[name] = ((row - row.mean()) / (sd if sd and sd > 0 else 1.0)) * w
        zdf = pd.DataFrame(zs)
        score = zdf.sum(axis=1, min_count=len(zs) // 2)
        log["n_before"].append(len(zdf))
        log["n_after"].append(len(score.dropna()))
        log["scores"][t] = score
        return score, zdf

    return patched, log


def fwd20(daily_ret, t, codes):
    """t 之后 20 个交易日的等权平均累计收益 (codes 集合)"""
    idx = daily_ret.index
    pos = idx.get_indexer([t])
    if len(pos) == 0 or pos[0] < 0:
        return np.nan
    seg = daily_ret.iloc[pos[0] + 1: pos[0] + 1 + fe.REBAL]
    cols = [c for c in codes if c in seg.columns]
    if not cols or len(seg) == 0:
        return np.nan
    per_stock = (1 + seg[cols]).prod() - 1
    return float(per_stock.mean())


def main():
    end = pd.Timestamp.today().strftime("%Y-%m-%d")
    conn = fe.get_conn()
    uni, list_dates = fe.load_universe_filter(conn)
    st = sl.load_strategies(conn, [f"prod_6f_eq@topn={TOPN}"])[0]
    factors = {n: sl.load_factor(conn, n, START, end) for n in st["cfg"]["factors"]}
    daily_ret = fe.load_daily_returns(conn, LOOKBACK, end)
    rebal = fe.rebalance_dates(daily_ret.index, START, end)
    conn.close()
    mats = build_trend_mats(daily_ret)
    orig_score = sl.score_cross

    variants = [
        ("baseline 不过滤", None),
        ("方案一: 剔除 收盘<0.9×MA60 (明确下跌)", "drop_deep60"),
        ("方案一: 剔除 收盘<0.9×MA120", "drop_deep120"),
        ("方案二: 只做 收盘>MA60 的票", "only_above_ma60"),
        ("方案二: 只做 收盘>MA120 的票", "only_above_ma120"),
        ("方案二: 只做 120日收益>0 的票", "only_ret120_pos"),
    ]

    lines = [f"# 个股级趋势过滤测试 (TopN{TOPN}, {START} ~ {end})\n",
             "- 过滤发生在截面打分之前 (先过滤→再打分→再选股); 趋势信号 T 日收盘已知, 与因子同时刻, 无前视",
             "- 价格 = 收益矩阵重构的复权价指数 (用于自身均线/动量, 不依赖绝对价格)",
             "- 方案一保留信号缺失的票 (只剔\"明确\"下跌); 方案二严格 (非上涨不做多, 缺失即剔除)\n",
             "| 方案 | 年化 | 夏普 | 最大回撤 | 平均截面数 | 每期被踢出Top30 |", "|---|---|---|---|---|---|"]

    # baseline 先跑, 记录每期满分与 Top30 (供诊断对比)
    results, logs = {}, {}
    for label, kind in variants:
        allowed_fn = make_allowed_fn(mats, kind) if kind else None
        patched, log = make_patched_score(allowed_fn)
        sl.score_cross = patched
        r = replay(st, factors, daily_ret, uni, list_dates, rebal)
        sl.score_cross = orig_score
        results[label] = r
        logs[label] = log
        s = stats(r["nav"])
        n_after = np.mean([x for x in log["n_after"]]) if log["n_after"] else np.nan
        lines.append(f"| {label} | {fmt(s['ann'])} | {s['sharpe']:.2f} | {fmt(s['dd'])} | "
                     f"{n_after:.0f} | {'—' if kind is None else ''} |")
        print(f"[{label}] 年化 {fmt(s['ann'])} 夏普 {s['sharpe']:.2f} 回撤 {fmt(s['dd'])} "
              f"截面 {n_after:.0f}")

    # ---------- 诊断: 每期被踢出 Top30 的票 vs 留下的票, 后 20 日实际收益 ----------
    base_scores = logs["baseline 不过滤"]["scores"]
    lines.append("\n## 诊断: 被过滤踢掉的票, 后 20 日到底跑赢还是跑输 (beta 论点检验)\n")
    lines.append("| 方案 | 期数 | 被踢票均收益 | 留下票均收益 | 被踢-留下(bp/期) | 被踢票跑赢期占比 |")
    lines.append("|---|---|---|---|---|---|")
    diag = {}
    for label, kind in variants:
        if kind is None:
            continue
        scores = logs[label]["scores"]
        pairs, n_periods = [], 0
        for t, fscore in scores.items():
            if t not in base_scores:
                continue
            sel0, _ = sl.select_stocks(base_scores[t].dropna(), TOPN)
            sel_f, _ = sl.select_stocks(fscore.dropna(), TOPN)
            kicked = [c for c in sel0 if c not in sel_f]
            if not kicked:
                continue
            n_periods += 1
            k, f = fwd20(daily_ret, t, kicked), fwd20(daily_ret, t, sel_f)
            if np.isfinite(k) and np.isfinite(f):
                pairs.append((k, f))
        if not pairs:
            continue
        kicked_r = np.array([p[0] for p in pairs])
        kept_r = np.array([p[1] for p in pairs])
        win = float((kicked_r > kept_r).mean())
        diag[label] = (kicked_r, kept_r)
        lines.append(f"| {label} | {n_periods} | {fmt(kicked_r.mean())} | {fmt(kept_r.mean())} | "
                     f"{(kicked_r.mean()-kept_r.mean())*1e4:.0f} | {win*100:.0f}% |")
        print(f"[diag] {label}: 被踢 {fmt(kicked_r.mean())} vs 留下 {fmt(kept_r.mean())} "
              f"差 {(kicked_r.mean()-kept_r.mean())*1e4:.0f}bp 跑赢率 {win*100:.0f}% ({n_periods}期)")

    # 年度对照: baseline vs 每方案族代表
    lines.append("\n## 年度对照\n")
    keys = ["baseline 不过滤", "方案一: 剔除 收盘<0.9×MA60 (明确下跌)",
            "方案二: 只做 收盘>MA120 的票", "方案二: 只做 120日收益>0 的票"]
    short = ["不过滤", "剔深跌(MA60)", "只做>MA120", "只做动量>0"]
    lines.append("| 年份 | " + " | ".join(short) + " |")
    lines.append("|" + "---|" * (len(keys) + 1))
    idx_nav = results[keys[0]]["nav"].index
    for y in sorted(set(idx_nav.year)):
        row = [str(y)]
        for k in keys:
            nav = results[k]["nav"]
            seg = nav[nav.index.year == y]
            row.append(fmt(seg.iloc[-1] / seg.iloc[0] - 1) if len(seg) > 20 else "-")
        lines.append("| " + " | ".join(row) + " |")

    out = os.path.join(REPO, "reports", "trend_filter_test.md")
    with open(out, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n✅ 报告: {out}")


if __name__ == "__main__":
    main()

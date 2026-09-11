#!/usr/bin/env python3
"""财报质量过滤测试: "反转要赌错杀修复, 垃圾股不是错杀——用财报诊断滤掉垃圾有没有用"
====================================================================================
James 假设: 引擎赌 20 日反转的前提是标的不是垃圾股 (退市/恶化股跌了补不回来);
用 ROE>=12%、资产负债率上限等财报诊断指标预过滤, 是否提升组合?

数据口径 (全部 PIT, T 日已知):
  ep_ttm   : factor_value 日频 (1/pe_ttm, 负值=亏损) — 最新鲜的亏损检测
  ann_roe  : financial_indicator Q1 报 roe×4 年化 (ann_date<=T asof; 表内每股每年仅 Q1 一期)
  debt     : financial_indicator Q1 报 debt_to_assets (asof; 银行/券商结构性 >90%, 不豁免, 诊断里看影响)
  universe : load_universe_filter 已剔当前 ST/退市变体/.BJ

过滤方案:
  方案族A (剔垃圾, 缺失保留): drop_loss(ep_ttm<0) / drop_roe_neg(annROE<0) / drop_debt70(>70%)
  方案族B (质量线, 缺失剔除): drop_roe_8 / drop_roe_12 (James 阈值, 年化)
  组合: drop_junk = 亏损 或 annROE<0 或 负债率>80

诊断: ①baseline Top30 的财报画像 (现在的票有多少垃圾) ②被踢票 vs 留下票后 20 日收益 (beta 论点同款检验)

用法: python3 scripts/junk_filter_test.py
输出: reports/junk_filter_test.md
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
TOPN = 30


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


def load_fin_events(conn):
    """Q1 财报事件表: (ts_code, ann_date, ann_roe, debt) — ann_roe = Q1 roe × 4 年化"""
    ev = pd.read_sql(
        "SELECT ts_code, ann_date, roe, debt_to_assets FROM financial_indicator "
        "WHERE ann_date IS NOT NULL AND report_type='1' "
        "AND (ts_code LIKE '%%.SZ' OR ts_code LIKE '%%.SH')", conn)
    ev["ann_date"] = pd.to_datetime(ev["ann_date"])
    ev["ann_roe"] = pd.to_numeric(ev["roe"]) * 4.0
    ev["debt"] = pd.to_numeric(ev["debt_to_assets"])
    ev = (ev.dropna(subset=["ann_date"])
          .sort_values(["ts_code", "ann_date"])
          .drop_duplicates(["ts_code", "ann_date"], keep="last"))
    return ev[["ts_code", "ann_date", "ann_roe", "debt"]]


def asof_snapshot(ev: pd.DataFrame, t) -> pd.Series:
    """T 日可见的最新 Q1 财报 (ann_date <= T), 返回 ann_roe Series (index=ts_code)"""
    sub = ev[ev["ann_date"] <= t]
    if sub.empty:
        return pd.Series(dtype=float)
    last = sub.drop_duplicates("ts_code", keep="last").set_index("ts_code")
    return last["ann_roe"], last["debt"]


def make_filter_fn(ep_wide, ev, kind):
    def allowed(t):
        ep = ep_wide.loc[t] if t in ep_wide.index else pd.Series(dtype=float)
        try:
            roe, debt = asof_snapshot(ev, t)
        except ValueError:
            snap = asof_snapshot(ev, t)
            roe, debt = snap, snap
        if kind == "drop_loss":
            bad = set(ep[ep.isna() | (ep < 0)].index)   # tushare 亏损股 pe_ttm 无正数 → ep NaN
            return None, bad
        if kind == "drop_roe_neg":
            bad = set(roe[roe < 0].index)
            return None, bad
        if kind == "drop_debt70":
            bad = set(debt[debt > 70].index)
            return None, bad
        if kind in ("drop_roe_8", "drop_roe_12"):
            th = 8.0 if kind == "drop_roe_8" else 12.0
            good = set(roe[roe >= th].index)     # 质量线: 只留达标的 (缺失也剔)
            return good, None
        if kind == "drop_junk":
            bad = set(ep[ep < 0].index) | set(roe[roe < 0].index) | set(debt[debt > 80].index)
            return None, bad
        return None, None

    return allowed


def make_patched_score(filter_fn, min_cross=50):
    log = {"n_after": [], "scores": {}}

    def patched(fwides, weights, t, uni, list_dates, _min_cross=50):
        good, bad = filter_fn(t)
        zs = {}
        for name, w in weights.items():
            fw = fwides[name]
            row = fw.loc[t].dropna() if t in fw.index else pd.Series(dtype=float)
            keep = [c for c in row.index if c in uni and list_dates.get(c) is not None
                    and t > pd.Timestamp(list_dates[c]) + pd.Timedelta(days=120)
                    and (good is None or c in good) and (bad is None or c not in bad)]
            row = row[keep]
            if len(row) < min_cross:
                return None, None
            sd = row.std()
            zs[name] = ((row - row.mean()) / (sd if sd and sd > 0 else 1.0)) * w
        zdf = pd.DataFrame(zs)
        score = zdf.sum(axis=1, min_count=len(zs) // 2)
        log["n_after"].append(len(score.dropna()))
        log["scores"][t] = score
        return score, zdf

    return patched, log


def fwd20(daily_ret, t, codes):
    idx = daily_ret.index
    pos = idx.get_indexer([t])
    if len(pos) == 0 or pos[0] < 0:
        return np.nan
    seg = daily_ret.iloc[pos[0] + 1: pos[0] + 1 + fe.REBAL]
    cols = [c for c in codes if c in seg.columns]
    if not cols or len(seg) == 0:
        return np.nan
    return float(((1 + seg[cols]).prod() - 1).mean())


def main():
    end = pd.Timestamp.today().strftime("%Y-%m-%d")
    conn = fe.get_conn()
    uni, list_dates = fe.load_universe_filter(conn)
    st = sl.load_strategies(conn, [f"prod_6f_eq@topn={TOPN}"])[0]
    factors = {n: sl.load_factor(conn, n, START, end) for n in st["cfg"]["factors"]}
    ep_wide = sl.load_factor(conn, "ep_ttm", START, end)
    ev = load_fin_events(conn)
    load_start = (pd.Timestamp(START) - pd.Timedelta(days=40)).strftime("%Y-%m-%d")
    daily_ret = fe.load_daily_returns(conn, load_start, end)
    rebal = fe.rebalance_dates(daily_ret.index, START, end)
    conn.close()
    orig_score = sl.score_cross

    variants = [
        ("baseline 不过滤", None),
        ("剔亏损股 (ep_ttm<0)", "drop_loss"),
        ("剔 ROE<0 (年化, Q1×4)", "drop_roe_neg"),
        ("剔 负债率>70%", "drop_debt70"),
        ("质量线: 只做 年化ROE≥8%", "drop_roe_8"),
        ("质量线: 只做 年化ROE≥12% (James阈值)", "drop_roe_12"),
        ("三线剔垃圾: 亏损 或 ROE<0 或 负债>80%", "drop_junk"),
    ]

    # ---------- 诊断①: baseline Top30 的财报画像 ----------
    base_patch, base_log = make_patched_score(lambda t: (None, None))
    sl.score_cross = base_patch
    r_base = replay(st, factors, daily_ret, uni, list_dates, rebal)
    sl.score_cross = orig_score
    prof = {"亏损": [], "ROE<0": [], "ROE<12": [], "负债>70": [], "annROE": [], "debt": [], "ST名": []}
    st_names = pd.read_sql("SELECT ts_code, name FROM stocks", fe.get_conn()) if False else None
    for t, score in base_log["scores"].items():
        sel, _ = sl.select_stocks(score.dropna(), TOPN)
        ep = ep_wide.loc[t] if t in ep_wide.index else pd.Series(dtype=float)
        try:
            roe, debt = asof_snapshot(ev, t)
        except ValueError:
            continue
        v = ep.reindex(sel)
        prof["亏损"].append(float((v.isna() | (v < 0)).mean()))
        prof["ROE<0"].append(float((roe.reindex(sel) < 0).mean()))
        prof["ROE<12"].append(float((roe.reindex(sel) < 12).mean()))
        prof["负债>70"].append(float((debt.reindex(sel) > 70).mean()))
        prof["annROE"].append(float(roe.reindex(sel).median()))
        prof["debt"].append(float(debt.reindex(sel).median()))

    lines = [f"# 财报质量过滤测试 (TopN{TOPN}, {START} ~ {end})\n",
             "- 过滤在截面打分之前; ep_ttm 日频 PIT (负=亏损); ROE/负债率 = Q1 年报快照 ann_date<=T asof (Q1×4 年化)",
             "- universe 已剔当前 ST/退市变体; 负债率过滤不豁免金融股 (诊断看实际影响)\n",
             "## 诊断①: baseline Top30 持仓的财报画像 (跨 93 期均值)\n",
             "| 指标 | 数值 |", "|---|---|",
             f"| 亏损股占比 (ep_ttm<0) | {np.mean(prof['亏损'])*100:.1f}% |",
             f"| 年化ROE<0 占比 | {np.mean(prof['ROE<0'])*100:.1f}% |",
             f"| 年化ROE<12% 占比 | {np.mean(prof['ROE<12'])*100:.1f}% |",
             f"| 负债率>70% 占比 | {np.mean(prof['负债>70'])*100:.1f}% |",
             f"| 持仓中位年化ROE | {np.nanmedian(prof['annROE']):.1f}% |",
             f"| 持仓中位负债率 | {np.nanmedian(prof['debt']):.0f}% |\n"]
    print(f"[画像] 亏损 {np.mean(prof['亏损'])*100:.1f}% | ROE<0 {np.mean(prof['ROE<0'])*100:.1f}% | "
          f"ROE<12 {np.mean(prof['ROE<12'])*100:.1f}% | 负债>70 {np.mean(prof['负债>70'])*100:.1f}% | "
          f"中位ROE {np.nanmedian(prof['annROE']):.1f}% | 中位负债 {np.nanmedian(prof['debt']):.0f}%")

    # ---------- 主表 ----------
    lines.append("## 主表: 各过滤方案回测\n")
    lines.append("| 方案 | 年化 | 夏普 | 最大回撤 | 平均截面数 |")
    lines.append("|---|---|---|---|---|")
    s = stats(r_base["nav"])
    base_n = np.mean(base_log["n_after"]) if base_log["n_after"] else np.nan
    lines.append(f"| baseline 不过滤 | {fmt(s['ann'])} | {s['sharpe']:.2f} | {fmt(s['dd'])} | {base_n:.0f} |")
    results, logs = {"baseline 不过滤": r_base}, {"baseline 不过滤": base_log}
    for label, kind in variants[1:]:
        fn = make_filter_fn(ep_wide, ev, kind)
        patched, log = make_patched_score(fn)
        sl.score_cross = patched
        r = replay(st, factors, daily_ret, uni, list_dates, rebal)
        sl.score_cross = orig_score
        results[label] = r
        logs[label] = log
        s = stats(r["nav"])
        n_after = np.mean(log["n_after"]) if log["n_after"] else np.nan
        lines.append(f"| {label} | {fmt(s['ann'])} | {s['sharpe']:.2f} | {fmt(s['dd'])} | {n_after:.0f} |")
        print(f"[{label}] 年化 {fmt(s['ann'])} 夏普 {s['sharpe']:.2f} 回撤 {fmt(s['dd'])} 截面 {n_after:.0f}")

    # ---------- 诊断②: 被踢票 vs 留下票 后20日 ----------
    base_scores = base_log["scores"]
    lines.append("\n## 诊断②: 被过滤踢掉的票, 后 20 日实际收益 (垃圾论点检验)\n")
    lines.append("| 方案 | 期数 | 被踢票均收益 | 留下票均收益 | 被踢-留下(bp/期) | 被踢票跑赢期占比 |")
    lines.append("|---|---|---|---|---|---|")
    for label, kind in variants[1:]:
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
        kr = np.array([p[0] for p in pairs])
        fr = np.array([p[1] for p in pairs])
        lines.append(f"| {label} | {n_periods} | {fmt(kr.mean())} | {fmt(fr.mean())} | "
                     f"{(kr.mean()-fr.mean())*1e4:.0f} | {(kr > fr).mean()*100:.0f}% |")
        print(f"[diag] {label}: 被踢 {fmt(kr.mean())} vs 留下 {fmt(fr.mean())} "
              f"差 {(kr.mean()-fr.mean())*1e4:.0f}bp ({n_periods}期)")

    out = os.path.join(REPO, "reports", "junk_filter_test.md")
    with open(out, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n✅ 报告: {out}")


if __name__ == "__main__":
    main()

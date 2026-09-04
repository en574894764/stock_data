#!/usr/bin/env python3
"""
行业中性化实证评估 (P2)
========================
对比 原始因子 vs 行业中性化因子 的 IC/IR/分层表现, 验证是否值得进生产管道。
行业口径: stocks.industry (tushare, ~90 个细分行业); 无行业的归 "UNKNOWN" 单独一组。
中性化: 每期截面内, 因子值减行业均值再除行业标准差 (行业内 z-score)。

用法:
    python3 scripts/neutralize_eval.py                    # 6 因子, 输出对比报告
    python3 scripts/neutralize_eval.py --start 2019-01-01 --out reports/neutralize_eval.md
"""
import argparse
import os
import sys
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import factor_eval as fe

PROD_FACTORS = ["ret_20d_rev", "turnover_20", "ivol_60", "ln_mv", "ep_ttm", "sue_delta"]


def load_industry(conn) -> pd.Series:
    cur = conn.cursor()
    cur.execute("SELECT ts_code, COALESCE(NULLIF(industry, ''), 'UNKNOWN') FROM stocks")
    s = pd.Series(dict(cur.fetchall()))
    cur.close()
    return s


def neutralize_wide(wide: pd.DataFrame, industry: pd.Series) -> pd.DataFrame:
    """每期截面行业内 z-score (groupby-transform 向量化)."""
    ind = industry.reindex(wide.columns).fillna("UNKNOWN")
    # long 格式 groupby (date, industry)
    long = wide.stack().rename("v").reset_index()
    long.columns = ["trade_date", "ts_code", "v"]
    long["industry"] = long["ts_code"].map(industry)
    g = long.groupby(["trade_date", "industry"])["v"]
    long["v"] = (long["v"] - g.transform("mean")) / g.transform("std").replace(0, np.nan)
    out = long.pivot(index="trade_date", columns="ts_code", values="v")
    return out.reindex(index=wide.index, columns=wide.columns)


def eval_one(name, factor, fwd, daily_ret, rebal, uni, list_dates):
    r = fe.evaluate_single(name, factor, fwd, daily_ret, rebal, uni, list_dates)
    ics = r["ics"]
    q = r["q_navs"]
    out = {"ic_mean": ics.mean() if len(ics) else np.nan,
           "ir": ics.mean() / ics.std() if len(ics) > 1 and ics.std() > 0 else np.nan,
           "ic_pos": (ics > 0).mean() if len(ics) else np.nan}
    if q is not None and not q.empty:
        s5 = fe.nav_stats(q[fe.QUANTILES])
        ls = fe.nav_stats(q[fe.QUANTILES] / q[1])
        out["q5_ann"] = s5["ann_ret"]
        out["ls_ann"] = ls["ann_ret"]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--end", default="2026-09-01")
    ap.add_argument("--factors", nargs="*", default=PROD_FACTORS)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    conn = fe.get_conn()
    uni, list_dates = fe.load_universe_filter(conn)
    industry = load_industry(conn)
    load_start = (pd.Timestamp(args.start) - pd.Timedelta(days=40)).strftime("%Y-%m-%d")
    daily_ret = fe.load_daily_returns(conn, load_start, args.end)
    fwd = fe.fwd_from_daily(daily_ret)
    rebal = fe.rebalance_dates(daily_ret.index, args.start, args.end)

    rows = []
    for name in args.factors:
        raw = fe.load_factors(conn, args.start, args.end).get(name)
        if raw is None:
            print(f"跳过 {name}: 缓存无此因子")
            continue
        raw = raw[(raw.index >= pd.Timestamp(args.start)) & (raw.index <= pd.Timestamp(args.end))]
        neu = neutralize_wide(raw, industry)

        e_raw = eval_one(name, raw, fwd, daily_ret, rebal, uni, list_dates)
        e_neu = eval_one(f"{name}_neu", neu, fwd, daily_ret, rebal, uni, list_dates)
        rows.append({"factor": name, **{f"raw_{k}": v for k, v in e_raw.items()},
                     **{f"neu_{k}": v for k, v in e_neu.items()}})
        print(f"{name}: IC {e_raw['ic_mean']:.3f} → {e_neu['ic_mean']:.3f} | "
              f"多空 {e_raw.get('ls_ann', np.nan):.1%} → {e_neu.get('ls_ann', np.nan):.1%}")

    df = pd.DataFrame(rows)
    L = ["# 行业中性化实证评估\n",
         f"- 区间: {args.start} ~ {args.end} | 行业: stocks.industry (tushare, 无行业归 UNKNOWN)",
         "- 中性化 = 每期截面行业内 z-score\n",
         "| 因子 | IC原 | IC中 | IR原 | IR中 | IC正率原 | IC正率中 | 多空年化原 | 多空年化中 |",
         "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        L.append("| {} | {:.3f} | {:.3f} | {:.2f} | {:.2f} | {:.0%} | {:.0%} | {:.1%} | {:.1%} |".format(
            r["factor"], r["raw_ic_mean"], r["neu_ic_mean"], r["raw_ir"], r["neu_ir"],
            r["raw_ic_pos"], r["neu_ic_pos"], r["raw_ls_ann"], r["neu_ls_ann"]))

    # 总结论
    ic_up = sum(1 for r in rows if r["neu_ic_mean"] > r["raw_ic_mean"])
    ls_up = sum(1 for r in rows if r["neu_ls_ann"] > r["raw_ls_ann"])
    L.append(f"\n**结论**: {len(rows)} 因子中, 中性化后 IC 改善 {ic_up} 个, 多空改善 {ls_up} 个。")
    L.append("(行业暴露是收益来源还是噪音, 看改善比例; 大幅改善 → 值得进生产管道)")

    report = "\n".join(L)
    print("\n" + report)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w") as f:
            f.write(report + "\n")
        print(f"报告已写入: {args.out}")
    conn.close()


if __name__ == "__main__":
    main()

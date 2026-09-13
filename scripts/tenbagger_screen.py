#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
十倍股早期识别漏斗 —— 回测校准 + 当下候选生成

漏斗设计（全部无前视偏差，仅用 t 时点可见数据）：
  L0 基础过滤：非 ST / 非退市 / 非北交所, 上市满 2 年
  L1 市值上限：total_mv <= 500 亿 (十倍股起点市值约束)
  L2 成长质量：营收增速 or_yoy >= G_MIN, ROE >= R_MIN, 毛利率 >= M_MIN (最近已披露中报)
  L3 行业景气（可选层）：个股所属行业的营收增速中位数位居前 1/3 且 >0
  L4 动量确认（可选层）：过去 12 个月复权收益 >= 市场中位数 (相对强度)

用法：
  python3 scripts/tenbagger_screen.py --asof 2016-09-12 --end 2026-09-11   # 2016 回测
  python3 scripts/tenbagger_screen.py --asof 2026-09-11                     # 当下候选
"""
import argparse
import json
import sys

import numpy as np
import pandas as pd
import psycopg2

DB = dict(host="/tmp", dbname="investassist", user="james")


def q(conn, sql, p=None):
    return pd.read_sql(sql, conn, params=p)


def fwd_window(asof: str, years: int = 10):
    t = pd.Timestamp(asof)
    return (t + pd.DateOffset(days=1)).strftime("%Y-%m-%d"), (t + pd.DateOffset(years=years)).strftime("%Y-%m-%d")


def build_snapshot(conn, asof: str, list_years: float = 2.0):
    """t 时点截面：行情估值 + 最近中报财务 + 12 个月动量。全部 <= asof 可见。
    list_years: 上市时长下限（年）；次新通道可降至 0.25"""
    t = pd.Timestamp(asof)
    # --- 股票池：asof 前有交易、A 股 ---
    uni = q(conn, """
        SELECT ts_code FROM daily_quote
        WHERE trade_date BETWEEN %(a)s AND %(b)s AND ts_code ~ '\\.(SH|SZ)$'
        GROUP BY ts_code
    """, {"a": (t - pd.DateOffset(days=75)).strftime("%Y-%m-%d"), "b": asof})
    codes = uni["ts_code"].tolist()

    # --- stocks 元数据（上市日期/行业/名称），剔除 ST 与上市不满 2 年 ---
    meta = q(conn, "SELECT ts_code, name, industry, list_date FROM stocks WHERE ts_code = ANY(%(c)s)", {"c": codes})
    meta = meta.set_index("ts_code")
    meta["list_date"] = pd.to_datetime(meta["list_date"], format="%Y%m%d", errors="coerce")
    meta = meta[~meta["name"].str.contains("ST", na=False)]
    meta = meta[meta["list_date"] <= t - pd.DateOffset(days=int(365.25 * list_years))]

    # --- daily_basic 截面（asof 前最近一行） ---
    db = q(conn, """
        SELECT DISTINCT ON (ts_code) ts_code, trade_date, pe_ttm, pb, total_mv
        FROM daily_basic
        WHERE trade_date BETWEEN %(a)s AND %(b)s AND ts_code = ANY(%(c)s)
        ORDER BY ts_code, trade_date DESC
    """, {"a": (t - pd.DateOffset(days=10)).strftime("%Y-%m-%d"), "b": asof, "c": codes}).set_index("ts_code")

    # --- 最近中报（8 月底前披露完毕）：当年 report_type='2' ---
    fi = q(conn, """
        SELECT ts_code, roe, or_yoy, netprofit_yoy, grossprofit_margin
        FROM financial_indicator WHERE report_year=%(y)s AND report_type='2'
    """, {"y": t.year}).set_index("ts_code")

    # --- 12 个月复权动量 ---
    mom = q(conn, """
        SELECT ts_code, exp(sum(ln(1.0 + pct_chg/100.0))) - 1.0 AS ret12m
        FROM daily_quote
        WHERE trade_date BETWEEN %(a)s AND %(b)s AND pct_chg IS NOT NULL AND ts_code = ANY(%(c)s)
        GROUP BY ts_code
    """, {"a": (t - pd.DateOffset(days=380)).strftime("%Y-%m-%d"), "b": asof, "c": codes}).set_index("ts_code")

    df = meta.join(db[["pe_ttm", "pb", "total_mv"]], how="left") \
             .join(fi, how="left").join(mom, how="left")
    df["mv_yi"] = df["total_mv"] / 1e4
    # 行业景气：行业营收增速中位数（样本>=3 才统计）
    ind = df.groupby("industry")["or_yoy"].agg(["median", "count"])
    ind.columns = ["ind_or_yoy_med", "ind_n"]
    df = df.join(ind, on="industry")
    ok = df["ind_n"] >= 3
    ranks = df.loc[ok, "ind_or_yoy_med"].rank(pct=True)
    df["ind_rank"] = np.nan
    df.loc[ok, "ind_rank"] = ranks
    return df


def apply_funnel(df, g_min, r_min, m_min, mv_max, use_ind, use_mom, mom_pct=0.5, ind_pct=2/3):
    """返回 (通过组合的 df, 各层剩余数量)。"""
    layers = {}
    d = df.dropna(subset=["total_mv", "or_yoy", "roe", "grossprofit_margin", "ret12m"]).copy()
    layers["L0 有完整数据"] = len(d)
    d = d[d["mv_yi"] <= mv_max]
    layers[f"L1 市值<={mv_max}亿"] = len(d)
    d = d[(d["or_yoy"] >= g_min) & (d["roe"] >= r_min) & (d["grossprofit_margin"] >= m_min)]
    layers[f"L2 营收增速>={g_min}% & ROE>={r_min}% & 毛利>={m_min}%"] = len(d)
    if use_ind:
        d = d[(d["ind_rank"] >= ind_pct) & (d["ind_or_yoy_med"] > 0)]
        layers["L3 行业景气前1/3"] = len(d)
    if use_mom:
        thr = df["ret12m"].quantile(mom_pct)
        d = d[d["ret12m"] >= thr]
        layers[f"L4 动量前{int((1-mom_pct)*100)}%"] = len(d)
    return d, layers


def future_returns(conn, codes, start, end):
    """start->end 各股复权累计倍数。"""
    if not codes:
        return pd.Series(dtype=float)
    r = q(conn, """
        SELECT ts_code, exp(sum(ln(1.0 + pct_chg/100.0))) AS mult
        FROM daily_quote
        WHERE trade_date BETWEEN %(a)s AND %(b)s AND pct_chg IS NOT NULL AND ts_code = ANY(%(c)s)
        GROUP BY ts_code
    """, {"a": start, "b": end, "c": codes}).set_index("ts_code")["mult"]
    return r


def bench_mult(conn, start, end, symbol="000300.SH"):
    r = q(conn, """
        SELECT exp(sum(ln(1.0 + pct_chg/100.0))) - 1.0 AS r FROM index_daily
        WHERE symbol=%(s)s AND trade_date BETWEEN %(a)s AND %(b)s
    """, {"s": symbol, "a": start, "b": end})
    return float(r["r"].iloc[0]) + 1.0 if len(r) else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", required=True)
    ap.add_argument("--end", default=None, help="回测终点；缺省=仅生成候选")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    conn = psycopg2.connect(**DB)
    df = build_snapshot(conn, args.asof)
    print(f"=== asof {args.asof}: 基础股票池 {len(df)} 只 ===")

    # 参数网格（校准用，保持少量）
    grids = [
        dict(g_min=15, r_min=8,  m_min=25, mv_max=500, use_ind=False, use_mom=False),
        dict(g_min=15, r_min=8,  m_min=25, mv_max=500, use_ind=True,  use_mom=False),
        dict(g_min=15, r_min=8,  m_min=25, mv_max=500, use_ind=True,  use_mom=True),
        dict(g_min=20, r_min=10, m_min=25, mv_max=500, use_ind=True,  use_mom=True),
        dict(g_min=20, r_min=10, m_min=30, mv_max=300, use_ind=True,  use_mom=True),
    ]

    if args.end:  # 回测模式
        start = (pd.Timestamp(args.asof) + pd.DateOffset(days=1)).strftime("%Y-%m-%d")
        # 十倍股名单（终点口径）
        tb = q(conn, """
            SELECT ts_code, exp(sum(ln(1.0+pct_chg/100.0))) AS mult
            FROM daily_quote
            WHERE trade_date BETWEEN %(a)s AND %(b)s AND pct_chg IS NOT NULL AND ts_code ~ '\\.(SH|SZ)$'
            GROUP BY ts_code HAVING exp(sum(ln(1.0+pct_chg/100.0))) >= 10
        """, {"a": start, "b": args.end}).set_index("ts_code")["mult"]
        tb = tb[tb.index.isin(df.index)]
        print(f"窗口 {start} ~ {args.end}: 全池十倍股 {len(tb)} 只")
        bm = bench_mult(conn, start, args.end)
        print(f"沪深300 同期倍数: {bm:.2f}x")
        uni_mult = future_returns(conn, df.index.tolist(), start, args.end)

        results = []
        for gi, g in enumerate(grids):
            sel, layers = apply_funnel(df, **g)
            hits = sorted(set(sel.index) & set(tb.index))
            fut = future_returns(conn, sel.index.tolist(), start, args.end)
            port_mult = float(fut.mean()) if len(fut) else None  # 等权持有
            prec = len(hits) / len(sel) * 100 if len(sel) else 0
            base = len(tb) / len(df) * 100
            hit_mults = tb.loc[hits] if hits else pd.Series(dtype=float)
            other = fut.drop(index=hits, errors="ignore")
            results.append(dict(
                grid=gi + 1, params=g, n=len(sel), layers=layers,
                hit=len(hits), hit_total=len(tb), precision=round(prec, 2),
                base_rate=round(base, 2), lift=round(prec / base, 1) if base else None,
                port_mult=round(port_mult, 2) if port_mult else None,
                bench_mult=round(bm, 2),
                other_mult=round(float(other.mean()), 2) if len(other) else None,
                hit_names=[f"{df.loc[c, 'name']}({tb[c]:.0f}x)" for c in hits],
            ))
            print(f"\n[grid{gi+1}] {g}")
            for k, v in layers.items():
                print(f"    {k}: {v}")
            print(f"    => 组合 {len(sel)} 只 | 命中 {len(hits)}/{len(tb)} | 精度 {prec:.2f}% (随机 {base:.2f}%, {prec/base:.1f}x)")
            print(f"    => 组合等权 {port_mult:.2f}x vs 沪深300 {bm:.2f}x | 未命中部分 {float(other.mean()):.2f}x" if len(other) else "")
            print(f"    => 命中: {[f'{df.loc[c, chr(215)+chr(215)]}' for c in hits] if False else [df.loc[c,'name'] for c in hits]}")

        out = dict(asof=args.asof, end=args.end, universe=len(df), tenbaggers=len(tb),
                   tenbagger_codes=tb.index.tolist(), results=results)
        op = args.out or "outputs/tenbagger_screen_backtest.json"
        with open(op, "w") as f:
            json.dump(out, f, ensure_ascii=False, indent=1, default=str)
        print(f"\n-> {op}")
    else:  # 候选生成模式（用校准后的最终参数，由调用处指定）
        sel, layers = apply_funnel(df, g_min=15, r_min=8, m_min=25, mv_max=500, use_ind=True, use_mom=True)
        print("漏斗各层:", json.dumps(layers, ensure_ascii=False, indent=1))
        cols = ["name", "industry", "mv_yi", "pe_ttm", "pb", "roe", "or_yoy",
                "netprofit_yoy", "grossprofit_margin", "ret12m", "ind_or_yoy_med"]
        out_df = sel[cols].sort_values("or_yoy", ascending=False).round(2)
        out_df.index.name = "ts_code"
        op = args.out or "outputs/tenbagger_candidates_latest.csv"
        out_df.to_csv(op, encoding="utf-8-sig")
        print(f"-> {op}  ({len(out_df)} 只)")
        print(out_df.head(40).to_string())


if __name__ == "__main__":
    main()

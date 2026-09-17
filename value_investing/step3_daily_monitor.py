#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
任务③ 每日扫描 + 分档 + 趋势
─────────────────────────────────────────────────────────
每天对「合格 list」做一次体检：

  1. 取最新交易日的总市值
  2. 与两个合理估值比：
       - 对去年年报合理估值的折价率
       - 对三年后合理估值的折价率      ← 分档依据
  3. 按 mv_ratio = 当前总市值 / 三年后合理估值 落四档：
       mv_ratio ≤ 0.5        非常低估
       0.5 < mv_ratio ≤ 0.7  低估
       0.7 < mv_ratio ≤ 1.0  一般低估
       mv_ratio > 1.0        高估
     （阈值全部在 config.json → monitor.tiers，可改）
  4. 判趋势：近 5/20/60 日涨跌幅、均线位置、MA20 斜率 → 上升/震荡/下降

输出：outputs/daily_scan_YYYYMMDD.csv（全字段，可直接接别的分析）
      再由 render_report.py 生成 HTML 日报
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

import numpy as np
import pandas as pd

import step2_fair_value as s2
import valuelib as vl


def scan(cfg: dict | None = None, base_year: int | None = None, trade_date: date | None = None,
         conn=None) -> tuple[pd.DataFrame, dict]:
    """跑一遍每日扫描，返回 (明细 DataFrame, 元信息)"""
    cfg = cfg or vl.load_config()
    base_year = base_year or vl.resolve_base_year(cfg)

    fv = s2.compute_fair_value(cfg, base_year, conn=conn)
    fv = fv[fv["估值可用"]].reset_index(drop=True)
    if fv.empty:
        raise RuntimeError("没有可估值的标的，请检查选股阈值与财报数据")

    codes = fv["代码"].tolist()
    conn = conn or vl.connect()

    trade_date = trade_date or vl.latest_trade_date(conn)
    snap = vl.fetch_market_snapshot(codes, trade_date, conn=conn)
    hist = vl.fetch_price_history(codes, int(cfg["monitor"]["price_lookback_days"]), conn=conn)

    # 收盘价序列 → 趋势指标 + 走势图数据
    curves, tr = {}, {}
    for ts, grp in hist.groupby("ts_code"):
        s = grp.sort_values("trade_date")["close"].astype(float).reset_index(drop=True)
        curves[ts] = s
        tr[ts] = vl.compute_trend(s, cfg)

    df = fv.merge(snap[["ts_code", "close", "total_mv", "pe_ttm", "pb", "circ_mv"]],
                  left_on="代码", right_on="ts_code", how="left").drop(columns=["ts_code"])

    df["当前市值"] = df["total_mv"].apply(vl.wan_to_yi)
    df["流通市值"] = df["circ_mv"].apply(vl.wan_to_yi)
    df["市值比_三年后"] = df["当前市值"] / df["三年后合理估值"]
    df["市值比_去年"] = df["当前市值"] / df["去年年报合理估值"]
    # 折价率口径：低估为负、高估为正 → 折价率 = (市值 − 合理估值) / 合理估值 = 市值比 − 1
    df["折价率_三年后"] = df["市值比_三年后"] - 1
    df["折价率_去年"] = df["市值比_去年"] - 1
    df["上行空间"] = df["市值比_三年后"].apply(vl.upside)
    df["档位"] = [vl.classify_tier(r, cfg)[0] for r in df["市值比_三年后"]]
    df["档位颜色"] = [vl.classify_tier(r, cfg)[1] for r in df["市值比_三年后"]]

    for k in ["ret_5", "ret_20", "ret_60", "ma20", "ma60", "ma20_slope", "pos_250",
              "trend", "trend_score", "high_250", "low_250"]:
        df[k] = df["代码"].map(lambda c, k=k: tr.get(c, {}).get(k, np.nan))

    df = df.rename(columns={"ret_5": "近5日", "ret_20": "近20日", "ret_60": "近60日",
                            "ma20": "MA20", "ma60": "MA60", "ma20_slope": "MA20斜率",
                            "pos_250": "250日分位", "trend": "趋势", "trend_score": "趋势分",
                            "high_250": "250日高", "low_250": "250日低",
                            "pe_ttm": "PE_TTM", "pb": "PB"})

    df["_curve"] = df["代码"].map(curves)
    df = df.sort_values("市值比_三年后").reset_index(drop=True)

    meta = dict(trade_date=trade_date, base_year=base_year, cfg=cfg,
                n_screen=len(df), n_invalid=fv.shape[0] - len(df))
    return df, meta


def tier_summary(df: pd.DataFrame) -> pd.DataFrame:
    """按档位汇总（实现见 valuelib，便于渲染层复用）"""
    return vl.tier_summary(df)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="价值投资 · 任务③ 每日扫描")
    ap.add_argument("--date", help="指定交易日 YYYY-MM-DD，默认最新")
    ap.add_argument("--year", type=int, help="基准年报年份")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    cfg = vl.load_config()
    if args.year:
        cfg["screen"]["base_year"] = args.year

    td = pd.to_datetime(args.date).date() if args.date else None
    df, meta = scan(cfg, trade_date=td)

    out = args.out or str(vl.OUTPUT_DIR / f"daily_scan_{meta['trade_date']:%Y%m%d}.csv")
    df.drop(columns=["_curve"]).to_csv(out, index=False, encoding="utf-8-sig")

    print(f"[任务③] 交易日 {meta['trade_date']} | 基准年报 {meta['base_year']}"
          f" | 扫描 {meta['n_screen']} 只 → {out}")
    print(tier_summary(df).to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

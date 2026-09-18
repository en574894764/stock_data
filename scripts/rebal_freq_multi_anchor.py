#!/usr/bin/env python3
"""调仓周期对照实验（多锚点 × 完整相位周期）：周频(5) / 双周(10) / 月度(20)
====================================================================================
背景
  reports/rebalance_freq.md (2026-09-15) 已做过一次单锚点(2019)对照，结论「双周是甜点」，
  但留了两个未决尾巴：
    ① 只有 anchor=2019 单锚点，未交叉验证；
    ② 相位 offset 0..19 在 REBAL=5/10 下是 mod-R 重复采样（周频只有 5 个独立相位），
       脚本报的离散度被重复样本压低，不可直接采信。

本实验
  - 相位口径修正：REBAL=R 时只跑 offset 0..R-1 = **一个完整周期**（每类相位恰好一次）
  - 锚点交叉：anchor = 2019-01-01 / 2020-01-01 / 2021-01-01（同一锚点内跨频率可比）
  - 组合层锁定生产口径：Top30 + min_var_cap10 (60日 Ledoit-Wolf 协方差) + 单边成本 0.15%
  - 因子 = 生产 6 因子 (raw z 等权合成，= prod_6f_eq)，持有期 = 调仓间隔

指标
  年化 / 夏普 / 回撤 / 单期换手 / 年化成本 / 样本外(>=2023)夏普 / 年均调仓次数
  另跑 cost=0 归因轮，把「年化差」拆成 成本 vs 因子期限错配

用法:
  python3 scripts/rebal_freq_multi_anchor.py                      # 全量 (~20min)
  python3 scripts/rebal_freq_multi_anchor.py --rebals 20 --anchors 2019-01-01 --n-phase 3
  python3 scripts/rebal_freq_multi_anchor.py --cost 0             # 零成本归因轮
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np
import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import factor_eval as fe          # noqa: E402
import strategy_lib as sl         # noqa: E402
import factor_weight_scan as fws  # noqa: E402
from phase_robustness import SectionZ, PROD6  # noqa: E402
from portfolio_optimization import compute_weights  # noqa: E402

HIST = 60          # 协方差窗口 (交易日)
TOP_N = 30
BACK_TO = "2016-01-01"
OOS = pd.Timestamp("2023-01-01")


# ---------------------------------------------------------------- 回测 (与 fws.backtest 同口径, 跳过 IC/Ridge)
def backtest_light(sec, daily_ret, rebal_dates, cost=0.0015, period=None):
    """period = 持有交易日数 (默认 fe.REBAL). 返回 nav + 换手序列。"""
    period = period or fe.REBAL
    dr_idx, dr_cols = daily_ret.index, daily_ret.columns
    pos_cache = {}

    def pos_dates(t):
        key = dr_idx.get_indexer([t])[0]
        if key < 0:
            return []
        if key not in pos_cache:
            pos_cache[key] = list(dr_idx[key + 1: key + 1 + period])
        return pos_cache[key]

    rets, turns, prev_w = [], [], {}
    for t in rebal_dates:
        zdf = sec.zdf(t)
        if zdf is None:
            continue
        score = zdf.sum(axis=1, min_count=3).dropna()      # 6 因子等权合成 (raw z)
        if len(score) < 50:
            continue
        sel = list(score.sort_values(ascending=False).head(TOP_N).index)
        if len(sel) < 5:
            continue
        pdates = pos_dates(t)
        if not pdates:
            break
        hist = daily_ret.loc[:t].iloc[-HIST:][[c for c in sel if c in dr_cols]]
        if hist.shape[1] < 5 or hist.shape[0] < 20:
            continue
        w = compute_weights(hist, "min_var_cap10")
        stocks = hist.columns.tolist()
        pmap = dict(zip(stocks, w))
        to = 0.5 * sum(abs(pmap.get(c, 0) - prev_w.get(c, 0))
                       for c in set(pmap) | set(prev_w))
        turns.append(to)
        m = daily_ret.iloc[dr_idx.get_indexer(pdates),
                           dr_cols.get_indexer(stocks)].to_numpy(float)
        dr = np.where(np.isfinite(m @ w), m @ w, 0.0)
        dr[0] -= to * 2 * cost
        for d, r in zip(pdates, dr):
            rets.append((d, float(r)))
        prev_w = pmap
    nav = (1 + pd.Series(dict(rets)).sort_index()).cumprod()
    return nav, (float(np.mean(turns)) if turns else np.nan), len(turns)


def pct(v):
    return "-" if v is None or not np.isfinite(v) else f"{v * 100:.1f}%"


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebals", default="20,10,5", help="调仓间隔(交易日), 逗号分隔")
    ap.add_argument("--anchors", default="2019-01-01,2020-01-01,2021-01-01")
    ap.add_argument("--cost", type=float, default=0.0015, help="单边成本; 0=零成本归因")
    ap.add_argument("--n-phase", type=int, default=0, help="覆盖相位数 (默认=REBAL 完整周期)")
    ap.add_argument("--tag", default="", help="输出文件名后缀")
    args = ap.parse_args()

    rebals = [int(x) for x in args.rebals.split(",")]
    anchors = args.anchors.split(",")
    end = pd.Timestamp.today().strftime("%Y-%m-%d")

    conn = fe.get_conn()
    uni, list_dates = fe.load_universe_filter(conn)
    daily_ret = fe.load_daily_returns(conn, "2015-10-01", end)
    idx = daily_ret.index
    fw = {n: sl.load_factor(conn, n, "2015-06-01", end).astype("float32") for n in PROD6}
    conn.close()
    print(f"股票池 {len(uni)} | 交易日 {idx[0].date()}~{idx[-1].date()} | 因子 {PROD6}", flush=True)

    # z 缓存跨频率/跨锚点/跨相位复用（z 只依赖因子截面，与 REBAL 无关）
    sec = SectionZ(fw, daily_ret, uni, list_dates, mode="raw")

    tag = f"_{args.tag}" if args.tag else ""
    rows_path = os.path.join(REPO, "outputs", f"rebal_freq_multi_anchor{tag}_rows.jsonl")
    open(rows_path, "w").close()   # 新一轮清空 (旧轮数据已归档在 json/md)
    rows = []
    for reb in rebals:
        fe.REBAL = reb
        n_phase = args.n_phase or reb
        for anchor in anchors:
            for off in range(n_phase):
                t0 = time.time()
                g = fws.offset_grid(idx, anchor, off, end)
                rb = [t for t in g if t >= pd.Timestamp(anchor)]
                nav, turn, n_per = backtest_light(sec, daily_ret, rb, cost=args.cost, period=reb)
                if nav.empty:
                    continue
                s = fws.stats(nav)
                so = fws.stats(nav[nav.index >= OOS])
                per_year = 244.0 / reb
                rec = {
                    "rebal": reb, "anchor": anchor, "offset": off, "cost": args.cost,
                    "ann": s["ann"], "sharpe": s["sharpe"], "dd": s["dd"],
                    "oos_sharpe": so.get("sharpe", np.nan), "oos_ann": so.get("ann", np.nan),
                    "turn_per_period": turn, "turn_per_year": turn * per_year,
                    "ann_cost": turn * 2 * args.cost * per_year,
                    "n_rebal": n_per, "rebal_per_year": per_year,
                    "secs": time.time() - t0,
                }
                rows.append(rec)
                # 增量落盘: 中途异常不丢已算结果 (JSONL 可续跑/可复核)
                with open(rows_path, "a") as fh:
                    fh.write(json.dumps(rec) + "\n")
            d = pd.DataFrame([r for r in rows if r["rebal"] == reb and r["anchor"] == anchor])
            print(f"  REBAL={reb:2d} anchor={anchor} | 相位 {n_phase} | "
                  f"年化均值 {pct(d['ann'].mean())} 离散 {d['ann'].std()*100:.1f}pp "
                  f"最差 {pct(d['ann'].min())} 最好 {pct(d['ann'].max())} "
                  f"夏普均值 {d['sharpe'].mean():.2f} ({time.time()-t0:.0f}s/相位)", flush=True)

    df = pd.DataFrame(rows)
    # ---------------------------------------------------------- 汇总
    L = [f"# 调仓周期对照：周频(5) / 双周(10) / 月度(20)",
         "",
         f"> {pd.Timestamp.today().date()} | 单边成本 {args.cost*100:.2f}% | "
         f"组合层锁定生产口径 Top30 + min_var_cap10 + 60日协方差 | 因子 = 生产 6 因子 raw z 等权",
         f"> 相位 = 各锚点起头 **完整周期** offset 0..R-1（每类相位恰好一次，修正上轮 mod-R 重复采样）",
         f"> 锚点 = {', '.join(anchors)} | 持有期 = 调仓间隔", ""]

    # 分锚点明细
    L += ["## 分锚点明细", "",
          "| 锚点 | 频率 | 年化均值 | 年化中位 | 年化离散 | 最差 | 最好 | 夏普均值 | 回撤均值 | OOS夏普均值 |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    summ = {}
    for anchor in anchors:
        for reb in rebals:
            d = df[(df["rebal"] == reb) & (df["anchor"] == anchor)]
            if d.empty:
                continue
            summ[(anchor, reb)] = {
                "ann_mean": float(d["ann"].mean()), "ann_med": float(d["ann"].median()),
                "ann_std": float(d["ann"].std()), "ann_min": float(d["ann"].min()),
                "ann_max": float(d["ann"].max()), "sharpe_mean": float(d["sharpe"].mean()),
                "dd_mean": float(d["dd"].mean()), "oos_sharpe": float(d["oos_sharpe"].mean()),
                "n_phase": int(len(d)),
            }
            L.append(f"| {anchor} | {reb}日 | **{pct(d['ann'].mean())}** | {pct(d['ann'].median())} | "
                     f"{d['ann'].std()*100:.1f}pp | {pct(d['ann'].min())} | {pct(d['ann'].max())} | "
                     f"{d['sharpe'].mean():.2f} | {pct(d['dd'].mean())} | {d['oos_sharpe'].mean():.2f} |")

    # 跨锚点汇总（等权平均 N 个锚点）
    n_anchor = len(anchors)
    agg_label = "单锚点" if n_anchor == 1 else f"{n_anchor} 锚点等权平均"
    L += ["", f"## 跨锚点汇总（{agg_label}）", "",
          "| 频率 | 平均年化 | 平均离散 | 最差相位均值 | 平均夏普 | 平均回撤 | 单期换手 | 年化成本 | 年均调仓次数 |",
          "|---|---|---|---|---|---|---|---|---|"]
    agg = {}
    for reb in rebals:
        d = df[df["rebal"] == reb]
        if d.empty:
            continue
        per_anchor_mean = d.groupby("anchor")["ann"].mean().mean()
        per_anchor_std = d.groupby("anchor")["ann"].std().mean()
        per_anchor_min = d.groupby("anchor")["ann"].min().mean()
        agg[reb] = {
            "ann": float(per_anchor_mean), "disp": float(per_anchor_std),
            "min": float(per_anchor_min), "sharpe": float(d["sharpe"].mean()),
            "dd": float(d["dd"].mean()), "turn_pp": float(d["turn_per_period"].mean()),
            "ann_cost": float(d["ann_cost"].mean()),
            "rebal_per_year": float(d["rebal_per_year"].mean()),
            "rebal_per_year_end": 244.0 / reb,
        }
        L.append(f"| **{reb}日** (年 {agg[reb]['rebal_per_year']:.0f} 次调仓) | "
                 f"**{pct(agg[reb]['ann'])}** | {agg[reb]['disp']*100:.1f}pp | "
                 f"{pct(agg[reb]['min'])} | {agg[reb]['sharpe']:.2f} | {pct(agg[reb]['dd'])} | "
                 f"{agg[reb]['turn_pp']*100:.0f}% | {agg[reb]['ann_cost']*100:.2f}% | "
                 f"{agg[reb]['rebal_per_year']:.0f} |")

    # 逐相位明细
    L += ["", "## 逐相位年化", "",
          "| 锚点 | offset | " + " | ".join(f"{r}日" for r in rebals) + " |",
          "|---" * (2 + len(rebals)) + "|"]
    for anchor in anchors:
        max_off = max(rebals)
        for off in range(max_off):
            cells = []
            for reb in rebals:
                v = df[(df["rebal"] == reb) & (df["anchor"] == anchor) & (df["offset"] == off)]["ann"]
                cells.append(pct(v.iloc[0]) if len(v) else "—")
            L.append(f"| {anchor} | {off} | " + " | ".join(cells) + " |")

    # 判读
    base = agg.get(20)
    L += ["", "## 判读", ""]
    if base and 10 in agg and 5 in agg:
        for reb in (10, 5):
            a = agg[reb]
            L.append(f"- **{reb}日 vs 20日**：平均年化 {(a['ann']-base['ann'])*100:+.1f}pp、"
                     f"离散 {a['disp']*100:.1f}pp vs {base['disp']*100:.1f}pp、"
                     f"最差相位 {pct(a['min'])} vs {pct(base['min'])}、"
                     f"夏普 {a['sharpe']:.2f} vs {base['sharpe']:.2f}、"
                     f"年化成本 {a['ann_cost']*100:.2f}% vs {base['ann_cost']*100:.2f}%")
            wr = []
            for anchor in anchors:
                x = df[(df["rebal"] == reb) & (df["anchor"] == anchor)].sort_values("offset")["ann"]
                y = df[(df["rebal"] == 20) & (df["anchor"] == anchor)].sort_values("offset")["ann"]
                k = min(len(x), len(y))     # 相位数不同 → 按 offset 对齐取前 k 个
                if k:
                    wr.append(float((x.to_numpy()[:k] > y.to_numpy()[:k]).mean()))
            if wr:
                L.append(f"  - 相位胜率（vs 20日，按 offset 对齐，3 锚点均值 {min(reb,20)} 个相位）："
                         f"**{np.mean(wr)*100:.0f}%**")

    outp = os.path.join(REPO, "reports", f"rebal_freq_multi_anchor{tag}.md")
    with open(outp, "w") as f:
        f.write("\n".join(L) + "\n")
    with open(os.path.join(REPO, "outputs", f"rebal_freq_multi_anchor{tag}.json"), "w") as f:
        json.dump({"cost": args.cost, "anchors": anchors, "rebals": rebals,
                   "summary": {f"{k[0]}|{k[1]}": v for k, v in summ.items()},
                   "agg": {str(k): v for k, v in agg.items()}, "rows": rows},
                  f, ensure_ascii=False, indent=2)
    print("\n" + "\n".join(L[-25:]))
    print(f"\n✅ {outp}")


if __name__ == "__main__":
    main()

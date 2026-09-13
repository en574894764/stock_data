#!/usr/bin/env python3
"""QFA 单季口径 + SUE 多子因子 评估（P3#3）
====================================================================================
评估框架复用 factor_weight_scan（同相位网格 + walk-forward + 多相位稳健性检验），
组合层锁定生产口径：Top30 + min_var_cap10 + 60日协方差 + 单边成本 0.15%。

四道验收关（与既有实验一致）
  1. 单因子 IC / ICIR / IC正率（同相位网格，2016 起 burn-in，2019 起报）
  2. 正交性：与现有 sue_gr / sue_delta 的截面秩相关（>0.8 视为冗余）
  3. 组合增益：等权池替换/新增 → 年化、夏普、样本外夏普
  4. 多相位稳健性：offset 2/5/10/15 交易日网格，报均值/最差
     ⚠ 判据：单相位 +2pp 以内改进不足为信（相位教训：错开 10 交易日曾致 9.8% vs 18.7%）

用法
  python3 scripts/qfa_sue_eval.py                  # 全流程
  python3 scripts/qfa_sue_eval.py --no-phase       # 跳过多相位
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import sys

import numpy as np
import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import factor_eval as fe          # noqa: E402
import strategy_lib as sl         # noqa: E402
import factor_weight_scan as fws  # noqa: E402  (Section / backtest / build_grid / stats)
import compute_qfa_sue as cq      # noqa: E402  (新因子计算，复用同一份口径)

ANCHOR = "2019-01-01"
BACK_TO = "2016-01-01"
PHASE_OFFSETS = [2, 5, 10, 15]

NEW = ["q_np_yoy", "q_or_yoy", "q_op_yoy", "q_roe_d", "q_acc_np",
       "sue_q_np", "sue_q_or", "sue_q_op", "sue_q_np_d"]
REF = ["sue_gr", "sue_delta"]
PROD6 = ["ln_mv", "ep_ttm", "ivol_60", "sue_delta", "ret_20d_rev", "turnover_20"]
PROD_OTHER = ["ln_mv", "ep_ttm", "ivol_60", "ret_20d_rev", "turnover_20"]


def loadf(conn, name, start, end):
    """加载因子宽表并降为 float32（15 张宽表 float64 会吃掉 ~2GB）。"""
    w = sl.load_factor(conn, name, start, end).astype("float32")
    gc.collect()
    return w


def stats_full(nav: pd.Series) -> dict:
    s = fws.stats(nav)
    s["oos_sharpe"] = fws.stats(nav[nav.index >= pd.Timestamp("2023-01-01")])["sharpe"]
    return s


def fmt(v):
    return "-" if v is None or not np.isfinite(v) else f"{v*100:.1f}%"


def run_backtest(fwides, daily_ret, uni, list_dates, grid, rebal):
    sec = fws.Section(fwides, daily_ret, uni, list_dates)
    nav, turn, _ = fws.backtest(sec, fws.w_equal, grid, daily_ret, rebal)
    return nav, turn


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-phase", action="store_true")
    args = ap.parse_args()

    end = pd.Timestamp.today().strftime("%Y-%m-%d")
    conn = fe.get_conn()
    uni, list_dates = fe.load_universe_filter(conn)
    daily_ret = fe.load_daily_returns(conn, "2015-10-01", end)
    idx = daily_ret.index
    grid = fws.build_grid(idx, ANCHOR, BACK_TO, end)
    rebal = [t for t in grid if t >= pd.Timestamp(ANCHOR)]
    print(f"股票池 {len(uni)} | 同相位网格 {len(grid)} 期 | 回测 {len(rebal)} 期")

    names = NEW + REF + [n for n in PROD_OTHER if n not in REF]
    fw = {}

    # 新因子：直接在内存从 income 构造宽表。
    # ⚠ 不走 PG：9 个因子 × 11.8M 行 = 106M 行 upsert，实测单因子 >6 分钟；
    #    评估阶段完全不需要落库，只把最终胜出的因子入库即可。
    dsel = daily_ret.index[daily_ret.index >= pd.Timestamp("2015-06-01")]
    ev = cq.load_quarterly(conn)
    print(f"季度事件 {len(ev):,} 行 | {ev['ts_code'].nunique()} 只")
    for n in NEW:
        ev[n] = cq.clip_series(n, ev[n].to_numpy(dtype=float))
        fw[n] = cq.pit_wide(ev, dsel, n)
        fw[n] = fw[n].reindex(index=dsel, columns=daily_ret.columns).astype("float32")
        print(f"  构造 {n:12s} {fw[n].shape} 非空 {int(fw[n].notna().sum().sum()):,}")
    del ev
    gc.collect()

    for n in REF + PROD_OTHER:
        if n in fw:
            continue
        fw[n] = loadf(conn, n, "2015-06-01", end)
        print(f"  载入 {n} {fw[n].shape}")
    conn.close()

    # ---------------- 1. 单因子 IC ----------------
    sec_all = fws.Section(fw, daily_ret, uni, list_dates)
    ics = sec_all.ic(grid)
    print("\n=== 单因子 IC（同相位网格全期） ===")
    out_rows = []
    for n in names:
        ic = ics[n].dropna()
        r = {"factor": n, "ic": ic.mean(), "icir": ic.mean() / ic.std() if ic.std() else np.nan,
             "pos": (ic > 0).mean(), "n": len(ic)}
        out_rows.append(r)
        print(f"  {n:12s} IC {r['ic']*100:+.2f}%  ICIR {r['icir']:+.2f}  正率 {r['pos']*100:.0f}%")

    # ---------------- 2. 正交性 ----------------
    print("\n=== 正交性（与现有成长因子的截面秩相关均值） ===")
    corr_rows = []
    samp = rebal[::3]
    for n in NEW:
        row = {"factor": n}
        for ref in REF:
            cs = []
            for t in samp:
                if t not in fw[n].index or t not in fw[ref].index:
                    continue
                a = fw[n].loc[t].dropna()
                b = fw[ref].loc[t].dropna()
                ix = a.index.intersection(b.index)
                if len(ix) < 200:
                    continue
                cs.append(a[ix].corr(b[ix], method="spearman"))
            row[ref] = float(np.nanmean(cs)) if cs else np.nan
        corr_rows.append(row)
        print(f"  {n:12s} vs sue_gr {row['sue_gr']:+.3f}  vs sue_delta {row['sue_delta']:+.3f}")

    # ---------------- 3. 组合增益 ----------------
    variants = [
        ("prod6_baseline", PROD6),
        ("swap_delta->np", [x if x != "sue_delta" else "sue_q_np" for x in PROD6]),
        ("add_sue_q_np", PROD6 + ["sue_q_np"]),
        ("add_sue_np_or", PROD6 + ["sue_q_np", "sue_q_or"]),
        ("add_q_np_yoy", PROD6 + ["q_np_yoy"]),
        ("swap_delta->q_np_yoy", [x if x != "sue_delta" else "q_np_yoy" for x in PROD6]),
    ]
    print("\n=== 组合对照（主相位） ===")
    navs, results = {}, []
    for label, flist in variants:
        sub = {n: fw[n] for n in set(flist)}
        nav, turn = run_backtest(sub, daily_ret, uni, list_dates, grid, rebal)
        s = stats_full(nav)
        navs[label] = nav
        results.append({"label": label, "factors": flist, "ann": s["ann"], "sharpe": s["sharpe"],
                        "dd": s["dd"], "oos_sharpe": s["oos_sharpe"], "turn": turn})
        print(f"  {label:22s} 年化 {fmt(s['ann'])} 夏普 {s['sharpe']:.2f} 回撤 {fmt(s['dd'])} "
              f"OOS夏普 {s['oos_sharpe']:.2f} 换手 {turn*100:.0f}%")

    # ---------------- 4. 多相位稳健性 ----------------
    phase = {}
    if not args.no_phase:
        print(f"\n=== 多相位稳健性（offset {PHASE_OFFSETS}） ===")
        for label, flist in variants:
            if label not in ("prod6_baseline", "swap_delta->np", "add_sue_q_np",
                             "add_sue_np_or", "swap_delta->q_np_yoy"):
                continue
            sub = {n: fw[n] for n in set(flist)}
            anns = [stats_full(navs[label])["ann"]]
            for off in PHASE_OFFSETS:
                g = fws.offset_grid(idx, ANCHOR, off, end)
                rb = [t for t in g if t >= pd.Timestamp(ANCHOR)]
                nav_o, _ = run_backtest(sub, daily_ret, uni, list_dates, g, rb)
                anns.append(fws.stats(nav_o)["ann"])
            phase[label] = {"anns": anns, "mean": float(np.mean(anns)),
                            "worst": float(np.min(anns))}
            print(f"  {label:22s} 主 {fmt(anns[0])} | " +
                  " ".join(f"+{o}:{fmt(a)}" for o, a in zip(PHASE_OFFSETS, anns[1:])) +
                  f" | 均值 {fmt(phase[label]['mean'])} 最差 {fmt(phase[label]['worst'])}")

    # ---------------- 报告 ----------------
    L = ["# QFA 单季口径 + SUE 多子因子 评估", "",
         f"> 2026-09-14 | 组合层锁定生产口径 Top30 + min_var_cap10 + 60日协方差 + 单边成本 0.15%",
         f"> 同相位网格（锚 {ANCHOR}，IC 历史自 {BACK_TO} 延伸避跨期前视）| 回测 {len(rebal)} 期", "",
         "## 1. 单因子 IC（同相位网格全期）", "",
         "| 因子 | IC均值 | ICIR | IC正率 | 备注 |", "|---|---|---|---|---|"]
    for r in out_rows:
        note = "生产在用" if r["factor"] in PROD6 else ("现有参照" if r["factor"] in REF else "**新因子**")
        L.append(f"| {r['factor']} | {r['ic']*100:+.2f}% | {r['icir']:+.2f} | "
                 f"{r['pos']*100:.0f}% | {note} |")
    L += ["", "## 2. 正交性（vs 现有成长因子，截面秩相关均值）", "",
          "| 新因子 | vs sue_gr | vs sue_delta | 判定 |", "|---|---|---|---|"]
    for r in corr_rows:
        mx = max(abs(r["sue_gr"]), abs(r["sue_delta"]))
        L.append(f"| {r['factor']} | {r['sue_gr']:+.3f} | {r['sue_delta']:+.3f} | "
                 f"{'**冗余**' if mx > 0.8 else ('高相关' if mx > 0.5 else '独立')} |")
    L += ["", "## 3. 组合增益（主相位）", "",
          "| 方案 | 年化 | 夏普 | 回撤 | 样本外夏普 | 换手 | vs 基准年化 |", "|---|---|---|---|---|---|---|"]
    base_ann = results[0]["ann"]
    for r in results:
        L.append(f"| {r['label']} | {fmt(r['ann'])} | {r['sharpe']:.2f} | {fmt(r['dd'])} | "
                 f"{r['oos_sharpe']:.2f} | {r['turn']*100:.0f}% | "
                 f"{(r['ann']-base_ann)*100:+.1f}pp |")
    if phase:
        L += ["", "## 4. 多相位稳健性（offset 2/5/10/15 交易日，报年化）", "",
              "| 方案 | 主相位 | " + " | ".join(f"+{o}日" for o in PHASE_OFFSETS) +
              " | 多相位均值 | 最差 | 均值vs基准 |", "|---" * (4 + len(PHASE_OFFSETS)) + "|"]
        bm = phase["prod6_baseline"]["mean"]
        for label, p in phase.items():
            L.append(f"| {label} | " + " | ".join(fmt(a) for a in p["anns"]) +
                     f" | {fmt(p['mean'])} | {fmt(p['worst'])} | {(p['mean']-bm)*100:+.1f}pp |")
        L += ["", "> 判据：**多相位均值**提升 < 2pp 视为不成立（相位敏感度大于信号本身）。"]

    os.makedirs(os.path.join(REPO, "outputs"), exist_ok=True)
    with open(os.path.join(REPO, "outputs", "qfa_sue_eval.json"), "w") as f:
        json.dump({"ic": out_rows, "corr": corr_rows, "results": results,
                   "phase": {k: v for k, v in phase.items()}}, f, ensure_ascii=False, indent=2)
    outp = os.path.join(REPO, "reports", "qfa_sue_eval.md")
    with open(outp, "w") as f:
        f.write("\n".join(L) + "\n")
    print("\n" + "\n".join(L))
    print(f"\n✅ {outp}")


if __name__ == "__main__":
    main()

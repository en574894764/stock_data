#!/usr/bin/env python3
"""LightGBM 因子合成测试 (路线图 P1#2, 方法论依据 docs/alpha_prediction_research_2025.md
与 docs/beta可预测性与alpha方法横向调研_2026-09.md: 排序友好标签 + walk-forward + 防过拟合)
====================================================================================
设计 (参数先验设定, 不做 OOS 调参——避免数据窥探):
  特征   : factor_value 全部 21 因子 (8 原生 + 13 技术), T 日已知, LGBM 原生处理 NaN
  标签   : T+1..T+20 前向收益的**截面料分位 (rank percentile)** —— 与 Spearman IC 同构
  训练   : walk-forward 逐调仓期重训; 滚动 36 个月窗口; purge: 标签窗口须完全早于测试日
  预测   : 每个调仓日 t 预测全市场 → 复用 backtest_report.replay (同引擎同成本同选股,
           与 prod_6f_eq@topn=30 严格可比)
  裁决   : 2×2 对照 {baseline, LGBM} × {原始, 市值中性化}
           中性化 = score 对 ln_mv 截面线性回归取残差 (剔除市值方向暴露)。
           判定: LGBM 中性化后年化仍显著高于 baseline 中性化 = 真 α;
                 打回 baseline 水平 = 首轮增益是小盘 β 幻觉。

用法: python3 scripts/lgbm_synth_test.py [--features all|native] [--years 3]
输出: reports/lgbm_synth_test.md
"""
import argparse
import os
import sys

import numpy as np
import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import factor_eval as fe  # noqa: E402
import strategy_lib as sl  # noqa: E402
from backtest_report import replay  # noqa: E402

import lightgbm as lgb  # noqa: E402

START = "2019-01-01"
TRAIN_START = "2016-01-01"
TOPN = 30
NATIVE = ["ret_20d_rev", "turnover_20", "ln_mv", "ivol_60", "ep_ttm",
          "roe_lf", "sue_gr", "sue_delta"]
PARAMS = dict(objective="regression", n_estimators=400, learning_rate=0.03,
              num_leaves=31, min_child_samples=300, feature_fraction=0.7,
              bagging_fraction=0.7, bagging_freq=1, lambda_l1=0.1, lambda_l2=5.0,
              max_bin=63, verbosity=-1, n_jobs=8, seed=42)


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


def build_per_date(factors: dict, daily_ret, dates, fnames):
    """每个日期的 (特征矩阵 float32, 股票列表, 标签 rank_pct)。标签=前向20日收益截面料分位"""
    out = {}
    idx = daily_ret.index
    n_h = fe.REBAL
    for d in dates:
        p = idx.get_indexer([d])
        if len(p) == 0 or p[0] < 0 or p[0] + 1 + n_h > len(idx):
            continue
        seg = daily_ret.iloc[p[0] + 1: p[0] + 1 + n_h]
        fwd = ((1 + seg).prod() - 1).dropna()
        if len(fwd) < 200:
            continue
        stocks = fwd.index
        X = np.empty((len(stocks), len(fnames)), dtype=np.float32)
        for j, fn in enumerate(fnames):
            fw = factors[fn]
            X[:, j] = fw.loc[d].reindex(stocks).values if d in fw.index else np.nan
        y = fwd.rank(pct=True).values.astype(np.float32)
        out[d] = (X, stocks, y)
    return out


def neutralize(score: pd.Series, mv_row) -> pd.Series | None:
    """市值中性化: score 对 ln_mv 截面线性回归取残差。样本不足返回 None"""
    if score is None or mv_row is None:
        return None
    df = pd.concat([score.rename("s"), mv_row.rename("m")], axis=1).dropna()
    if len(df) < 50 or df["m"].std() < 1e-9:
        return None
    b, a = np.polyfit(df["m"].values, df["s"].values, 1)
    return pd.Series(df["s"].values - (a + b * df["m"].values), index=df.index)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--features", choices=["all", "native"], default="all")
    ap.add_argument("--years", type=int, default=3)
    args = ap.parse_args()
    end = pd.Timestamp.today().strftime("%Y-%m-%d")

    conn = fe.get_conn()
    uni, list_dates = fe.load_universe_filter(conn)
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT factor_name FROM factor_value ORDER BY 1")
    all_names = [r[0] for r in cur.fetchall()]
    cur.close()
    fnames = all_names if args.features == "all" else NATIVE
    factors = {n: sl.load_factor(conn, n, TRAIN_START, end) for n in fnames}
    # baseline 6 因子 (IC 对照需要)
    st_base = sl.load_strategies(conn, ["prod_6f_eq@topn=30"])[0]
    f6 = {n: factors[n] for n in st_base["cfg"]["factors"]}
    daily_ret = fe.load_daily_returns(conn, "2015-10-01", end)
    conn.close()

    idx = daily_ret.index
    test_dates = fe.rebalance_dates(idx, START, end)
    train_pool = fe.rebalance_dates(idx, TRAIN_START, end)
    print(f"特征 {len(fnames)} 个 | 测试期 {len(test_dates)} | 训练池 {len(train_pool)}")

    # 预构 per-date 特征/标签 (训练池 + 测试期都要特征; 测试期不需要标签但顺带算了, 不用)
    per_date = build_per_date(factors, daily_ret, sorted(set(train_pool) | set(test_dates)), fnames)
    pos_of = {d: i for i, d in enumerate(idx)}

    # walk-forward
    scores, scores_n, base_scores, base_scores_n = [], [], [], []
    ic_lgbm, ic_lgbm_n, ic_base, ic_base_n = [], [], [], []
    train_sizes, imp_acc = [], np.zeros(len(fnames))
    n_models = 0
    st_cfg = dict(st_base["cfg"])
    st_cfg.update({"factors": {"lgbm": 1.0}, "top_n": TOPN})
    st_lgbm = {"cfg": st_cfg}
    mv = factors["ln_mv"]

    for k, t in enumerate(test_dates):
        # 训练集: 滚动窗口 + purge (标签窗口完全早于 t)
        lo = pd.Timestamp(t) - pd.DateOffset(years=args.years)
        ds = [d for d in train_pool
              if lo <= d < t and pos_of[d] + fe.REBAL <= pos_of[t] and d in per_date]
        if len(ds) < 12:
            scores.append((t, None))
            scores_n.append((t, None))
            continue
        Xs = np.vstack([per_date[d][0] for d in ds])
        ys = np.concatenate([per_date[d][2] for d in ds])
        model = lgb.train(PARAMS, lgb.Dataset(Xs, label=ys))
        gi = np.array(model.feature_importance("gain"))
        if gi.sum() > 0:
            imp_acc += gi / gi.sum()
            n_models += 1
        train_sizes.append(len(ys))
        # 预测
        if t not in per_date:
            scores.append((t, None))
            scores_n.append((t, None))
            continue
        Xt, stocks_t, _ = per_date[t]
        pred = model.predict(Xt)
        ps = pd.Series(pred, index=stocks_t)
        scores.append((t, ps))
        mvt = mv.loc[t] if t in mv.index else None
        ps_n = neutralize(ps, mvt)
        scores_n.append((t, ps_n if ps_n is not None else ps))
        # IC 对照 (同日, 同 universe 过滤)
        keep = [c for c in stocks_t if c in uni and list_dates.get(c) is not None
                and t > pd.Timestamp(list_dates[c]) + pd.Timedelta(days=120)]
        seg_idx = idx.get_indexer([t])
        seg = daily_ret.iloc[seg_idx[0] + 1: seg_idx[0] + 1 + fe.REBAL]
        fwd = ((1 + seg).prod() - 1)
        sub_l = scores[-1][1].reindex(keep)
        fl = fwd.reindex(sub_l.index)
        m = sub_l.notna() & fl.notna()
        if m.sum() > 100:
            ic_lgbm.append((t, sub_l[m].rank().corr(fl[m].rank())))
        if ps_n is not None:
            sub_ln = ps_n.reindex(keep)
            fln = fwd.reindex(sub_ln.index)
            mn = sub_ln.notna() & fln.notna()
            if mn.sum() > 100:
                ic_lgbm_n.append((t, sub_ln[mn].rank().corr(fln[mn].rank())))
        bscore, _ = sl.score_cross(f6, st_base["cfg"]["factors"], t, uni, list_dates)
        if bscore is not None:
            base_scores.append((t, bscore))
            fb = fwd.reindex(bscore.index)
            mb = bscore.notna() & fb.notna()
            if mb.sum() > 100:
                ic_base.append((t, bscore[mb].rank().corr(fb[mb].rank())))
            bs_n = neutralize(bscore, mvt)
            base_scores_n.append((t, bs_n if bs_n is not None else bscore))
            if bs_n is not None:
                fbn = fwd.reindex(bs_n.index)
                mbn = bs_n.notna() & fbn.notna()
                if mbn.sum() > 100:
                    ic_base_n.append((t, bs_n[mbn].rank().corr(fbn[mbn].rank())))
        if (k + 1) % 10 == 0:
            print(f"  ...{k+1}/{len(test_dates)} 期, 最近IC lgbm={ic_lgbm[-1][1]:.3f} base={ic_base[-1][1]:.3f}")

    # 净值回放: 2×2 对照 (score 宽表 → 复用 replay, 同引擎同成本同选股)
    def to_wide(pairs):
        w = pd.DataFrame({t: s for t, s in pairs if s is not None}).T
        w.index.name = "trade_date"
        return w

    wide_b, wide_bn = to_wide(base_scores), to_wide(base_scores_n)
    wide_l, wide_ln = to_wide(scores), to_wide(scores_n)
    st_b = {"cfg": dict(st_base["cfg"], factors={"base_score": 1.0}, top_n=TOPN)}
    st_l = {"cfg": dict(st_base["cfg"], factors={"lgbm": 1.0}, top_n=TOPN)}
    runs = [
        ("baseline 6因子等权", st_b, "base_score", wide_b),
        ("baseline + 市值中性化", st_b, "base_score", wide_bn),
        (f"LightGBM-{len(fnames)}特征", st_l, "lgbm", wide_l),
        ("LightGBM + 市值中性化", st_l, "lgbm", wide_ln),
    ]
    results = []
    for lab, st, fname, w in runs:
        r = replay(st, {fname: w}, daily_ret, uni, list_dates, test_dates)
        results.append((lab, r, stats(r["nav"])))

    ic_b = pd.Series(dict(ic_base)).sort_index()
    ic_bn = pd.Series(dict(ic_base_n)).sort_index()
    ic_l = pd.Series(dict(ic_lgbm)).sort_index()
    ic_ln = pd.Series(dict(ic_lgbm_n)).sort_index()

    lines = [f"# LightGBM 因子合成 × 市值中性化 2×2 裁决 ({args.features} 特征, 滚动{args.years}年, {START} ~ {end})\n",
             f"- 特征 {len(fnames)} 个 (T 日已知) | 标签 = 前20日收益截面料分位 | LGBM 参数先验固定无 OOS 调参",
             f"- walk-forward 逐期重训, purge 标签窗口 | 平均训练样本 {np.mean(train_sizes)/1e3:.0f}k 行",
             "- 中性化 = score 对 ln_mv 截面线性回归取残差 (剔除市值方向暴露)\n",
             "## 净值口径 2×2 主表 (同引擎同成本)\n",
             "| 策略 | 年化 | 夏普 | 最大回撤 | 回撤谷底 |", "|---|---|---|---|---|"]
    for lab, rr, ss in results:
        ddn = rr["nav"] / rr["nav"].cummax() - 1
        lines.append(f"| {lab} | {fmt(ss['ann'])} | {ss['sharpe']:.2f} | {fmt(ss['dd'])} | {ddn.idxmin().date()} |")

    lines += ["\n## 年度收益 (净值口径)\n",
              "| 年份 | baseline | base中性 | LGBM | LGBM中性 |", "|---|---|---|---|---|"]
    for y in sorted(set(results[0][1]["nav"].index.year)):
        row = [str(y)]
        for _, rr, _ in results:
            seg = rr["nav"][rr["nav"].index.year == y]
            row.append(fmt(seg.iloc[-1] / seg.iloc[0] - 1) if len(seg) > 20 else "-")
        lines.append("| " + " | ".join(row) + " |")

    lines += ["\n## IC 口径 2×2\n", "| 模型 | RankIC 均值 | ICIR | IC>0 占比 | 期数 |", "|---|---|---|---|---|"]
    for lab, ic in [("6因子等权", ic_b), ("6因子 + 中性化", ic_bn),
                    (f"LightGBM-{len(fnames)}", ic_l), ("LightGBM + 中性化", ic_ln)]:
        if len(ic) > 0:
            lines.append(f"| {lab} | {ic.mean()*100:.1f}% | {ic.mean()/ic.std():.2f} | {(ic>0).mean()*100:.0f}% | {len(ic)} |")

    lines.append("\n## 分年 RankIC (LGBM 原始 vs 中性化)\n\n| 年份 | LGBM | LGBM中性 | baseline | 期数 |\n|---|---|---|---|---|")
    for y in sorted(set(ic_l.index.year)):
        sub_l = ic_l[ic_l.index.year == y]
        sub_ln = ic_ln[ic_ln.index.year == y] if len(ic_ln) > 0 else pd.Series(dtype=float)
        sub_b = ic_b[ic_b.index.year == y]
        lines.append(f"| {y} | {sub_l.mean()*100:.1f}% | {sub_ln.mean()*100:.1f}% | {sub_b.mean()*100:.1f}% | {len(sub_l)} |")

    # 特征重要性 (gain 归一化, 跨期平均)
    imp = imp_acc / max(n_models, 1)
    top = pd.Series(imp, index=fnames).sort_values(ascending=False).head(12)
    lines.append("\n## 特征重要性 Top12 (gain, 跨期平均)\n\n| 因子 | 重要性 |\n|---|---|")
    for k_, v in top.items():
        lines.append(f"| {k_} | {v*100:.1f}% |")

    # 小盘暴露诊断: 4 组 Top30 持仓中位总市值(亿元)
    # (factor ln_mv = -ln(总市值[万元]), 方向已按约定翻转: 值越大=市值越小)
    def med_mv(w):
        vals = []
        for t, row in w.iterrows():
            keep = [c for c in row.index if c in uni and list_dates.get(c) is not None
                    and t > pd.Timestamp(list_dates[c]) + pd.Timedelta(days=120)]
            sel, _ = sl.select_stocks(row.reindex(keep).dropna(), TOPN)
            if sel and t in mv.index:
                v = mv.loc[t].reindex(sel).median()
                if pd.notna(v):
                    vals.append(np.exp(-v) / 1e4)  # 万元 → 亿元
        return np.nanmean(vals) if vals else np.nan

    lines += ["\n## 小盘暴露诊断 (Top30 持仓中位总市值, 亿元)\n",
              "| 组 | 中位市值(亿) |", "|---|---|"]
    for (lab, _, _), w in zip(results, [wide_b, wide_bn, wide_l, wide_ln]):
        lines.append(f"| {lab} | {med_mv(w):.0f} |")

    # 裁决
    a_b, a_bn, a_l, a_ln = (ss["ann"] for _, _, ss in results)
    dd_l, dd_ln = results[2][2]["dd"], results[3][2]["dd"]
    lines += ["\n## 裁决 (判定标准: 中性化后仍显著跑赢 = 真 α; 打回 baseline = 小盘 β 幻觉)\n",
              f"- LGBM 原始 {fmt(a_l)} → 中性化后 {fmt(a_ln)} (中性化损失 {abs(a_l-a_ln)*100:.1f}pp)",
              f"- baseline 原始 {fmt(a_b)} → 中性化后 {fmt(a_bn)} (中性化损失 {abs(a_b-a_bn)*100:.1f}pp)",
              f"- 中性化口径下 LGBM 对 baseline 超额: {(a_ln-a_bn)*100:+.1f}pp",
              f"- LGBM 回撤: 原始 {fmt(dd_l)} → 中性化后 {fmt(dd_ln)}",
              f"- 结论: {'中性化后仍显著跑赢 → 增益含真实 α 成分' if a_ln - a_bn > 0.03 else '中性化后超额消失 → 首轮增益主要是小盘 β 暴露'}"]

    out = os.path.join(REPO, "reports", "lgbm_synth_test.md")
    with open(out, "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines[:30]))
    print(f"\n✅ 报告: {out}")


if __name__ == "__main__":
    main()

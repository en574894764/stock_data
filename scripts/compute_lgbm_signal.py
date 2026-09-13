#!/usr/bin/env python3
"""LGBM 合成信号生产固化 (P1#2 裁决 → 生产候选)
====================================================================================
把 lgbm_synth_test 的 walk-forward 训练 + 预测 + 市值中性化 固化为 factor_value 的
`lgbm_score` 因子。写入后即可被 generate_signals / factor_eval / backtest_report 复用
(单因子 z-score = 单调变换 = 排序不变)。

设计 (与 lgbm_synth_test 严格一致, 参数先验固定无 OOS 调参):
  特征 : 全部因子 (factor_value DISTINCT, 排除 lgbm_score 自身)
  标签 : T+1..T+20 前向收益截面料分位
  训练 : walk-forward 逐调仓日重训; 滚动 36 个月; purge 标签窗口
  中性化 : score 对 ln_mv 截面线性回归取残差 (剔除市值方向暴露, 2026-09-11 2×2 裁决)

用法:
    python3 scripts/compute_lgbm_signal.py --backfill   # 全历史回填 (2019 起, 回测验证)
    python3 scripts/compute_lgbm_signal.py              # 增量: 最新一个调仓日 (pipeline 每日)
    python3 scripts/compute_lgbm_signal.py --dry-run    # 预览最新调仓日, 不落库
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

import lightgbm as lgb  # noqa: E402

FACTOR_NAME = "lgbm_score"
START = "2019-01-01"
TRAIN_START = "2016-01-01"

# 特征排除表: 入库但已裁决「不参与 LGBM 训练」的实验因子。
# 背景: 特征集 = factor_value 全部因子 (自动发现), 这导致任何实验因子入库即自动成为
#       生产特征 → 必须显式排除, 否则污染生产。
# 2026-09-12 隔夜-日内结构因子 8 个: 等权池无增益; 30 特征重训致 OOS 夏普 1.56→1.11、
#   三年窗口 21.0%→13.1%、年化 22.1%→18.2% (冗余: id_mom_20 vs ret_20d_rev 相关 0.83、
#   on_vol_20 vs ivol_60 相关 0.64; 噪声: tug_20/on_skew_20)。详见 reports/intraday_pool_test.md
EXCLUDE_FACTORS = {"on_mom_20", "id_mom_20", "on_share_20", "id_on_amp_20",
                   "on_vol_20", "on_skew_20", "on_rev_5", "tug_20"}
# 2026-09-14 QFA 单季 / SUE 多子因子: 组合层实证无增益 (新增 sue_q_np 多相位均值 +0.0pp;
#   替换 sue_delta→sue_q_np 反而 -4.7pp 年化)，见 reports/qfa_sue_eval.md。
#   单因子 IC 虽高于现有 sue_gr/sue_delta（+1.9~2.2% vs +0.85/+1.38%），但边际信息被现有因子吸收。
#   仅 q_np_yoy 曾入库（脚本首次试跑遗留），登记排除以免自动进入 LGBM 生产特征；
#   其余 8 个未落库（按需 `python3 scripts/compute_qfa_sue.py --start 2015-01-01` 写入）。
#   若要做「新因子是否提升 LGBM」实验，须先复制本表去掉 q_np_yoy 再重训对比。
EXCLUDE_FACTORS |= {"q_np_yoy"}

PARAMS = dict(objective="regression", n_estimators=400, learning_rate=0.03,
              num_leaves=31, min_child_samples=300, feature_fraction=0.7,
              bagging_fraction=0.7, bagging_freq=1, lambda_l1=0.1, lambda_l2=5.0,
              max_bin=63, verbosity=-1, n_jobs=8, seed=42)


def neutralize(score: pd.Series, mv_row) -> pd.Series | None:
    """score 对 ln_mv 截面线性回归取残差 (市值中性化)."""
    if score is None or mv_row is None:
        return None
    df = pd.concat([score.rename("s"), mv_row.rename("m")], axis=1).dropna()
    if len(df) < 50 or df["m"].std() < 1e-9:
        return None
    b, a = np.polyfit(df["m"].values, df["s"].values, 1)
    return pd.Series(df["s"].values - (a + b * df["m"].values), index=df.index)


def load_factor_names(conn) -> list:
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT factor_name FROM factor_value ORDER BY 1")
    names = [r[0] for r in cur.fetchall()]
    cur.close()
    return [n for n in names if n != FACTOR_NAME and n not in EXCLUDE_FACTORS]


def fit_predict(factors, daily_ret, mv, train_ds, t, fnames) -> pd.Series | None:
    """滚动窗口训练 + 预测 t 截面 + 中性化。返回 score Series (index=ts_code) 或 None。

    预测只依赖 t 截面特征 (t 日有行情的股票); t 的前向收益仅训练标签需要 (历史完整)。"""
    n_h = fe.REBAL
    idx = daily_ret.index
    if t not in idx:
        return None
    # 预测股票池: t 日有行情的股票
    stocks_t = daily_ret.loc[t].dropna().index
    if len(stocks_t) < 200:
        return None
    Xt = np.empty((len(stocks_t), len(fnames)), dtype=np.float32)
    for j, fn in enumerate(fnames):
        fw = factors[fn]
        Xt[:, j] = fw.loc[t].reindex(stocks_t).values if t in fw.index else np.nan

    # 训练集 (滚动窗口 + purge, 需前向收益作标签)
    Xs, ys = [], []
    for d in train_ds:
        if d not in factors[fnames[0]].index:
            continue
        pd_ = idx.get_indexer([d])
        if len(pd_) == 0 or pd_[0] < 0 or pd_[0] + 1 + n_h > len(idx):
            continue
        seg_d = daily_ret.iloc[pd_[0] + 1: pd_[0] + 1 + n_h]
        fwd_d = ((1 + seg_d).prod() - 1).dropna()
        if len(fwd_d) < 200:
            continue
        stocks_d = fwd_d.index
        Xd = np.empty((len(stocks_d), len(fnames)), dtype=np.float32)
        for j, fn in enumerate(fnames):
            fw = factors[fn]
            Xd[:, j] = fw.loc[d].reindex(stocks_d).values if d in fw.index else np.nan
        Xs.append(Xd)
        ys.append(fwd_d.rank(pct=True).values.astype(np.float32))
    if len(Xs) < 12:
        return None
    X = np.vstack(Xs)
    y = np.concatenate(ys)
    model = lgb.train(PARAMS, lgb.Dataset(X, label=y))
    pred = pd.Series(model.predict(Xt), index=stocks_t)
    mvt = mv.loc[t] if t in mv.index else None
    out = neutralize(pred, mvt)
    return out if out is not None else pred


def upsert(conn, trade_date, score: pd.Series):
    """写入 factor_value (单截面, execute_values 足够)."""
    rows = [(trade_date.date(), c, FACTOR_NAME, float(v))
            for c, v in score.items() if pd.notna(v)]
    if not rows:
        return 0
    from psycopg2.extras import execute_values
    cur = conn.cursor()
    execute_values(cur, """INSERT INTO factor_value (trade_date, ts_code, factor_name, value)
        VALUES %s ON CONFLICT (factor_name, trade_date, ts_code) DO UPDATE SET value=EXCLUDED.value""",
        rows)
    conn.commit()
    cur.close()
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true", help="全历史回填 2019 起所有调仓日")
    ap.add_argument("--years", type=int, default=3, help="滚动训练窗口年数")
    ap.add_argument("--dry-run", action="store_true", help="只预览最新调仓日, 不落库")
    args = ap.parse_args()
    end = pd.Timestamp.today().strftime("%Y-%m-%d")

    conn = fe.get_conn()
    fnames = load_factor_names(conn)
    factors = {n: sl.load_factor(conn, n, TRAIN_START, end) for n in fnames}
    mv = factors["ln_mv"] if "ln_mv" in factors else sl.load_factor(conn, "ln_mv", TRAIN_START, end)
    daily_ret = fe.load_daily_returns(conn, "2015-10-01", end)
    conn.close()

    idx = daily_ret.index
    test_dates = fe.rebalance_dates(idx, START, end)
    train_pool = fe.rebalance_dates(idx, TRAIN_START, end)
    pos_of = {d: i for i, d in enumerate(idx)}

    if args.backfill:
        targets = test_dates
        print(f"全历史回填: {len(targets)} 个调仓日 ({targets[0].date()} ~ {targets[-1].date()})")
    else:
        # 增量: 算最新交易日 (今日为交易日时), 供 generate_signals 调仓日读最新值
        conn = fe.get_conn()
        cur = conn.cursor()
        cur.execute("SELECT MAX(trade_date) FROM factor_value WHERE factor_name=%s", (FACTOR_NAME,))
        last = cur.fetchone()[0]
        cur.close()
        conn.close()
        latest = idx[-1]  # 最新交易日
        if last is not None and latest <= pd.Timestamp(last):
            targets = []
            print(f"增量: {FACTOR_NAME} 已最新 ({last}), 跳过")
        else:
            targets = [latest]
            print(f"增量: 算最新交易日 {latest.date()}")

    written = 0
    for k, t in enumerate(targets):
        lo = pd.Timestamp(t) - pd.DateOffset(years=args.years)
        train_ds = [d for d in train_pool
                    if lo <= d < t and pos_of[d] + fe.REBAL <= pos_of[t]]
        score = fit_predict(factors, daily_ret, mv, train_ds, t, fnames)
        if score is None or len(score) < 100:
            print(f"  {t.date()}: 跳过 (训练/截面不足)")
            continue
        if args.dry_run:
            print(f"  [dry-run] {t.date()}: 预测 {len(score)} 只, "
                  f"均值 {score.mean():.4f} 标准差 {score.std():.4f}")
            continue
        conn = fe.get_conn()
        n = upsert(conn, t, score)
        conn.close()
        written += n
        if (k + 1) % 10 == 0:
            print(f"  ...{k+1}/{len(targets)} 期 ({t.date()}: {n} 只)")

    print(f"✅ {FACTOR_NAME} 完成: {len(targets)} 期, 写入 {written} 行" + (" [dry-run, 未落库]" if args.dry_run else ""))


if __name__ == "__main__":
    main()

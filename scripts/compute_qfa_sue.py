#!/usr/bin/env python3
"""QFA 单季口径 + SUE 多子因子 → PG factor_value
====================================================================================
动机（来自 factor_weight_scan 的裁决）
  现有 sue_gr   = 最新披露 netprofit_yoy（**累计**同比）
  现有 sue_delta= 同类型财报 netprofit_yoy 的环比差（IC 仅 1.4%，六因子里最弱）
  问题直指口径：netprofit_yoy 是 **年内累计同比**（Q3 的同比被前两季拖累），
  而 sue_delta 用「上一期累计同比」作期望基准 = **线性外推**，既不干净也不稳。
  华泰实证：**单季口径优于 TTM/累计口径**；SUE 应用**一致预期**作基准。

本脚本（无分析师一致预期数据，用学术标准的季节性随机游走 SRW 作基准 —— 已有实证
表明 SRW 在无覆盖股票上对一致预期的预测力是接近的）

数据与口径
  源：income（利润表），report_type ∈ {'1','2','3','4'} 表示**报告期**（Q1/H1/Q3/年报），
      total_revenue / n_income_attr_p / operate_profit 均为**年内累计**值。
  PIT：按 ann_date 对齐，同一 (ts_code, report_year, report_type) 有多条披露时**取最早公告**
      （避免用后续更正/重述数据 → 无前视）。
  单季还原：q1 = cum1；qk = cum_k − cum_{k−1}（k≥2，且必须季度连续）。
  同比：q_t / q_{t−4} − 1（必须严格间隔 4 个季度）。
  SUE：对「相对惊喜」s_t = (q_t − q_{t−4}) / |q_{t−4}| 做自身历史标准化
        SRW 版   SUE = s_t / σ(过去8季 s)              基准 = 去年同季
        drift 版 SUE = (s_t − μ(过去8季 s)) / σ(过去8季 s)  基准 = 去年同季 × 历史平均增速
      与线性外推（sue_delta）的区别：基准锚在**去年同季**（季节对齐），
      漂移项由自身 8 季历史估计，而不是"上一期累计值"。

产出因子（全部「值大 = 预期收益高」，与 factor_value 约定一致）
  q_np_yoy    单季归母净利同比
  q_or_yoy    单季营收同比
  q_op_yoy    单季营业利润同比
  q_roe_d     单季 ROE 差分（financial_indicator.roe 年内累计值差分）
  sue_q_np    SUE(单季归母净利, SRW)
  sue_q_or    SUE(单季营收, SRW)
  sue_q_op    SUE(单季营业利润, SRW)
  sue_q_np_d  SUE(单季归母净利, 带漂移)
  q_acc_np    单季净利同比的加速度 = yoy_t − yoy_{t−1}

⚠ 截断：值按经济合理区间 clip（yoy ±5 = ±500%，SUE ±15），否则截面 z 被极端值绑架
  （现有 sue_gr 截面 |z|max 达 66σ，ep_ttm 22σ）。截断后 upsert。

⚠ 入库后 LGBM 会**自动把它当生产特征**（compute_lgbm_signal 按 factor_value DISTINCT
  factor_name 自动发现）。裁决前必须在 compute_lgbm_signal.EXCLUDE_FACTORS 登记，
  或确认后正式纳入。

用法
  python3 scripts/compute_qfa_sue.py --dry-run          # 只算不写，打印覆盖统计
  python3 scripts/compute_qfa_sue.py --start 2015-01-01 # 全量写入
  python3 scripts/compute_qfa_sue.py                    # 增量（最近10个交易日）
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd
import psycopg2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SRC_START_YEAR = 2009      # 需要 4 季(同比) + 8 季(SUE 历史) ≈ 3 年前置
CLIP = {"yoy": 5.0, "sue": 15.0, "roe_d": 30.0, "acc": 10.0}
MIN_SURPRISE_HIST = 6      # SUE 至少需要 6 个历史惊喜点

FACTORS = ["q_np_yoy", "q_or_yoy", "q_op_yoy", "q_roe_d", "q_acc_np",
           "sue_q_np", "sue_q_or", "sue_q_op", "sue_q_np_d"]


def get_conn():
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "/tmp"), port=os.environ.get("PGPORT", "5432"),
        dbname=os.environ.get("PGDATABASE", "investassist"),
        user=os.environ.get("PGUSER", "james"), password=os.environ.get("PGPASSWORD", ""))


def load_quarterly(conn) -> pd.DataFrame:
    """利润表 → 单季还原 + 同比 + SUE。返回 long: ts_code, ann_date, <各因子列>"""
    df = pd.read_sql(
        f"""SELECT ts_code, report_year, report_type, ann_date,
                   total_revenue, n_income_attr_p, operate_profit
            FROM income
            WHERE ann_date IS NOT NULL AND report_type IN ('1','2','3','4')
              AND (ts_code LIKE '%%.SZ' OR ts_code LIKE '%%.SH')
              AND report_year >= {SRC_START_YEAR}""", conn)
    df["ann_date"] = pd.to_datetime(df["ann_date"])
    df["q"] = df["report_type"].astype(int)
    df["qi"] = df["report_year"].astype(int) * 4 + (df["q"] - 1)   # 连续季度索引
    # 同季度多次披露 → 取最早公告（原披露），避免重述前视
    df = df.sort_values(["ts_code", "qi", "ann_date"]).drop_duplicates(["ts_code", "qi"], keep="first")
    df = df.sort_values(["ts_code", "qi"]).reset_index(drop=True)

    for col, new in [("total_revenue", "q_or"), ("n_income_attr_p", "q_np"), ("operate_profit", "q_op")]:
        g = df.groupby("ts_code")[col]
        prev, prev_qi = g.shift(1), df.groupby("ts_code")["qi"].shift(1)
        cont = prev_qi == df["qi"] - 1
        df[new] = np.where(df["q"] == 1, df[col], np.where(cont, df[col] - prev, np.nan))

    # 同比（严格间隔 4 季）
    for c in ["q_np", "q_or", "q_op"]:
        g = df.groupby("ts_code")[c]
        base, base_qi = g.shift(4), df.groupby("ts_code")["qi"].shift(4)
        gap4 = df["qi"] - base_qi == 4
        df[c + "_yoy"] = np.where(gap4 & base.notna() & (base.abs() > 1e-6),
                                  df[c] / base - 1.0, np.nan)
        # 相对惊喜（SRW 基准 = 去年同季）
        df[c + "_s"] = np.where(gap4 & base.notna() & (base.abs() > 1e-6),
                                (df[c] - base) / base.abs(), np.nan)

    # SUE：自身历史标准化（过去 8 季，排除当期）
    for c in ["q_np", "q_or", "q_op"]:
        s = df.groupby("ts_code")[c + "_s"]
        mu = s.transform(lambda x: x.shift(1).rolling(8, min_periods=MIN_SURPRISE_HIST).mean())
        sd = s.transform(lambda x: x.shift(1).rolling(8, min_periods=MIN_SURPRISE_HIST).std())
        df[c + "_sue"] = np.where(sd > 1e-6, df[c + "_s"] / sd, np.nan)
        df[c + "_sue_d"] = np.where(sd > 1e-6, (df[c + "_s"] - mu) / sd, np.nan)

    # 同比加速度（单季同比的一阶差分，季节已对齐）
    df["q_acc_np"] = df.groupby("ts_code")["q_np_yoy"].diff()

    # ROE 单季差分（financial_indicator.roe 为年内累计口径）
    roe = pd.read_sql(
        f"""SELECT ts_code, report_year, report_type, ann_date, roe
            FROM financial_indicator
            WHERE ann_date IS NOT NULL AND roe IS NOT NULL AND report_type IN ('1','2','3','4')
              AND (ts_code LIKE '%%.SZ' OR ts_code LIKE '%%.SH')
              AND report_year >= {SRC_START_YEAR}""", conn)
    roe["ann_date"] = pd.to_datetime(roe["ann_date"])
    roe["q"] = roe["report_type"].astype(int)
    roe["qi"] = roe["report_year"].astype(int) * 4 + (roe["q"] - 1)
    roe = roe.sort_values(["ts_code", "qi", "ann_date"]).drop_duplicates(["ts_code", "qi"], keep="first")
    roe = roe.sort_values(["ts_code", "qi"])
    prev_qi = roe.groupby("ts_code")["qi"].shift(1)
    prev_roe = roe.groupby("ts_code")["roe"].shift(1)
    roe["q_roe_d"] = np.where(roe["q"] == 1, roe["roe"],
                              np.where(prev_qi == roe["qi"] - 1, roe["roe"] - prev_roe, np.nan))
    df = df.merge(roe[["ts_code", "qi", "q_roe_d"]], on=["ts_code", "qi"], how="left")

    out = df[["ts_code", "ann_date", "q_np_yoy", "q_or_yoy", "q_op_yoy", "q_roe_d", "q_acc_np",
              "q_np_sue", "q_or_sue", "q_op_sue", "q_np_sue_d"]].copy()
    out.columns = ["ts_code", "ann_date", "q_np_yoy", "q_or_yoy", "q_op_yoy", "q_roe_d", "q_acc_np",
                   "sue_q_np", "sue_q_or", "sue_q_op", "sue_q_np_d"]
    return out.dropna(subset=FACTORS, how="all").sort_values(["ts_code", "ann_date"])


def clip_series(name: str, v: np.ndarray) -> np.ndarray:
    if name in ("sue_q_np", "sue_q_or", "sue_q_op", "sue_q_np_d"):
        lim = CLIP["sue"]
    elif name == "q_acc_np":
        lim = CLIP["acc"]
    elif name == "q_roe_d":
        lim = CLIP["roe_d"]
    else:
        lim = CLIP["yoy"]
    return np.clip(v, -lim, lim)


def pit_wide(ev: pd.DataFrame, dates: pd.Index, name: str) -> pd.DataFrame:
    """事件流 (ts_code, ann_date, value) → 宽表 (dates × ts_code)，ann_date <= T 取最后一条。

    性能: 预分配 numpy 再按列切片赋值（逐列 DataFrame 赋值在 5500 列 × 2800 行下会慢 10 倍以上）。"""
    ev = ev[["ts_code", "ann_date", name]].dropna()
    codes = ev["ts_code"].unique()
    cpos = {c: i for i, c in enumerate(codes)}
    ts_idx = pd.to_datetime(pd.Index(dates)).values
    out = np.full((len(dates), len(codes)), np.nan, dtype=np.float64)
    for code, g in ev.groupby("ts_code", sort=False):
        ga = g["ann_date"].values
        gv = g[name].to_numpy(dtype=np.float64)
        pos = np.searchsorted(ga, ts_idx, side="right") - 1
        valid = pos >= 0
        if valid.any():
            out[valid, cpos[code]] = gv[np.clip(pos[valid], 0, len(gv) - 1)]
    return pd.DataFrame(out, index=dates, columns=codes)


def upsert_factor(conn, name: str, wide: pd.DataFrame, only_dates=None):
    import tempfile
    if only_dates is not None:
        wide = wide.loc[wide.index.isin(only_dates)]
    wide = wide.replace([np.inf, -np.inf], np.nan)
    try:
        st = wide.stack(future_stack=True)      # pandas>=2.1，NaN 自动丢弃
    except TypeError:
        st = wide.stack(dropna=True)
    if st.empty:
        return 0
    df = st.rename("value").reset_index()
    df.columns = ["trade_date", "ts_code", "value"]
    df["factor_name"] = name
    n = len(df)
    df = df[["ts_code", "trade_date", "factor_name", "value"]]
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS _staging_factor (LIKE factor_value INCLUDING DEFAULTS)")
    cur.execute("TRUNCATE _staging_factor")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as tf:
        path = tf.name
    try:
        # 注意: 不要传 float_format —— 一旦指定，pandas 会掉出 C 快速路径，
        # 逐行 Python 格式化，11.8M 行要多花 5 倍以上时间。
        df.to_csv(path, header=False, index=False)
        with open(path) as f:
            cur.copy_expert("COPY _staging_factor (ts_code, trade_date, factor_name, value) "
                            "FROM STDIN WITH CSV", f)
        cur.execute("INSERT INTO factor_value SELECT * FROM _staging_factor "
                    "ON CONFLICT (factor_name, trade_date, ts_code) DO UPDATE SET value=EXCLUDED.value")
        conn.commit()
    finally:
        if os.path.exists(path):
            os.unlink(path)
    cur.close()
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None, help="全量起始日 (默认增量: 最近10个交易日)")
    ap.add_argument("--dry-run", action="store_true", help="只算不写")
    args = ap.parse_args()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT trade_date FROM daily_quote "
                "WHERE (ts_code LIKE '%.SZ' OR ts_code LIKE '%.SH') ORDER BY trade_date")
    all_dates = pd.DatetimeIndex([r[0] for r in cur.fetchall()])
    cur.close()

    if args.start:
        dates = all_dates[all_dates >= pd.Timestamp(args.start)]
        only_dates = None
    else:
        # 增量: PIT 对齐只依赖目标日自身 (ann_date <= T)，故只需构造最近 10 个交易日的宽表
        dates = all_dates[-10:]
        only_dates = set(dates)
        print(f"增量模式: 回写 {dates.min().date()} ~ {dates.max().date()}")

    print(f"交易日 {len(dates)} 天 ({dates.min().date()} ~ {dates.max().date()})")
    ev = load_quarterly(conn)
    print(f"季度事件 {len(ev):,} 行 | {ev['ts_code'].nunique()} 只 | "
          f"公告 {ev['ann_date'].min().date()} ~ {ev['ann_date'].max().date()}")
    cov = ev[FACTORS].notna().mean().round(4)
    print("非空覆盖率:\n" + cov.to_string())

    if args.dry_run:
        conn.close()
        print("\n[dry-run] 未写入")
        return

    for name in FACTORS:
        ev[name] = clip_series(name, ev[name].to_numpy(dtype=float))
        wide = pit_wide(ev, dates, name)
        n = upsert_factor(conn, name, wide, only_dates)
        print(f"  {name:12s} {n:>12,} 行  最新日非空 {int(wide.iloc[-1].notna().sum())} 只")
        del wide

    cur = conn.cursor()
    cur.execute("SELECT factor_name, COUNT(*), MIN(trade_date), MAX(trade_date) FROM factor_value "
                "WHERE factor_name = ANY(%s) GROUP BY 1 ORDER BY 1", (FACTORS,))
    print("\n=== 新因子入库现状 ===")
    for r in cur.fetchall():
        print(f"  {r[0]:<12} {r[1]:>12,} 行  {r[2]} ~ {r[3]}")
    cur.close()
    conn.close()
    print("完成")


if __name__ == "__main__":
    main()

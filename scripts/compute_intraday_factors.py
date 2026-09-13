#!/usr/bin/env python3
"""隔夜-日内结构因子 → PG factor_value 表
====================================================================================
来源: 中信建投《"逐鹿"Alpha 专题(二十九)——隔夜-日内异象因子及领先滞后分析》(2025-11)
A 股独有"隔夜负收益之谜": 平均隔夜收益(C2O)显著为负, 日内收益(O2C)正溢价;
传统日频因子把该结构差异平均掉了。本脚本把日收益拆为隔夜/日内两段后派生因子。

收益拆分 (复权口径, 无除权污染):
  隔夜收益 on_t = open_t / pre_close_t - 1   (pre_close 为除权调整后前收盘 → 纯净隔夜)
  日内收益 id_t = close_t / open_t - 1
  校验: close_t/pre_close_t - 1 ≡ pct_chg_t/100 (脚本启动时校验一致率)

8 个因子 (4 维度 × 2):
  动量类: on_mom_20(隔夜动量) / id_mom_20(日内动量)
  结构类: on_share_20(隔夜收益占比) / id_on_amp_20(日内隔夜幅度比)
  波动类: on_vol_20(隔夜波动) / on_skew_20(隔夜偏度)
  反转类: on_rev_5(短期隔夜反转) / tug_20(拔河系数: 隔夜-日内相关性)

方向: 约定 值大=预期收益高。方向由**样本内 (2016-2022) IC 符号**决定 (无前视),
      样本外 (2023+) 仅用于验证。

用法:
  python3 scripts/compute_intraday_factors.py --start 2015-01-01   # 全量
  python3 scripts/compute_intraday_factors.py                      # 增量: 补最近 10 个交易日
  python3 scripts/compute_intraday_factors.py --no-write           # 只算不写 (诊断)
"""
import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.compute_factors import get_conn, upsert_factor  # noqa: E402

FACTORS = ["on_mom_20", "id_mom_20", "on_share_20", "id_on_amp_20",
           "on_vol_20", "on_skew_20", "on_rev_5", "tug_20"]

IN_START, IN_END = "2016-01-01", "2022-12-31"   # 样本内 (定方向)
OUT_START = "2023-01-01"                        # 样本外 (验证)


def load_ohlc(conn, start: str):
    """加载 open/pre_close/close/pct_chg → 4 个宽表"""
    sql = ("SELECT ts_code, trade_date, pre_close, open, close, pct_chg FROM daily_quote "
           f"WHERE trade_date >= '{start}' AND (ts_code LIKE '%%.SZ' OR ts_code LIKE '%%.SH')")
    df = pd.read_sql(sql, conn)
    for c in ["pre_close", "open", "close", "pct_chg"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    out = {}
    for c in ["pre_close", "open", "close", "pct_chg"]:
        w = df.pivot_table(index="trade_date", columns="ts_code",
                           values=c, aggfunc="last").sort_index()
        w.index = pd.to_datetime(w.index)   # 必须: pivot 后 index 为 date 对象, 与 Timestamp 不匹配
        out[c] = w
    return out


def check_adjustment(ohlc: dict) -> None:
    """校验 pre_close 为除权调整口径: close/pre_close-1 应 ≈ pct_chg/100"""
    pc, cl, pct = ohlc["pre_close"], ohlc["close"], ohlc["pct_chg"]
    valid = (pc > 0) & cl.notna() & pct.notna()
    derived = (cl / pc.where(pc > 0) - 1.0) - pct / 100.0
    d = derived.where(valid)
    n = int(d.notna().sum().sum())
    bad = int((d.abs() > 0.005).sum().sum())
    print(f"  pre_close 口径校验: {n:,} 个有效格, 偏离>0.5pp 的 {bad:,} 个 ({bad/max(n,1)*100:.3f}%)")
    if bad / max(n, 1) > 0.001:
        print("  ⚠ 偏离比例偏高: pre_close 可能含未复权记录, 隔夜收益会被除权跳空污染")


def build_factors(ohlc: dict) -> dict:
    """返回 {因子名: 原始方向宽表}"""
    on = ohlc["open"] / ohlc["pre_close"].where(ohlc["pre_close"] > 0) - 1.0   # 隔夜
    idr = ohlc["close"] / ohlc["open"].where(ohlc["open"] > 0) - 1.0           # 日内
    on = on.clip(-0.35, 0.35)     # 极端值截断 (ST/异常报价)
    idr = idr.clip(-0.35, 0.35)

    on_sum20 = on.rolling(20, min_periods=15).sum()
    id_sum20 = idr.rolling(20, min_periods=15).sum()
    abs_on20 = on.abs().rolling(20, min_periods=15).sum()
    abs_id20 = idr.abs().rolling(20, min_periods=15).sum()

    out = {}
    # 动量类
    out["on_mom_20"] = on_sum20
    out["id_mom_20"] = id_sum20
    # 结构类
    out["on_share_20"] = abs_on20 / (abs_on20 + abs_id20).replace(0, np.nan)
    out["id_on_amp_20"] = abs_id20 / abs_on20.replace(0, np.nan)
    # 波动类
    out["on_vol_20"] = on.rolling(20, min_periods=15).std()
    out["on_skew_20"] = on.rolling(20, min_periods=15).skew()
    # 反转类
    out["on_rev_5"] = on.rolling(5, min_periods=4).sum()
    # 拔河系数: 20 日 corr(隔夜, 日内), 手算避免版本差异
    m_on, m_id = on.rolling(20, min_periods=15).mean(), idr.rolling(20, min_periods=15).mean()
    cov = (on * idr).rolling(20, min_periods=15).mean() - m_on * m_id
    sd_on, sd_id = on.rolling(20, min_periods=15).std(), idr.rolling(20, min_periods=15).std()
    out["tug_20"] = cov / (sd_on * sd_id).replace(0, np.nan)
    return out


def screener(daily_ret: pd.DataFrame, uni: set, list_dates: dict, rebal):
    """t 日截面: 股票池过滤 + 前向 20 日收益 (与 factor_eval 同口径)"""
    from scripts.factor_eval import fwd_from_daily
    return fwd_from_daily(daily_ret)


def ic_sign(wide: dict, fwd: pd.DataFrame, rebal, uni, list_dates) -> dict:
    """各因子样本内/外 RankIC 均值 → 定方向用"""
    res = {}
    for name in FACTORS:
        f = wide[name]
        rows = {}
        for t in rebal:
            if t not in f.index or t not in fwd.index:
                continue
            row = f.loc[t].dropna()
            row = row[[c for c in row.index if c in uni]]
            fw = fwd.loc[t].reindex(row.index).dropna()
            row = row.reindex(fw.index)
            if len(row) < 50:
                continue
            rows[t] = row.corr(fw, method="spearman")
        s = pd.Series(rows).dropna()
        if s.empty:
            res[name] = {"in": np.nan, "out": np.nan}
            continue
        i = s[(s.index >= pd.Timestamp(IN_START)) & (s.index <= pd.Timestamp(IN_END))]
        o = s[s.index >= pd.Timestamp(OUT_START)]
        res[name] = {"in": i.mean(), "out": o.mean(), "n_in": len(i), "n_out": len(o)}
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None, help="全量起始日; 默认增量最近 10 个交易日")
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()

    conn = get_conn()
    if args.start:
        start, only_dates = args.start, None
    else:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT trade_date FROM daily_quote "
                    "WHERE ts_code LIKE '%.SZ' OR ts_code LIKE '%.SH' "
                    "ORDER BY trade_date DESC LIMIT 10")
        recent = [r[0] for r in cur.fetchall()]
        cur.close()
        if not recent:
            print("daily_quote 无数据"); return
        only_dates = set(recent)
        start = (min(recent) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
        print(f"增量模式: 回写 {min(recent)} ~ {max(recent)}")

    print(f"加载 OHLC (start={start}) ...")
    ohlc = load_ohlc(conn, start)
    print(f"  {ohlc['close'].shape[1]} 只 × {len(ohlc['close'])} 日")
    check_adjustment(ohlc)

    print("计算隔夜-日内结构因子 ...")
    raw = build_factors(ohlc)
    for name in FACTORS:
        print(f"  {name:14s} 非空率 {raw[name].notna().sum().sum()/raw[name].size*100:.1f}%")

    if args.no_write:
        conn.close()
        return

    # 方向判定: 样本内 IC 符号 (无前视)
    from scripts.factor_eval import (load_universe_filter, load_daily_returns, rebalance_dates)
    uni, list_dates = load_universe_filter(conn)
    daily_ret = load_daily_returns(conn, IN_START, pd.Timestamp.today().strftime("%Y-%m-%d"))
    fwd = screener(daily_ret, uni, list_dates, None)
    rebal = rebalance_dates(daily_ret.index, IN_START, "2026-09-11")
    sig = ic_sign(raw, fwd, rebal, uni, list_dates)

    print("\n=== IC 符号诊断 (样本内定方向 / 样本外验证) ===")
    print(f"{'因子':<15}{'样本内IC':>10}{'样本外IC':>10}{'方向':>6}")
    lines = []
    for name in FACTORS:
        s = sig[name]
        d = -1.0 if (np.isfinite(s["in"]) and s["in"] < 0) else 1.0
        print(f"{name:<15}{s['in']*100:>9.1f}%{s['out']*100:>9.1f}%{d:>6.0f}")
        lines.append(f"| {name} | {s['in']*100:.1f}% | {s['out']*100:.1f}% | {d:+.0f} |")
        # 应用方向
        raw[name] = raw[name] * d

    print("\n写入 factor_value ...")
    for name in FACTORS:
        n = upsert_factor(conn, name, raw[name], only_dates)
        print(f"  {name}: {n}")

    # 报告片段
    rep = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "reports", "intraday_factors_ic.md")
    with open(rep, "w") as f:
        f.write("# 隔夜-日内结构因子: IC 符号诊断 (方向 = 样本内 IC 符号)\n\n")
        f.write("| 因子 | 样本内IC(2016-2022) | 样本外IC(2023+) | 方向系数 |\n")
        f.write("|---|---|---|---|\n")
        f.write("\n".join(lines) + "\n")
    print(f"✅ IC 诊断: {rep}")
    conn.close()


if __name__ == "__main__":
    main()

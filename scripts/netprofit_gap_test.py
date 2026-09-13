#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
净利润断层信号实测（2017-2025）

信号定义（当日可见，无前视）：
  业绩：财报披露 netprofit_yoy >= 50%
  跳空：披露后首个交易日 low > 前一交易日 high（存在向上缺口）
  放量：当日 vol >= 1.5 × 前 20 日均量
  收阳：当日 close >= pre_close
买入：信号日收盘价，持有 252 个交易日
对照：同期沪深300 的 1 年收益
分层：单次信号 vs 12 个月内 >=2 次信号（连续断层）
验证：十年十倍股（2016-09→2026-09 的 20 只）在 2016-2020 早期是否发出过信号
"""
import json
import numpy as np
import pandas as pd
import psycopg2

DB = dict(host="/tmp", dbname="investassist", user="james")
SIG_START, SIG_END = "2016-09-01", "2025-09-11"   # 信号窗口（留 1 年持有期）
HOLD = 252


def q(conn, sql, p=None):
    return pd.read_sql(sql, conn, params=p)


def main():
    conn = psycopg2.connect(**DB)

    # ---- 1. 业绩事件 ----
    ev = q(conn, """
        SELECT ts_code, ann_date, report_year, report_type, netprofit_yoy, or_yoy
        FROM financial_indicator
        WHERE ann_date BETWEEN %(a)s AND %(b)s
          AND netprofit_yoy >= 50 AND ts_code ~ '\\.(SH|SZ)$'
    """, {"a": SIG_START, "b": SIG_END})
    ev = ev.dropna(subset=["ann_date"])
    ev["ann_date"] = pd.to_datetime(ev["ann_date"])
    print(f"业绩事件 (净利增速>=50%): {len(ev)}")

    # ---- 2. 全量行情（事件涉及的交易日范围） ----
    px = q(conn, """
        SELECT trade_date, ts_code, open, high, low, close, pre_close, pct_chg, vol
        FROM daily_quote
        WHERE trade_date BETWEEN '2016-06-01' AND '2026-09-11'
          AND ts_code ~ '\\.(SH|SZ)$'
    """)
    px["trade_date"] = pd.to_datetime(px["trade_date"])
    px = px.sort_values(["ts_code", "trade_date"])
    print(f"行情行数: {len(px)}")

    # 按股分组为 numpy 便于快速窗口查询
    grouped = {c: g.reset_index(drop=True) for c, g in px.groupby("ts_code")}

    # ---- 3. 沪深300 基准 ----
    bench = q(conn, """
        SELECT trade_date, pct_chg FROM index_daily
        WHERE symbol='000300.SH' AND trade_date BETWEEN '2016-06-01' AND '2026-09-11'
        ORDER BY trade_date
    """)
    bench["trade_date"] = pd.to_datetime(bench["trade_date"])
    bench = bench.set_index("trade_date")["pct_chg"] / 100.0

    def stock_1y_ret(g, i0):
        """从 i0 次日开始 252 日复权收益；不足 200 日视为 None。"""
        r = g["pct_chg"].iloc[i0 + 1: i0 + 1 + HOLD].dropna() / 100.0
        if len(r) < 200:
            return None
        return float((1 + r).prod() - 1)

    def bench_1y_ret(d0):
        r = bench.loc[bench.index > d0].iloc[:HOLD]
        if len(r) < 200:
            return None
        return float((1 + r).prod() - 1)

    # ---- 4. 信号检测 ----
    sigs = []
    for row in ev.itertuples():
        g = grouped.get(row.ts_code)
        if g is None:
            continue
        # 披露后首个交易日（ann_date 盘后 → 下一个交易日；盘中披露按当日亦可，统一取 >= ann_date 的第一天）
        idx = g.index[g["trade_date"] >= row.ann_date]
        if len(idx) == 0:
            continue
        i0 = idx[0]
        if i0 < 21 or i0 + 1 >= len(g):
            continue
        cur, prev = g.iloc[i0], g.iloc[i0 - 1]
        # 跳空: 当日最低 > 前日最高
        if not (pd.notna(cur["low"]) and pd.notna(prev["high"]) and cur["low"] > prev["high"]):
            continue
        # 收阳
        if not (pd.notna(cur["close"]) and pd.notna(cur["pre_close"]) and cur["close"] >= cur["pre_close"]):
            continue
        # 放量
        vol_ma20 = g["vol"].iloc[i0 - 20: i0].mean()
        if not (pd.notna(cur["vol"]) and vol_ma20 > 0 and cur["vol"] >= 1.5 * vol_ma20):
            continue
        r1y = stock_1y_ret(g, i0)
        b1y = bench_1y_ret(cur["trade_date"])
        sigs.append(dict(ts_code=row.ts_code, sig_date=str(cur["trade_date"].date()),
                         report_type=int(row.report_type), npy=float(row.netprofit_yoy),
                         ret_1y=r1y, bench_1y=b1y,
                         excess=(r1y - b1y) if (r1y is not None and b1y is not None) else None))

    sig = pd.DataFrame(sigs)
    print(f"\n净利润断层信号总数: {len(sig)}  (2016-09 ~ 2025-09)")
    valid = sig.dropna(subset=["excess"])
    print(f"可评估 1 年收益的: {len(valid)}")

    def stats(s, name):
        if len(s) == 0:
            print(f"  {name}: 无样本")
            return None
        r = dict(name=name, n=len(s),
                 mean_1y=round(float(s["ret_1y"].mean()) * 100, 1),
                 median_1y=round(float(s["ret_1y"].median()) * 100, 1),
                 win_rate=round(float((s["ret_1y"] > 0).mean()) * 100, 1),
                 mean_excess=round(float(s["excess"].mean()) * 100, 1),
                 median_excess=round(float(s["excess"].median()) * 100, 1),
                 excess_win=round(float((s["excess"] > 0).mean()) * 100, 1))
        print(f"  {name}: n={r['n']} | 1年均值 {r['mean_1y']}% 中位 {r['median_1y']}% 胜率 {r['win_rate']}% "
              f"| 超额均值 {r['mean_excess']}% 中位 {r['median_excess']}% 超额胜率 {r['excess_win']}%")
        return r

    print("\n=== 信号后 1 年表现 ===")
    st_all = stats(valid, "全部信号")
    sig["sig_date"] = pd.to_datetime(sig["sig_date"])
    sig = sig.sort_values(["ts_code", "sig_date"])
    sig["prev_sig"] = sig.groupby("ts_code")["sig_date"].shift(1)
    sig["days_since"] = (sig["sig_date"] - sig["prev_sig"]).dt.days
    sig["repeat"] = sig["days_since"] <= 370
    valid = sig.dropna(subset=["excess"])
    st_rep = stats(valid[valid["repeat"] == True], "连续断层(12个月内>=2次)")
    st_first = stats(valid[valid["repeat"] != True], "首次断层")

    # ---- 5. 十倍股早期信号覆盖 ----
    tb_codes = ["300308.SZ", "300502.SZ", "300394.SZ", "002463.SZ", "300476.SZ",
                "002916.SZ", "603986.SH", "688041.SH", "300661.SZ", "002371.SZ",
                "603501.SH", "600809.SH", "000975.SZ", "601899.SH", "002714.SZ",
                "600989.SH", "601100.SH", "000733.SZ", "603067.SH", "603738.SH"]
    early = sig[(sig["ts_code"].isin(tb_codes)) & (sig["sig_date"] <= "2021-06-30")]
    covered = sorted(early["ts_code"].unique())
    print(f"\n=== 十年十倍股 20 只中, 2016-09~2021-06 早期发出过断层信号的: {len(covered)} 只 ===")
    meta = q(conn, "SELECT ts_code, name FROM stocks WHERE ts_code = ANY(%(c)s)", {"c": tb_codes}).set_index("ts_code")
    for c in covered:
        es = early[early["ts_code"] == c]
        dates = ", ".join(d.strftime("%Y-%m") for d in es["sig_date"].head(4))
        print(f"  {meta.loc[c,'name']}({c}): {len(es)} 次, 首次 {es['sig_date'].min().date()}  [{dates}]")

    out = dict(signal_window=[SIG_START, SIG_END], n_signals=int(len(sig)),
               stats=[s for s in [st_all, st_rep, st_first] if s],
               tenbagger_early_coverage=dict(total=20, covered=len(covered), codes=covered),
               signals=sig.assign(sig_date=sig["sig_date"].astype(str)).drop(columns=["prev_sig"]).to_dict("records"))
    with open("outputs/netprofit_gap_test.json", "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=str)
    print("\n-> outputs/netprofit_gap_test.json")


if __name__ == "__main__":
    main()

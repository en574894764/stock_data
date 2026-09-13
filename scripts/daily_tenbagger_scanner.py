#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日十倍股扫描器（高召回，并集多通道）

设计原则：召回优先、精度交给人工。六条独立通道取并集（任一触发即入池），
每条通道对应一类被历史验证过的十倍股早期形态：
  A 断层确认：财报净利增速>=50% 且披露日跳空+放量+收阳（近90天）——动态最强信号
  B 业绩高增：最新报告净利>=80% 或 (净利>=50% 且 营收>=30%)（近150天披露）
  C 次新股：上市<2年 且 (净利>=30% 或 营收>=25%) —— 45%的十倍股是次新股
  D 趋势新高：距52周高点<5% 且 净利>=20% —— CANSLIM 相对强度
  E 宽松漏斗：营收>=10% & ROE>=5% & 毛利>=20% & 市值<=500亿 & 行业景气前1/3
  F 质量成长：ROE>=15% & 营收>=20% & 净利>=20% —— 牧原型

用法：
  python3 scripts/daily_tenbagger_scanner.py                  # 扫描最新交易日
  python3 scripts/daily_tenbagger_scanner.py --date 2026-09-11
  python3 scripts/daily_tenbagger_scanner.py --backtest       # 历史召回验证(2016-2021)
输出：
  outputs/daily_scan/watchlist_<date>.csv  +  outputs/daily_scan/latest.csv
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd
import psycopg2

DB = dict(host="/tmp", dbname="investassist", user="james")
OUT_DIR = "outputs/daily_scan"
CACHE_DIR = "outputs/bulk_cache"
CACHE_FILES = ["meta", "fi", "mon_mret", "mon_high", "mon_amt", "gap_days"]

# 十倍股名单（2016-09→2026-09 窗口，用于历史验证）
TB20 = {"300308.SZ": "中际旭创", "300502.SZ": "新易盛", "300394.SZ": "天孚通信",
        "002463.SZ": "沪电股份", "300476.SZ": "胜宏科技", "002916.SZ": "深南电路",
        "603986.SH": "兆易创新", "688041.SH": "海光信息", "300661.SZ": "圣邦股份",
        "002371.SZ": "北方华创", "603501.SH": "韦尔股份", "600809.SH": "山西汾酒",
        "000975.SZ": "银泰黄金", "601899.SH": "紫金矿业", "002714.SZ": "牧原股份",
        "300548.SZ": "长芯博创", "601100.SH": "恒立液压", "000733.SZ": "振华科技",
        "603067.SH": "振华风光", "603738.SH": "泰晶科技"}


# ---------------- 基建：一次性加载 ----------------

def q(conn, sql, p=None):
    return pd.read_sql(sql, conn, params=p)


def load_bulk(conn, refresh=False):
    """加载批量数据。带当日 parquet 缓存：当天内重复运行秒级加载，跨天自动重建。"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    paths = {n: f"{CACHE_DIR}/{n}.parquet" for n in CACHE_FILES}
    fresh = all(os.path.exists(p) for p in paths.values())
    if fresh and not refresh:
        mt = max(os.path.getmtime(p) for p in paths.values())
        import datetime as _dt
        fresh = _dt.date.fromtimestamp(mt) == _dt.date.today()
    if fresh and not refresh:
        print("[bulk] 使用当日缓存", flush=True)
        bulk = {n: pd.read_parquet(paths[n]) for n in CACHE_FILES}
        bulk["meta"] = bulk["meta"].set_index("ts_code")
        for k in ["mon_mret", "mon_high", "mon_amt"]:
            bulk[k].index = pd.to_datetime(bulk[k].index)
        bulk["fi"]["ann_date"] = pd.to_datetime(bulk["fi"]["ann_date"])
        bulk["gap_days"]["trade_date"] = pd.to_datetime(bulk["gap_days"]["trade_date"])
        return bulk

    bulk = {}
    print("[bulk] meta ...", flush=True)
    meta = q(conn, "SELECT ts_code, name, industry, list_date FROM stocks")
    meta["list_date"] = pd.to_datetime(meta["list_date"], format="%Y%m%d", errors="coerce")
    bulk["meta"] = meta.set_index("ts_code")

    print("[bulk] financial_indicator ...", flush=True)
    fi = q(conn, """
        SELECT ts_code, ann_date, report_year, report_type, roe, or_yoy, netprofit_yoy, grossprofit_margin
        FROM financial_indicator WHERE ann_date IS NOT NULL AND ts_code ~ '\\.(SH|SZ)$'
        ORDER BY ts_code, ann_date
    """)
    fi["ann_date"] = pd.to_datetime(fi["ann_date"])
    bulk["fi"] = fi

    print("[bulk] monthly bars (10M rows 聚合, 约1-2分钟) ...", flush=True)
    mon = q(conn, """
        SELECT date_trunc('month', trade_date)::date AS m, ts_code,
               exp(sum(ln(1.0 + pct_chg/100.0))) - 1.0 AS mret,
               max(high) AS mhigh, avg(amount) AS mamount
        FROM daily_quote
        WHERE pct_chg IS NOT NULL AND ts_code ~ '\\.(SH|SZ)$'
        GROUP BY 1, 2
    """)
    mon["m"] = pd.to_datetime(mon["m"])
    bulk["mon_mret"] = mon.pivot(index="m", columns="ts_code", values="mret").sort_index()
    bulk["mon_high"] = mon.pivot(index="m", columns="ts_code", values="mhigh").sort_index()
    bulk["mon_amt"] = mon.pivot(index="m", columns="ts_code", values="mamount").sort_index()

    print("[bulk] 断层确认日集合 (窗口函数, 约3-6分钟) ...", flush=True)
    gap = q(conn, """
        WITH w AS (
          SELECT ts_code, trade_date, low, close, pre_close, vol,
                 lag(high) OVER (PARTITION BY ts_code ORDER BY trade_date) AS ph,
                 avg(vol) OVER (PARTITION BY ts_code ORDER BY trade_date
                                ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS v20
          FROM daily_quote WHERE ts_code ~ '\\.(SH|SZ)$'
        )
        SELECT ts_code, trade_date FROM w
        WHERE low > ph AND close >= pre_close AND vol >= 1.5 * v20
    """)
    gap["trade_date"] = pd.to_datetime(gap["trade_date"])
    bulk["gap_days"] = gap  # 每行一个"断层确认日"
    print(f"[bulk] done. 断层确认日 {len(gap)} 个", flush=True)

    # 写缓存（meta 先重置索引）
    bulk["meta"].reset_index().to_parquet(paths["meta"])
    bulk["fi"].to_parquet(paths["fi"], index=False)
    bulk["mon_mret"].to_parquet(paths["mon_mret"])
    bulk["mon_high"].to_parquet(paths["mon_high"])
    bulk["mon_amt"].to_parquet(paths["mon_amt"])
    bulk["gap_days"].to_parquet(paths["gap_days"], index=False)
    print("[bulk] 缓存已写入", flush=True)
    return bulk


# ---------------- 截面构建 ----------------

def latest_reports(fi, asof, n=2):
    """每股截至 asof 最近 n 份报告（按披露日）。"""
    sub = fi[fi["ann_date"] <= asof]
    return sub.groupby("ts_code").tail(n)


def snapshot(bulk, conn, asof):
    t = pd.Timestamp(asof)
    meta = bulk["meta"]
    # 最新收盘/估值（asof 前 10 天内最近一行; close 取 daily_basic 当日同表的 daily_quote 收盘）
    db = q(conn, """
        SELECT DISTINCT ON (d.ts_code) d.ts_code, d.trade_date, d.pe_ttm, d.pb, d.total_mv, q2.close
        FROM daily_basic d
        JOIN daily_quote q2 ON q2.ts_code = d.ts_code AND q2.trade_date = d.trade_date
        WHERE d.trade_date BETWEEN %(a)s AND %(b)s
        ORDER BY d.ts_code, d.trade_date DESC
    """, {"a": (t - pd.DateOffset(days=10)).strftime("%Y-%m-%d"), "b": asof}).set_index("ts_code")

    # 最近两份报告
    rep = latest_reports(bulk["fi"], t, 2)
    last = rep.groupby("ts_code").tail(1).set_index("ts_code")
    prev = rep.groupby("ts_code").head(1).set_index("ts_code")
    prev = prev[~prev.index.isin([])]
    # head(1) 对只有1份的也会取到，需要区分：只保留有2份的
    cnt = rep.groupby("ts_code").size()
    prev = prev[prev.index.isin(cnt[cnt >= 2].index)]

    # 动量: 近12个完整月
    mret = bulk["mon_mret"]
    months = mret.index[mret.index < t]
    m12 = months[-12:]
    mom12 = (1 + mret.loc[m12]).prod() - 1.0
    mom12.name = "ret12m"
    # 52周高点: 近12月 max(mhigh)（月内最高价的年max，精确到月）
    mh = bulk["mon_high"]
    hi52 = mh.loc[mh.index[mh.index < t][-12:]].max()
    hi52.name = "high52"
    # 流动性: 近3月平均 amount（千元）
    ma = bulk["mon_amt"]
    amt3 = ma.loc[ma.index[ma.index < t][-3:]].mean() / 10.0  # 千元 -> 万元
    amt3.name = "amt_wan"

    df = meta.join(db[["pe_ttm", "pb", "total_mv", "close"]], how="inner") \
             .join(last[["ann_date", "roe", "or_yoy", "netprofit_yoy", "grossprofit_margin"]]
                   .rename(columns={"ann_date": "last_ann"}), how="left")
    prev_npy = prev["netprofit_yoy"].rename("prev_npy")
    df = df.join(prev_npy, how="left").join(mom12, how="left").join(hi52, how="left").join(amt3, how="left")
    df["mv_yi"] = df["total_mv"] / 1e4
    df["dist_high"] = df["close"] / df["high52"]
    df["list_age_y"] = (t - df["list_date"]).dt.days / 365.25
    # 行业景气分位（最新报告 or_yoy）
    ind = df.groupby("industry")["or_yoy"].agg(["median", "count"])
    ind.columns = ["ind_med", "ind_n"]
    df = df.join(ind, on="industry")
    ok = df["ind_n"] >= 3
    df["ind_rank"] = np.nan
    df.loc[ok, "ind_rank"] = df.loc[ok, "ind_med"].rank(pct=True)
    # 断层: 近90天内有 (财报事件 npy>=50) 且 披露后首个交易日是断层确认日
    gap = bulk["gap_days"]
    ev = bulk["fi"][(bulk["fi"]["ann_date"] > t - pd.DateOffset(days=90)) &
                    (bulk["fi"]["ann_date"] <= t) & (bulk["fi"]["netprofit_yoy"] >= 50)]
    if len(ev):
        gmap = gap[gap["trade_date"] <= t].groupby("ts_code")["trade_date"].apply(set).to_dict()
        def has_gap(r):
            s = gmap.get(r["ts_code"])
            if not s:
                return False
            later = [d for d in s if d >= r["ann_date"]]
            return bool(later) and min(later) <= r["ann_date"] + pd.Timedelta(days=7)
        gap_hits = set(ev[ev.apply(has_gap, axis=1)]["ts_code"])
    else:
        gap_hits = set()
    df["gap90"] = df.index.isin(gap_hits)
    return df


# ---------------- 六通道 ----------------

def scan(df, asof):
    t = pd.Timestamp(asof)
    ch = {}
    npy, ory, roe, gm = df["netprofit_yoy"], df["or_yoy"], df["roe"], df["grossprofit_margin"]
    fresh150 = df["last_ann"] >= t - pd.DateOffset(days=150)

    ch["A_断层"] = df.index[df["gap90"]]
    ch["B_业绩高增"] = df.index[fresh150 & (df["mv_yi"] <= 1000) &
                                ((npy >= 100) | ((npy >= 60) & (ory >= 40)))]
    ch["C_次新"] = df.index[(df["list_age_y"] < 2) & ((npy >= 30) | (ory >= 25)) & (df["mv_yi"] <= 800)]
    ch["D_趋势新高"] = df.index[(df["dist_high"] >= 0.95) & (npy >= 20) & (df["mv_yi"] >= 30) & (df["amt_wan"] >= 3000)]
    ch["E_宽松漏斗"] = df.index[(ory >= 10) & (roe >= 5) & (gm >= 20) & (df["mv_yi"] <= 500) &
                                (df["ind_rank"] >= 2 / 3) & (df["ind_med"] > 0)]
    ch["F_质量成长"] = df.index[(roe >= 15) & (ory >= 20) & (npy >= 20)]

    # 基础排除：ST、停牌无数据、上市<30天
    base = (~df["name"].str.contains("ST", na=False)) & df["close"].notna() & (df["list_age_y"] > 0.08)
    rows = []
    for code in df.index[base]:
        tags = [k[:-2] + k[-1] if False else k for k, v in ch.items() if code in set(v)]
        if not tags:
            continue
        r = df.loc[code]
        rows.append(dict(
            ts_code=code, name=r["name"], industry=r["industry"],
            n_ch=len(tags), channels="/".join(x.split("_")[0] for x in tags),
            mv_yi=round(r["mv_yi"], 0) if pd.notna(r["mv_yi"]) else None,
            pe=round(r["pe_ttm"], 1) if pd.notna(r["pe_ttm"]) else None,
            npy=round(r["netprofit_yoy"], 0) if pd.notna(r["netprofit_yoy"]) else None,
            or_yoy=round(r["or_yoy"], 0) if pd.notna(r["or_yoy"]) else None,
            roe=round(r["roe"], 1) if pd.notna(r["roe"]) else None,
            gm=round(r["grossprofit_margin"], 0) if pd.notna(r["grossprofit_margin"]) else None,
            ret12m=round(r["ret12m"], 2) if pd.notna(r["ret12m"]) else None,
            dist_high=round(r["dist_high"], 2) if pd.notna(r["dist_high"]) else None,
            list_age_y=round(r["list_age_y"], 1),
        ))
    out = pd.DataFrame(rows)
    if len(out):
        out["tier"] = out["n_ch"].map(lambda n: "★★★" if n >= 3 else ("★★" if n == 2 else "★"))
        out = out.sort_values(["n_ch", "npy"], ascending=[False, False])
    return out, {k: len(v) for k, v in ch.items()}


# ---------------- 输出截断（多通道优先, 总量可控） ----------------

def apply_cap(out, multi_cap=200, single_cap=100):
    """★★/★★★ 多通道全留(超 multi_cap 时按通道数+净利增速截断), ★ 单通道截 single_cap。"""
    if not len(out):
        return out
    multi = out[out["n_ch"] >= 2].head(multi_cap)
    single = out[out["n_ch"] < 2].head(single_cap)
    return pd.concat([multi, single])


# ---------------- 入口 ----------------

def latest_trade_date(conn):
    return str(q(conn, "SELECT max(trade_date) AS d FROM daily_quote")["d"].iloc[0])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--cap", type=int, default=250, help="单通道(★)截断上限, 多通道不截")
    args = ap.parse_args()

    conn = psycopg2.connect(**DB)
    os.makedirs(OUT_DIR, exist_ok=True)
    bulk = load_bulk(conn)

    if args.backtest:
        dates = ["2016-09-12", "2016-12-30", "2017-03-31", "2017-06-30", "2017-09-29",
                 "2017-12-29", "2018-03-30", "2018-06-29", "2018-09-28", "2018-12-28",
                 "2019-03-29", "2019-06-28", "2019-09-30", "2019-12-31",
                 "2020-03-31", "2020-06-30", "2020-09-30", "2020-12-31",
                 "2021-03-31", "2021-06-30"]
        appeared = {}
        sizes = []
        for d in dates:
            df = snapshot(bulk, conn, d)
            out, chs = scan(df, d)
            out = apply_cap(out)  # 召回按真实输出(截断后)统计
            n_multi = int(out["n_ch"].ge(2).sum()) if len(out) else 0
            sizes.append((d, int(len(out)), n_multi))
            for c in set(out["ts_code"]) & set(TB20):
                appeared.setdefault(c, []).append(d)
            print(f"  {d}: 输出 {len(out)} (多通道 {n_multi}) 通道 {chs}", flush=True)
        print("\n=== 历史召回验证 (2016-09 ~ 2021-06 任一日期入池即算召回) ===")
        print(f"召回 {len(appeared)}/20 = {len(appeared)/20*100:.0f}%")
        for c, ds in sorted(appeared.items(), key=lambda x: x[1][0]):
            print(f"  {TB20[c]}({c}): 首次入池 {ds[0]}, 共 {len(ds)} 次")
        miss = set(TB20) - set(appeared)
        print(f"未召回 {len(miss)}: {[TB20[c] for c in miss]}")
        json.dump(dict(sizes=sizes, appeared={c: v for c, v in appeared.items()}),
                  open(f"{OUT_DIR}/backtest_recall.json", "w"), ensure_ascii=False, indent=1, default=str)
        return

    asof = args.date or latest_trade_date(conn)
    print(f"扫描日期: {asof}")
    df = snapshot(bulk, conn, asof)
    out, chs = scan(df, asof)
    print(f"通道命中: {chs}")
    final = apply_cap(out, multi_cap=args.cap, single_cap=100)
    n_multi = int(final["n_ch"].ge(2).sum()) if len(final) else 0
    p1 = f"{OUT_DIR}/watchlist_{asof}.csv"
    final.to_csv(p1, index=False, encoding="utf-8-sig")
    final.to_csv(f"{OUT_DIR}/latest.csv", index=False, encoding="utf-8-sig")
    print(f"入池 {len(final)} 只 (多通道 {n_multi}, 单通道 {len(final) - n_multi})")
    print(f"-> {p1} & latest.csv")
    print("\n=== ★★★ 与 ★★（多通道共振，优先人工研究）===")
    cols = ["ts_code", "name", "industry", "n_ch", "channels", "mv_yi", "pe", "npy", "or_yoy", "roe", "dist_high", "tier"]
    print(final[final["n_ch"] >= 2][cols].head(50).to_string(index=False))


if __name__ == "__main__":
    main()

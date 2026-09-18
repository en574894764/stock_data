#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
验证：本口径下「深度低估（市值/三年后合理估值 低）」是否对应更高的未来收益（赔率）。
方法：每年 4 月底（年报出齐后）重算一次 mv_ratio，看未来 1 年 / 6 个月收益按分档的表现。
口径与生产一致：ROE(上一年年报)≥12% & 负债率<50% & 扣非占比≥70%；合理估值=归母净利×30×(1+g)^3。
"""

from datetime import date

import numpy as np
import pandas as pd

import valuelib as vl

pd.set_option("display.width", 220)
pd.set_option("display.unicode.east_asian_width", True)


def cagr(vals):
    v = [float(x) for x in vals]
    return (v[-1] / v[0]) ** (1 / (len(v) - 1)) - 1


def main():
    conn = vl.connect()
    try:
        cfg = vl.load_config()
        roe_min = cfg["screen"]["roe_min"]
        debt_max = cfg["screen"]["debt_to_assets_max"]
        dpr_min = cfg["screen"].get("deduct_profit_ratio_min", 0.7)

        # 交易日历
        dates = vl.query("SELECT DISTINCT trade_date AS d FROM daily_quote ORDER BY d", conn=conn)["d"].tolist()
        dates = [pd.Timestamp(x) for x in dates]

        # 每年 4 月底快照 + 未来 250/125 交易日
        snaps = {}
        for y in range(2017, 2025):
            cutoff = pd.Timestamp(date(y, 4, 30))
            cand = [x for x in dates if x <= cutoff]
            if not cand:
                continue
            i = dates.index(cand[-1])
            snaps[y] = dict(i=i, d=cand[-1],
                            i1=i + 250 if i + 250 < len(dates) else None,
                            i6=i + 125 if i + 125 < len(dates) else None)

        need_dates = [snaps[y]["d"] for y in snaps]
        need_dates += [dates[snaps[y]["i1"]] for y in snaps if snaps[y]["i1"]]
        need_dates += [dates[snaps[y]["i6"]] for y in snaps if snaps[y]["i6"]]
        need_dates = sorted(set(need_dates))

        # 财务：ROE/负债/扣非(各年) + 归母净利历史
        fi = vl.query(
            "SELECT ts_code, report_year, roe, debt_to_assets, profit_dedt FROM financial_indicator "
            "WHERE report_type='4' AND report_year BETWEEN 2016 AND 2024", conn=conn)
        inc = vl.query(
            "SELECT ts_code, report_year, n_income_attr_p FROM income "
            "WHERE report_type='4' AND report_year BETWEEN 2011 AND 2024", conn=conn)
        stocks = vl.query("SELECT ts_code, name, market, list_status FROM stocks", conn=conn)

        # 行情：快照日市值 + 各日收盘（用字符串日期对齐）
        snap_dates_list = [snaps[y]["d"].date() for y in snaps]
        need_dates_list = [d.date() for d in need_dates]
        mv = vl.query(
            "SELECT ts_code, to_char(trade_date,'YYYY-MM-DD') AS d, total_mv FROM daily_basic WHERE trade_date = ANY(%s)",
            (snap_dates_list,), conn=conn)
        px = vl.query(
            "SELECT ts_code, to_char(trade_date,'YYYY-MM-DD') AS d, close FROM daily_quote WHERE trade_date = ANY(%s)",
            (need_dates_list,), conn=conn)

        # 组织成宽表
        fi["report_year"] = fi["report_year"].astype(int)
        inc["report_year"] = inc["report_year"].astype(int)
        prof = {ts: dict(zip(g["report_year"], g["n_income_attr_p"])) for ts, g in inc.groupby("ts_code")}

        mvmap = mv.pivot(index="d", columns="ts_code", values="total_mv")
        pxmap = px.pivot(index="d", columns="ts_code", values="close")

        rows = []
        for y, s in snaps.items():
            base = y - 1
            d_str = s["d"].strftime("%Y-%m-%d")
            f1 = dates[s["i1"]] if s["i1"] else None
            f6 = dates[s["i6"]] if s["i6"] else None

            fiy = fi[fi["report_year"] == base]
            fiy = fiy.merge(stocks, on="ts_code", how="left")
            fiy = fiy[fiy["list_status"] == "L"]
            fiy = fiy[fiy["ts_code"].str.endswith((".SH", ".SZ", ".BJ"), na=False)]
            fiy = fiy[~fiy["name"].fillna("").str.contains("ST|退", regex=True)]

            # 归母净利（同年报）
            np_map = {ts: prof.get(ts, {}).get(base) for ts in fiy["ts_code"]}
            fiy["n_income_attr_p"] = fiy["ts_code"].map(np_map)
            fiy["扣非占比"] = fiy["profit_dedt"] / fiy["n_income_attr_p"]

            # 选股口径
            fiy = fiy[(fiy["roe"] >= roe_min) & (fiy["debt_to_assets"] <= debt_max)]
            if dpr_min is not None:
                fiy = fiy[fiy["扣非占比"] >= dpr_min]
            fiy = fiy[fiy["n_income_attr_p"] > 0]

            # 增速
            def growth(ts):
                pts = [prof.get(ts, {}).get(base - k) for k in range(4, -1, -1)]
                if all(p is not None and p > 0 for p in pts):
                    return min(max(cagr(pts), -0.10), 0.30)
                pts3 = [prof.get(ts, {}).get(base - k) for k in range(2, -1, -1)]
                if all(p is not None and p > 0 for p in pts3):
                    return min(max(cagr(pts3), -0.10), 0.30)
                return 0.05

            fiy["g"] = fiy["ts_code"].map(growth)
            fiy["fv3_yi"] = fiy["n_income_attr_p"] / 1e8 * 30 * (1 + fiy["g"]) ** 3

            for ts, r in fiy.iterrows():
                mv_now = mvmap.at[d_str, r["ts_code"]] if d_str in mvmap.index and r["ts_code"] in mvmap.columns else np.nan
                if pd.isna(mv_now) or mv_now <= 0:
                    continue
                mv_yi = mv_now / 1e4
                ratio = mv_yi / r["fv3_yi"]
                f1_str = f1.strftime('%Y-%m-%d') if f1 else None
                f6_str = f6.strftime('%Y-%m-%d') if f6 else None
                px0 = pxmap.at[d_str, r["ts_code"]] if d_str in pxmap.index and r["ts_code"] in pxmap.columns else np.nan
                p1 = pxmap.at[f1_str, r["ts_code"]] if f1_str and f1_str in pxmap.index and r["ts_code"] in pxmap.columns else np.nan
                p6 = pxmap.at[f6_str, r["ts_code"]] if f6_str and f6_str in pxmap.index and r["ts_code"] in pxmap.columns else np.nan
                if pd.isna(px0):
                    continue
                rows.append(dict(
                    year=y, ts_code=r["ts_code"], ratio=ratio,
                    fwd1y=(p1 / px0 - 1) if pd.notna(p1) else np.nan,
                    fwd6m=(p6 / px0 - 1) if pd.notna(p6) else np.nan,
                ))

        df = pd.DataFrame(rows)
        df["档位"] = pd.cut(df["ratio"], bins=[-np.inf, 0.5, 0.7, 1.0, np.inf],
                            labels=["非常低估", "低估", "一般低估", "高估"])
        df["上行空间"] = 1 / df["ratio"] - 1

        print("=" * 70)
        print("深度低估 → 未来收益（快照 2017~2024 每年4月底，样本 %d 条）" % len(df))
        print("=" * 70)
        bench = df["fwd1y"].mean()
        print(f"全池等权未来1年收益基准：{bench:+.1%}\n")
        print(f"{'档位':<8}{'n':>6}{'未来1年均值':>12}{'中位':>9}{'胜率':>8}{'跑赢基准':>9}{'未来6月均值':>12}")
        for t in ["非常低估", "低估", "一般低估", "高估"]:
            sub = df[df["档位"] == t]
            m1, m6 = sub["fwd1y"].mean(), sub["fwd6m"].mean()
            med = sub["fwd1y"].median()
            win = (sub["fwd1y"] > 0).mean()
            beat = (sub["fwd1y"] > bench).mean()
            print(f"{t:<8}{len(sub):>6}{m1:>+11.1%}{med:>+8.1%}{win:>7.1%}{beat:>8.1%}{m6:>+11.1%}")

        # 非常低估内部：折价越深，赔率越大？
        print("\n非常低估档内按折价深度细分（未来1年）:")
        deep = df[df["档位"] == "非常低估"].copy()
        deep["折价分层"] = pd.cut(deep["ratio"], bins=[0, 0.3, 0.4, 0.5],
                                  labels=["<0.3(折价>70%)", "0.3~0.4", "0.4~0.5"])
        for t, sub in deep.groupby("折价分层", observed=True):
            print(f"  {t:<14} n={len(sub):>4}  未来1年均值 {sub['fwd1y'].mean():+.1%}"
                  f"  中位 {sub['fwd1y'].median():+.1%}  胜率 {(sub['fwd1y']>0).mean():.1%}")

        return df
    finally:
        conn.close()


if __name__ == "__main__":
    main()

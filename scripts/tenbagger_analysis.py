# -*- coding: utf-8 -*-
"""
十年十倍股分析 (2016-09-12 -> 2026-09-11, A股, 起点前已上市)
1) 筛选 10x 名单
2) 上涨路径 (何时达成10x, 各年度累计)
3) 起点画像 vs 全市场 (市值/ROE/毛利率/增速/估值)
4) 收益归因 (EPS增长 vs PE扩张)
5) 2016 时点识别规则测试 (规则能覆盖几只/组合多大)
结果落盘 outputs/tenbagger_analysis.json
"""
import json
import sys

import numpy as np
import pandas as pd
import psycopg2

sys.path.insert(0, "/Users/james/workspace/stock_data")

START, END = "2016-09-12", "2026-09-11"
CONN = dict(host="/tmp", dbname="investassist", user="james")


def get_conn():
    return psycopg2.connect(**CONN)


def q(sql, params=None):
    with get_conn() as c:
        return pd.read_sql(sql, c, params=params)


# ---------- 1. 筛选 10x ----------
sql_10x = """
SELECT d.ts_code, s.name, s.industry, s.list_date,
       (exp(sum(ln(1.0 + d.pct_chg/100.0))) - 1) AS total_ret
FROM daily_quote d JOIN stocks s ON s.ts_code = d.ts_code
WHERE d.trade_date BETWEEN %(s)s AND %(e)s AND d.pct_chg IS NOT NULL
  AND d.ts_code ~ '\.(SH|SZ)$'
GROUP BY d.ts_code, s.name, s.industry, s.list_date
HAVING count(*) > 1900 AND min(d.trade_date) < '2016-11-01'
  AND exp(sum(ln(1.0 + d.pct_chg/100.0))) >= 10.0
ORDER BY total_ret DESC
"""
ten = q(sql_10x, {"s": START, "e": END})
ten["total_ret"] = ten["total_ret"].astype(float)
codes = ten["ts_code"].tolist()
print(f"10x 名单: {len(ten)} 只 (全市场起点可比 {2637} 只, 命中率 {len(ten)/2637*100:.2f}%)")

# ---------- 2. 路径: 何时达成 10x + 年度累计 ----------
sql_path = """
SELECT trade_date, ts_code, pct_chg FROM daily_quote
WHERE trade_date BETWEEN %(s)s AND %(e)s AND pct_chg IS NOT NULL AND ts_code = ANY(%(c)s)
ORDER BY ts_code, trade_date
"""
path = q(sql_path, {"s": START, "e": END, "c": codes})
path["trade_date"] = pd.to_datetime(path["trade_date"])
milestones = ["2017-12-31", "2018-12-31", "2019-12-31", "2020-12-31",
              "2021-12-31", "2022-12-31", "2023-12-31", "2024-12-31", "2025-12-31"]
path_rows = []
for code, g in path.groupby("ts_code"):
    g = g.sort_values("trade_date").set_index("trade_date")
    cum = (1 + g["pct_chg"] / 100.0).cumprod()
    hit = cum[cum >= 10.0]
    t10 = str(hit.index[0].date()) if len(hit) else None
    row = {"ts_code": code,
           **{m[:4]: round(float(cum[cum.index <= m].iloc[-1]) - 1, 2)
              if (cum.index <= m).any() else None for m in milestones}}
    row["first_10x_date"] = t10
    path_rows.append(row)
path_df = pd.DataFrame(path_rows)
ten = ten.merge(path_df, on="ts_code", how="left")

# ---------- 3. 起点画像: daily_basic + financial_indicator ----------
sql_db_start = """
SELECT DISTINCT ON (ts_code) ts_code, trade_date, total_mv, pe_ttm, pb, circ_mv
FROM daily_basic
WHERE trade_date BETWEEN '2016-09-05' AND '2016-09-12' AND ts_code ~ '\.(SH|SZ)$'
ORDER BY ts_code, trade_date DESC
"""
db_start = q(sql_db_start)
sql_db_end = """
SELECT DISTINCT ON (ts_code) ts_code, trade_date, total_mv, pe_ttm, pb
FROM daily_basic WHERE trade_date BETWEEN '2026-09-05' AND '2026-09-11'
ORDER BY ts_code, trade_date DESC
"""
db_end = q(sql_db_end)

# 2016 中报 (Q2, 披露截止8月底, 起点可见) + 2015 年报
sql_fi = """
SELECT ts_code, report_year, report_type, roe, grossprofit_margin, or_yoy,
       netprofit_yoy, debt_to_assets
FROM financial_indicator
WHERE (report_year = 2016 AND report_type = '2')
   OR (report_year = 2015 AND report_type = '4')
"""
fi = q(sql_fi)
fi16 = fi[fi["report_year"] == 2016].set_index("ts_code")
fi15 = fi[fi["report_year"] == 2015].set_index("ts_code")

# income: 2015 年报 vs 2025 年报 营收/归母净利
sql_inc = """
SELECT ts_code, report_year, report_type, revenue, n_income_attr_p
FROM income WHERE report_type = '4' AND report_year IN (2015, 2025)
"""
inc = q(sql_inc)
inc15 = inc[inc["report_year"] == 2015].set_index("ts_code")
inc25 = inc[inc["report_year"] == 2025].set_index("ts_code")

prof_rows = []
for _, r in ten.iterrows():
    c = r["ts_code"]
    db_s = db_start[db_start["ts_code"] == c]
    db_e = db_end[db_end["ts_code"] == c]
    f16 = fi16.loc[c] if c in fi16.index else None
    f15 = fi15.loc[c] if c in fi15.index else None
    i15 = inc15.loc[c] if c in inc15.index else None
    i25 = inc25.loc[c] if c in inc25.index else None
    mv_s = float(db_s["total_mv"].iloc[0]) / 1e4 if len(db_s) and pd.notna(db_s["total_mv"].iloc[0]) else None  # 万元->亿
    pe_s = float(db_s["pe_ttm"].iloc[0]) if len(db_s) and pd.notna(db_s["pe_ttm"].iloc[0]) else None
    pb_s = float(db_s["pb"].iloc[0]) if len(db_s) and pd.notna(db_s["pb"].iloc[0]) else None
    pe_e = float(db_e["pe_ttm"].iloc[0]) if len(db_e) and pd.notna(db_e["pe_ttm"].iloc[0]) else None
    mv_e = float(db_e["total_mv"].iloc[0]) / 1e4 if len(db_e) and pd.notna(db_e["total_mv"].iloc[0]) else None
    roe16 = float(f16["roe"]) * 2 if f16 is not None and pd.notna(f16["roe"]) else None  # H1 年化
    gm16 = float(f16["grossprofit_margin"]) if f16 is not None and pd.notna(f16["grossprofit_margin"]) else None
    ory16 = float(f16["or_yoy"]) if f16 is not None and pd.notna(f16["or_yoy"]) else None
    roe15 = float(f15["roe"]) if f15 is not None and pd.notna(f15["roe"]) else None
    rev15 = float(i15["revenue"]) / 1e8 if i15 is not None and pd.notna(i15["revenue"]) else None
    rev25 = float(i25["revenue"]) / 1e8 if i25 is not None and pd.notna(i25["revenue"]) else None
    ni15 = float(i15["n_income_attr_p"]) / 1e8 if i15 is not None and pd.notna(i15["n_income_attr_p"]) else None
    ni25 = float(i25["n_income_attr_p"]) / 1e8 if i25 is not None and pd.notna(i25["n_income_attr_p"]) else None
    # 归因: (1+ret) = EPS倍数 x PE倍数
    eps_mult = None
    if pe_s and pe_e and pe_s > 0 and pe_e > 0:
        eps_mult = (1 + r["total_ret"]) / (pe_e / pe_s)
    prof_rows.append({
        "ts_code": c, "name": r["name"], "industry": r["industry"],
        "total_ret": round(r["total_ret"], 1), "first_10x_date": r["first_10x_date"],
        "mv_start": round(mv_s, 1) if mv_s else None,
        "pe_start": round(pe_s, 1) if pe_s else None,
        "pb_start": round(pb_s, 2) if pb_s else None,
        "roe16_ann": round(roe16, 1) if roe16 is not None else None,
        "gm16": round(gm16, 1) if gm16 is not None else None,
        "or_yoy16": round(ory16, 1) if ory16 is not None else None,
        "roe15": round(roe15, 1) if roe15 is not None else None,
        "rev15": round(rev15, 1) if rev15 else None,
        "rev25": round(rev25, 1) if rev25 else None,
        "rev_mult": round(rev25 / rev15, 1) if rev15 and rev25 and rev15 > 0 else None,
        "ni15": round(ni15, 2) if ni15 else None,
        "ni25": round(ni25, 2) if ni25 else None,
        "ni_mult": round(ni25 / ni15, 1) if ni15 and ni25 and abs(ni15) > 1e-8 else None,
        "pe_end": round(pe_e, 1) if pe_e else None,
        "mv_end": round(mv_e, 0) if mv_e else None,
        "eps_mult": round(eps_mult, 1) if eps_mult else None,
        **{y: r.get(y) for y in ["2017", "2018", "2019", "2020", "2021", "2022", "2023", "2024", "2025"]},
    })
prof = pd.DataFrame(prof_rows)

print("\n=== 名单 (含起点画像) ===")
cols_show = ["ts_code", "name", "industry", "total_ret", "first_10x_date", "mv_start",
             "pe_start", "roe16_ann", "or_yoy16", "rev_mult", "ni_mult", "eps_mult"]
print(prof[cols_show].to_string(index=False))

# ---------- 4. 全市场起点基线 (percentile) ----------
base = db_start.merge(fi16.reset_index()[["ts_code", "roe", "or_yoy", "grossprofit_margin"]],
                      on="ts_code", how="left")
base["roe_ann"] = base["roe"] * 2
base["mv_yi"] = base["total_mv"] / 1e4


def pct_rank(series, val):
    s = series.dropna()
    if val is None or not np.isfinite(val):
        return None
    return float((s < val).mean() * 100)


base_stats = {
    "n": len(base),
    "mv_median": float(base["mv_yi"].median()),
    "pe_median": float(base["pe_ttm"][base["pe_ttm"] > 0].median()),
    "roe_median": float(base["roe_ann"].median()),
    "or_yoy_median": float(base["or_yoy"].median()),
}
print("\n=== 全市场 2016-09 基线 ===")
print(base_stats)

pct_rows = []
for _, r in prof.iterrows():
    pct_rows.append({
        "ts_code": r["ts_code"], "name": r["name"],
        "mv_pct": pct_rank(base["mv_yi"], r["mv_start"]),
        "pe_pct": pct_rank(base["pe_ttm"], r["pe_start"]),
        "roe_pct": pct_rank(base["roe_ann"], r["roe16_ann"]),
        "ory_pct": pct_rank(base["or_yoy"], r["or_yoy16"]),
    })
pct_df = pd.DataFrame(pct_rows)
print("\n=== 10x 股起点市场分位 (0=最小/最便宜/最差) ===")
print(pct_df.round(0).to_string(index=False))

med = {
    "10x股 mv 中位(亿)": prof["mv_start"].median(),
    "10x股 pe 中位": prof["pe_start"].median(),
    "10x股 roe16 中位": prof["roe16_ann"].median(),
    "10x股 or_yoy16 中位": prof["or_yoy16"].median(),
    "10x股 rev 倍数中位": prof["rev_mult"].median(),
    "10x股 净利倍数中位": prof["ni_mult"].median(),
    "10x股 eps倍数中位": prof["eps_mult"].median(),
    "10x股 mv 分位中位": pct_df["mv_pct"].median(),
    "10x股 roe 分位中位": pct_df["roe_pct"].median(),
    "10x股 增速分位中位": pct_df["ory_pct"].median(),
    "10x股 首次10x年份分布": prof["first_10x_date"].str[:4].value_counts().sort_index().to_dict(),
}
print("\n=== 共同点摘要 ===")
for k, v in med.items():
    print(f"  {k}: {v}")

# ---------- 5. 2016 时点识别规则测试 ----------
mkt = base.dropna(subset=["roe_ann"]).copy()
ten_set = set(codes)
mkt["is_10x"] = mkt["ts_code"].isin(ten_set)


def test_rule(name, mask):
    sub = mkt[mask]
    hit = sub["is_10x"].sum()
    print(f"  {name:<42} 组合{len(sub):>5} 只 | 命中10x {hit:>2}/{len(ten)} | 精度 {sub['is_10x'].mean()*100 if len(sub) else 0:.2f}%")


print("\n=== 2016-09-12 时点可见信息的识别规则测试 ===")
test_rule("ROE>15% (质量派)", mkt["roe_ann"] > 15)
test_rule("营收增速>30% (成长派)", mkt["or_yoy"] > 30)
test_rule("PE<20 & PB<2 (便宜派)",
          (mkt["pe_ttm"] > 0) & (mkt["pe_ttm"] < 20) & (mkt["pb"] > 0) & (mkt["pb"] < 2))
test_rule("市值<80亿 (小市值)", mkt["mv_yi"] < 80)
test_rule("ROE>15% & PE<30 (质量+合理价)",
          (mkt["roe_ann"] > 15) & (mkt["pe_ttm"] > 0) & (mkt["pe_ttm"] < 30))
test_rule("ROE>10% & 增速>20% & 市值<100亿",
          (mkt["roe_ann"] > 10) & (mkt["or_yoy"] > 20) & (mkt["mv_yi"] < 100))
test_rule("ROE>15% & 增速>30% (质量+成长)",
          (mkt["roe_ann"] > 15) & (mkt["or_yoy"] > 30))
test_rule("全市场 (无规则)", mkt["pe_ttm"].notna() | mkt["roe_ann"].notna())

# 命中名单明细 (最佳单规则)
best = mkt[(mkt["roe_ann"] > 15) & (mkt["or_yoy"] > 30)]
caught = sorted(best[best["is_10x"]]["ts_code"].tolist())
missed = [c for c in codes if c not in caught]
print(f"\n  质量+成长规则命中: {caught}")
print(f"  漏掉: {missed}")

# ---------- 保存 ----------
out = {
    "window": [START, END],
    "universe_n": 2637,
    "tenbaggers": prof.replace({np.nan: None}).to_dict(orient="records"),
    "percentiles": pct_df.replace({np.nan: None}).to_dict(orient="records"),
    "market_baseline": base_stats,
    "common_summary": {k: (v if not isinstance(v, (np.integer, np.floating)) else (int(v) if isinstance(v, np.integer) else float(v))) for k, v in med.items()},
}
with open("/Users/james/workspace/stock_data/outputs/tenbagger_analysis.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1, default=str)
print("\n已保存 outputs/tenbagger_analysis.json")

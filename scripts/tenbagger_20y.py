# -*- coding: utf-8 -*-
"""
二十年十倍股分析 (2006-09-12 -> 2026-09-11, A股, 起点前已上市)
口径:
- 终值>=10x: 持有至今仍 >=10 倍
- 曾达>=10x: 历史上任意月末曾 >=10 倍 (月度分辨率)
起点画像(2006-09, 无前视): 2005年报财务 + 当日收盘价
  PE(静态) = close / eps_2005;  PB = close / bps_2005
  市值(亿) = close * (归母净资产_2005 / bps_2005) / 1e8
结果: outputs/tenbagger_20y.json
"""
import json
import sys

import numpy as np
import pandas as pd
import psycopg2

sys.path.insert(0, "/Users/james/workspace/stock_data")

START, END = "2006-09-12", "2026-09-11"
T0 = "2006-09-12"


def get_conn():
    return psycopg2.connect(host="/tmp", dbname="investassist", user="james")


def q(sql, params=None):
    with get_conn() as c:
        return pd.read_sql(sql, c, params=params)


# ---------- 1. 月度收益矩阵 (全 universe) ----------
sql_month = """
SELECT date_trunc('month', trade_date)::date AS m, ts_code,
       exp(sum(ln(1.0 + pct_chg/100.0))) - 1 AS mret, count(*) AS nd
FROM daily_quote
WHERE trade_date BETWEEN %(s)s AND %(e)s AND pct_chg IS NOT NULL
  AND ts_code ~ '\.(SH|SZ)$'
GROUP BY 1,2
"""
mon = q(sql_month, {"s": START, "e": END})
mon["m"] = pd.to_datetime(mon["m"])
mr = mon.pivot(index="m", columns="ts_code", values="mret").sort_index()

# 起点: 要求 2006-09~11 有交易 (起点前已上市)
sql_first = """
SELECT ts_code, min(trade_date) AS fd FROM daily_quote
WHERE trade_date BETWEEN '2006-09-01' AND '2006-11-01' AND ts_code ~ '\.(SH|SZ)$'
GROUP BY ts_code
"""
first = q(sql_first)
universe_codes = first["ts_code"].tolist()

# universe = 2006-09 前已上市的全部 (含后来退市者, 曾达口径必须计入)
mr = mr.reindex(columns=[c for c in universe_codes if c in mr.columns])
real = mr.notna()                        # 该月是否有真实交易
nav = (1 + mr.fillna(0.0)).cumprod()     # 停牌月按 0 收益, 净值延续
nav_real = nav.where(real)               # 仅真实交易月的净值 (退市后定格)

final = nav_real.ffill().iloc[-1]        # 最后一个真实交易月的累计值
evermax = nav_real.max()
first10x = nav_real.apply(lambda s: s.index[s >= 10.0][0] if (s >= 10.0).any() else pd.NaT)

still_listed = real.iloc[-1]
hold = final[(final >= 10.0) & still_listed]  # 终值>=10x 且 2026 仍上市有数据
touched = evermax[evermax >= 10.0]
print(f"universe (2006-09 前已上市): {len(universe_codes)}, 有月度数据: {nav.shape[1]}")
print(f"终值>=10x: {len(hold)}  ({len(hold)/nav.shape[1]*100:.1f}%)")
print(f"曾达>=10x(任意月末): {len(touched)}  ({len(touched)/nav.shape[1]*100:.1f}%)")
print(f"曾达但最终<10x (坐了过山车): {len(touched) - len([c for c in touched.index if c in hold.index])}")

# ---------- 2. 名单明细 ----------
sql_meta = """
SELECT ts_code, name, industry, list_status, delist_date FROM stocks
WHERE ts_code = ANY(%(c)s)
"""
meta = q(sql_meta, {"c": hold.index.tolist()}).set_index("ts_code")

# 起点收盘价 (<= 2006-09-12 最近一日)
sql_close = """
SELECT DISTINCT ON (ts_code) ts_code, trade_date, close FROM daily_quote
WHERE trade_date BETWEEN '2006-08-01' AND '2006-09-12' AND close IS NOT NULL
  AND ts_code = ANY(%(c)s)
ORDER BY ts_code, trade_date DESC
"""
close_s = q(sql_close, {"c": nav.columns.tolist()}).set_index("ts_code")["close"]

# 2005 年报财务
sql_fi = """
SELECT f.ts_code, f.roe, f.bps, f.basic_eps, f.or_yoy, f.grossprofit_margin,
       b.total_hldr_eqy_exc_min_int AS eqy
FROM financial_indicator f
LEFT JOIN balance_sheet b ON b.ts_code=f.ts_code AND b.report_year=f.report_year AND b.report_type=f.report_type
WHERE f.report_year=2005 AND f.report_type='4'
"""
fi05 = q(sql_fi).set_index("ts_code")

# income 2004/2005/2025 年报
sql_inc = """
SELECT ts_code, report_year, revenue, n_income_attr_p, basic_eps
FROM income WHERE report_type='4' AND report_year IN (2004, 2005, 2025)
"""
inc = q(sql_inc)
inc04 = inc[inc["report_year"] == 2004].set_index("ts_code")
inc05 = inc[inc["report_year"] == 2005].set_index("ts_code")
inc25 = inc[inc["report_year"] == 2025].set_index("ts_code")


def feats(code):
    if code not in close_s.index or code not in fi05.index:
        return None
    cl = float(close_s[code])
    f = fi05.loc[code]
    bps = float(f["bps"]) if pd.notna(f["bps"]) and f["bps"] > 0 else None
    i5 = inc05.loc[code] if code in inc05.index else None
    # basic_eps 2005 老股多为 NaN, 用 净利/股本 推算 (股本=权益/bps)
    eqy_ = float(f["eqy"]) if pd.notna(f["eqy"]) and f["eqy"] else None
    if i5 is not None and pd.notna(i5["basic_eps"]) and i5["basic_eps"] > 0:
        eps = float(i5["basic_eps"])
    elif i5 is not None and eqy_ and bps and pd.notna(i5["n_income_attr_p"]):
        eps = float(i5["n_income_attr_p"]) / (eqy_ / bps)
    else:
        eps = None
    pe = cl / eps if eps and eps > 0 else None
    pb = cl / bps if bps else None
    eqy = float(f["eqy"]) if pd.notna(f["eqy"]) and f["eqy"] else None
    mv = cl * (eqy / bps) / 1e8 if eqy and bps else None
    roe = float(f["roe"]) if pd.notna(f["roe"]) else None
    ory = float(f["or_yoy"]) if pd.notna(f["or_yoy"]) else None
    gm = float(f["grossprofit_margin"]) if pd.notna(f["grossprofit_margin"]) else None
    i5 = inc05.loc[code] if code in inc05.index else None
    i25 = inc25.loc[code] if code in inc25.index else None
    i4 = inc04.loc[code] if code in inc04.index else None
    rev5 = float(i5["revenue"]) / 1e8 if i5 is not None and pd.notna(i5["revenue"]) else None
    rev25 = float(i25["revenue"]) / 1e8 if i25 is not None and pd.notna(i25["revenue"]) else None
    ni5 = float(i5["n_income_attr_p"]) / 1e8 if i5 is not None and pd.notna(i5["n_income_attr_p"]) else None
    ni25 = float(i25["n_income_attr_p"]) / 1e8 if i25 is not None and pd.notna(i25["n_income_attr_p"]) else None
    eps4 = float(i4["basic_eps"]) if i4 is not None and pd.notna(i4["basic_eps"]) else None
    eps_g = (eps / eps4 - 1) * 100 if eps and eps4 and eps4 > 0 and eps > 0 else None
    return dict(pe=pe, pb=pb, mv=mv, roe=roe, ory=ory, gm=gm, eps_g=eps_g,
                rev5=rev5, rev25=rev25, ni5=ni5, ni25=ni25)


rows = []
for code, ret in hold.sort_values(ascending=False).items():
    ft = feats(code)
    m = meta.loc[code] if code in meta.index else None
    nm = m["name"] if m is not None else code
    ind = m["industry"] if m is not None and pd.notna(m["industry"]) else ""
    t10 = first10x[code]
    rows.append({
        "ts_code": code, "name": nm, "industry": ind,
        "final_mult": round(float(ret), 1),
        "ever_max": round(float(evermax[code]), 1),
        "first_10x": str(t10.date()) if pd.notna(t10) else None,
        "delisted": bool(m is not None and pd.notna(m["delist_date"])),
        **({k: (round(v, 2) if v else None) for k, v in ft.items()} if ft else {}),
        "rev_mult": round(ft["rev25"] / ft["rev5"], 1) if ft and ft.get("rev25") and ft.get("rev5") and ft["rev5"] > 0 else None,
        "ni_mult": round(ft["ni25"] / ft["ni5"], 1) if ft and ft.get("ni25") and ft.get("ni5") and abs(ft["ni5"]) > 1e-6 else None,
    })
prof = pd.DataFrame(rows)
pd.set_option("display.width", 250)
print("\n=== 二十年终值>=10x 名单 ===")
print(prof[["ts_code", "name", "industry", "final_mult", "first_10x", "pe", "pb", "mv",
            "roe", "ory", "eps_g", "rev_mult", "ni_mult"]].to_string(index=False))

# ---------- 3. 全市场基线 + 分位 ----------
base_rows = []
for code in nav.columns:
    ft = feats(code)
    base_rows.append({"ts_code": code, **(ft or {})})
base = pd.DataFrame(base_rows).set_index("ts_code")


def pct_rank(series, val):
    s = series.dropna()
    if val is None or not np.isfinite(val):
        return None
    return float((s < val).mean() * 100)


print("\n=== 全市场 2006-09 基线 (2005年报口径) ===")
base_stats = {
    "n": len(base),
    "mv_median": float(base["mv"].median()),
    "pe_median": float(base["pe"][base["pe"] > 0].median()),
    "pb_median": float(base["pb"][base["pb"] > 0].median()),
    "roe_median": float(base["roe"].median()),
    "ory_median": float(base["ory"].median()),
}
print(base_stats)

prof_idx = prof.set_index("ts_code")
pct_rows = []
for code, r in prof_idx.iterrows():
    pct_rows.append({"ts_code": code, "name": r["name"],
                     "mv_pct": pct_rank(base["mv"], r.get("mv")),
                     "pe_pct": pct_rank(base["pe"], r.get("pe")),
                     "roe_pct": pct_rank(base["roe"], r.get("roe")),
                     "ory_pct": pct_rank(base["ory"], r.get("ory"))})
pct_df = pd.DataFrame(pct_rows).set_index("ts_code")
print("\n=== 起点市场分位 (0=最小/最差) ===")
print(pct_df.round(0).to_string())

med = {
    "20x股 mv 中位(亿)": prof["mv"].median(), "20x股 pe 中位": prof["pe"].median(),
    "20x股 pb 中位": prof["pb"].median(), "20x股 roe05 中位": prof["roe"].median(),
    "20x股 ory05 中位": prof["ory"].median(), "20x股 毛利率中位": prof["gm"].median(),
    "20x股 rev 倍数中位": prof["rev_mult"].median(), "20x股 净利倍数中位": prof["ni_mult"].median(),
    "20x股 mv 分位中位": pct_df["mv_pct"].median(), "20x股 pe 分位中位": pct_df["pe_pct"].median(),
    "20x股 roe 分位中位": pct_df["roe_pct"].median(), "20x股 ory 分位中位": pct_df["ory_pct"].median(),
    "首次10x年份分布": prof["first_10x"].str[:4].value_counts().sort_index().to_dict(),
    "行业分布": prof["industry"].value_counts().to_dict(),
}
print("\n=== 共同点摘要 ===")
for k, v in med.items():
    print(f"  {k}: {v}")

# 曾达但未守住 (过山车)
touched_codes = set(touched.index)
hold_codes = set(hold.index)
roller = touched_codes - hold_codes
roller_names = {}
sql_rn = "SELECT ts_code, name FROM stocks WHERE ts_code = ANY(%(c)s)"
rn = q(sql_rn, {"c": sorted(roller)})
for _, rr in rn.iterrows():
    roller_names[rr["ts_code"]] = rr["name"]
roller_finals = [(c, round(float(final[c]), 1)) for c in sorted(roller)]
print(f"\n曾达10x但终点<10x: {len(roller)} 只 (示例前30): ")
print([(c, roller_names.get(c), f) for c, f in roller_finals[:30]])

# ---------- 4. 2006 时点识别规则测试 ----------
mkt = base.dropna(subset=["roe"]).copy()
mkt["is_10x"] = mkt.index.isin(hold_codes)
mkt["is_touch"] = mkt.index.isin(touched_codes)


def test_rule(name, mask):
    sub = mkt[mask]
    hit = sub["is_10x"].sum()
    hit2 = sub["is_touch"].sum()
    print(f"  {name:<44} 组合{len(sub):>5} 只 | 终值10x {hit:>2}/{len(hold)} | 曾达10x {hit2:>2}/{len(touched)} | 精度 {sub['is_10x'].mean()*100 if len(sub) else 0:.2f}%")


print("\n=== 2006-09-12 时点可见信息的识别规则测试 ===")
test_rule("ROE>15% (质量派)", mkt["roe"] > 15)
test_rule("营收增速>30% (成长派)", mkt["ory"] > 30)
test_rule("PE<20 & PB<2 (便宜派)", (mkt["pe"] > 0) & (mkt["pe"] < 20) & (mkt["pb"] > 0) & (mkt["pb"] < 2))
test_rule("市值<50亿 (小市值)", mkt["mv"] < 50)
test_rule("ROE>15% & PE<30 (质量+合理价)", (mkt["roe"] > 15) & (mkt["pe"] > 0) & (mkt["pe"] < 30))
test_rule("ROE>10% & 增速>20%", (mkt["roe"] > 10) & (mkt["ory"] > 20))
test_rule("ROE>15% & 增速>30% (质量+成长)", (mkt["roe"] > 15) & (mkt["ory"] > 30))
peg_mask = (mkt["pe"] > 0) & (mkt["eps_g"] > 20) & (mkt["pe"] / mkt["eps_g"] < 1)
test_rule("PEG<1 & EPS增速>20% (林奇派)", peg_mask)
test_rule("全市场 (无规则)", mkt["roe"].notna())

best = mkt[(mkt["roe"] > 15) & (mkt["ory"] > 30)]
caught = sorted(best[best["is_10x"]].index.tolist())
print(f"\n  质量+成长规则命中: {caught}")

# ---------- 5. 保存 ----------
out = {
    "window": [START, END], "universe_n": int(nav.shape[1]), "universe_listed_at_start": len(universe_codes),
    "n_hold": int(len(hold)), "n_touched": int(len(touched)),
    "hold_list": prof.replace({np.nan: None}).to_dict(orient="records"),
    "roller_coasters": [{"ts_code": c, "name": roller_names.get(c),
                          "final_mult": f, "ever_max": round(float(evermax[c]), 1)}
                         for c, f in roller_finals],
    "market_baseline": base_stats,
    "common_summary": {k: (int(v) if isinstance(v, (np.integer,)) else
                           (float(v) if isinstance(v, np.floating) else v)) for k, v in med.items()},
}
with open("/Users/james/workspace/stock_data/outputs/tenbagger_20y.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1, default=str)
print("\n已保存 outputs/tenbagger_20y.json")

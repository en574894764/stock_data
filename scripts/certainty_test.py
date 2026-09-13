#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""成长确定性指标区分度验证:
2016-09-12 时点用 2013-2015 年报构造确定性指标(无前视), 检验:
1) 对前向 10 年收益的分层能力 (五分位)
2) 对 20 只十年十倍股的分布
指标: 盈利含金量 OCF/NI、ROE 水平与稳定性、毛利率趋势、应收-营收增速差
"""
import json
import numpy as np
import pandas as pd
import psycopg2

DB = dict(host="/tmp", dbname="investassist", user="james")
T0, T1 = "2016-09-12", "2026-09-11"

conn = psycopg2.connect(**DB)

def q(sql, p=None):
    return pd.read_sql(sql, conn, params=p)

# ---- universe: 2016-09 前已上市且有交易的 A 股 ----
uni = q("""
SELECT ts_code FROM daily_quote
WHERE trade_date BETWEEN '2016-09-01' AND '2016-11-01' AND ts_code ~ '\\.(SH|SZ)$'
GROUP BY ts_code HAVING min(trade_date) < '2016-11-01'
""")["ts_code"].tolist()
codes = uni
print(f"universe: {len(codes)}")

# ---- 2013-2015 年报: 净利/经营现金流/营收/应收/ROE/毛利率 ----
inc = q("""
SELECT ts_code, report_year, revenue, n_income_attr_p
FROM income WHERE report_type='4' AND report_year BETWEEN 2013 AND 2015
  AND ts_code = ANY(%(c)s)
""", {"c": codes})
cf = q("""
SELECT ts_code, report_year, n_cashflow_act
FROM cashflow WHERE report_type='4' AND report_year BETWEEN 2013 AND 2015
  AND ts_code = ANY(%(c)s)
""", {"c": codes})
bs = q("""
SELECT ts_code, report_year, accounts_receiv
FROM balance_sheet WHERE report_type='4' AND report_year IN (2013, 2015)
  AND ts_code = ANY(%(c)s)
""", {"c": codes})
fi = q("""
SELECT ts_code, report_year, roe, grossprofit_margin
FROM financial_indicator WHERE report_type='4' AND report_year BETWEEN 2013 AND 2015
  AND ts_code = ANY(%(c)s)
""", {"c": codes})

# ---- 前向 10 年总收益 ----
fwd = q("""
SELECT ts_code, exp(sum(ln(1.0 + pct_chg/100.0))) - 1 AS ret10, count(*) AS nd
FROM daily_quote
WHERE trade_date BETWEEN %(t0)s AND %(t1)s AND pct_chg IS NOT NULL
  AND ts_code ~ '\\.(SH|SZ)$'
GROUP BY ts_code
""", {"t0": T0, "t1": T1}).set_index("ts_code")
fwd = fwd[fwd["nd"] > 1800]["ret10"]
print(f"forward returns: {len(fwd)}")

tb20 = {r["ts_code"]: r["name"] for r in json.load(open("outputs/tenbagger_analysis.json"))["tenbaggers"]}

# ---- 指标构造 ----
inc_p = inc.pivot_table(index="ts_code", columns="report_year", values="n_income_attr_p")
cf_p = cf.pivot_table(index="ts_code", columns="report_year", values="n_cashflow_act")
rev_p = inc.pivot_table(index="ts_code", columns="report_year", values="revenue")
roe_p = fi.pivot_table(index="ts_code", columns="report_year", values="roe")
gm_p = fi.pivot_table(index="ts_code", columns="report_year", values="grossprofit_margin")
recv_p = bs.pivot_table(index="ts_code", columns="report_year", values="accounts_receiv")

feats = pd.DataFrame(index=inc_p.index)
# 盈利含金量: 3 年 OCF 合计 / 3 年净利合计 (要求合计净利>0)
ni_sum = inc_p.sum(axis=1, min_count=3)
ocf_sum = cf_p.sum(axis=1, min_count=3)
feats["ocf_ni"] = np.where(ni_sum > 0, ocf_sum / ni_sum, np.nan)
feats["ocf_ni"] = feats["ocf_ni"].clip(0, 5)
# ROE 水平 / 稳定性
feats["roe_avg"] = roe_p.mean(axis=1)
feats["roe_std"] = roe_p.std(axis=1)
# 毛利率趋势: 2015 - 2013
feats["gm_trend"] = gm_p[2015] - gm_p[2013]
# 应收-营收增速差 (2年 CAGR): 应收膨胀快于营收 = 收入质量差
rev_cagr = np.sqrt(rev_p[2015] / rev_p[2013].replace(0, np.nan)) - 1
recv_cagr = np.sqrt(recv_p[2015] / recv_p[2013].replace(0, np.nan)) - 1
feats["recv_gap"] = recv_cagr - rev_cagr
feats["recv_gap"] = feats["recv_gap"].clip(-1, 1)

# ---- 合成确定性分 (横截面百分位, 高=确定) ----
def pct(s, sign=1):
    r = s.rank(pct=True) * sign
    return r

comp = pd.DataFrame({
    "ocf_ni": pct(feats["ocf_ni"]),
    "roe_avg": pct(feats["roe_avg"]),
    "roe_std": pct(feats["roc_stab"] if "roc_stab" in feats else -feats["roe_std"]),  # 稳定=好
    "gm_trend": pct(feats["gm_trend"]),
    "recv_gap": pct(-feats["recv_gap"]),
})
score = comp.mean(axis=1, skipna=True)
score.name = "score"
data = feats.join(score).join(fwd, how="inner").dropna(subset=["score"])
print(f"scored universe: {len(data)}")

# ---- 五分位前向收益 ----
data["q"] = pd.qcut(data["score"], 5, labels=[1, 2, 3, 4, 5])
g = data.groupby("q")["ret10"]
summary = pd.DataFrame({
    "n": g.count(),
    "median": g.median(),
    "mean": g.mean(),
    "win_gt_1x": g.apply(lambda x: (x > 0).mean()),
    "win_gt_3x": g.apply(lambda x: (x >= 2).mean()),
    "tb20": data[data.index.isin(tb20)].groupby("q").size(),
}).fillna({"tb20": 0})
print("\n=== 确定性五分位 → 前向10年 (Q1=确定性最低, Q5=最高) ===")
print(summary.round(3).to_string())

# ---- 十倍股在确定性维度上的分布 ----
tb_idx = [c for c in tb20 if c in data.index]
print(f"\n十倍股 scored: {len(tb_idx)}/20")
tb_dist = data.loc[tb_idx][["ocf_ni", "roe_avg", "roe_std", "gm_trend", "recv_gap", "score", "ret10"]]
for c, name in tb20.items():
    if c in data.index:
        r = data.loc[c]
        print(f"  {name:<6} OCF/NI {r.ocf_ni:5.2f} ROE均 {r.roe_avg:5.1f}±{r.roe_std:4.1f} "
              f"毛利Δ {r.gm_trend:+5.1f} 应收差 {r.recv_gap:+.2f} 确定性分位 {r.score:.2f} (Q{int(r.q)}) "
              f"收益 {r.ret10:.0f}x")

# ---- 单指标单调性 ----
print("\n=== 单指标 Q5-Q1 差 (前向10年中位收益) ===")
for m, sign in [("ocf_ni", 1), ("roe_avg", 1), ("roe_std", -1), ("gm_trend", 1), ("recv_gap", -1)]:
    s = feats[m].dropna()
    common = s.index.intersection(fwd.index)
    s = s.loc[common]; f = fwd.loc[common]
    qbin = pd.qcut(s.rank(ascending=(sign < 0)), 5, labels=[1, 2, 3, 4, 5])
    med = f.groupby(qbin).median()
    print(f"  {m:10s} Q1 {med[1]:.2f}x | Q3 {med[3]:.2f}x | Q5 {med[5]:.2f}x | Q5-Q1 {(med[5]-med[1]):+.2f}x")

out = {
    "window": [T0, T1],
    "universe_scored": int(len(data)),
    "quintile_summary": summary.reset_index().astype({"q": int}).to_dict("records"),
    "tenbagger_dist": {c: {"name": tb20[c], "score": float(data.loc[c, "score"]),
                            "q": int(data.loc[c, "q"]), "ret10": float(data.loc[c, "ret10"])}
                       for c in tb_idx},
}
json.dump(out, open("outputs/certainty_test.json", "w"), ensure_ascii=False, indent=1, default=str)
print("\n-> outputs/certainty_test.json")

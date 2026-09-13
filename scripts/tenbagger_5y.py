# -*- coding: utf-8 -*-
"""
五年十倍股分析 (2021-09-12 -> 2026-09-11, A股, 起点前已上市)
+ 补: 十年窗口 (2016-09-12 -> 2026-09-11) 的曾达/过山车统计 (对齐二十年口径)
口径:
- 终值>=10x / 曾达>=10x(任意月末) / 过山车(曾达未守住)
起点画像(2021-09, 无前视): daily_basic 2021-09-10 最近交易日 (pe_ttm/pb/total_mv)
  + 2020年报 (roe/or_yoy/gm) + income 2020/2025 年报 (盈利倍数)
规则测试: 2021-09 时点可见信息
结果: outputs/tenbagger_5y.json
"""
import json
import sys

import numpy as np
import pandas as pd
import psycopg2

sys.path.insert(0, "/Users/james/workspace/stock_data")

START, END = "2021-09-12", "2026-09-11"
DB = dict(host="/tmp", dbname="investassist", user="james")


def q(sql, params=None):
    with psycopg2.connect(**DB) as c:
        return pd.read_sql(sql, c, params=params)


# ---------- 通用: 窗口分析 ----------
def window_stats(start, end, uni_from, uni_to):
    sql_month = """
    SELECT date_trunc('month', trade_date)::date AS m, ts_code,
           exp(sum(ln(1.0 + pct_chg/100.0))) - 1 AS mret
    FROM daily_quote
    WHERE trade_date BETWEEN %(s)s AND %(e)s AND pct_chg IS NOT NULL
      AND ts_code ~ '\.(SH|SZ)$'
    GROUP BY 1,2
    """
    mon = q(sql_month, {"s": start, "e": end})
    mon["m"] = pd.to_datetime(mon["m"])
    mr = mon.pivot(index="m", columns="ts_code", values="mret").sort_index()

    sql_uni = """
    SELECT ts_code FROM daily_quote
    WHERE trade_date BETWEEN %(f)s AND %(t)s AND ts_code ~ '\.(SH|SZ)$'
    GROUP BY ts_code
    """
    uni = q(sql_uni, {"f": uni_from, "t": uni_to})["ts_code"].tolist()
    mr = mr.reindex(columns=[c for c in uni if c in mr.columns])
    real = mr.notna()
    nav = (1 + mr.fillna(0.0)).cumprod()
    nav_real = nav.where(real)
    final = nav_real.ffill().iloc[-1]
    evermax = nav_real.max()
    first10x = nav_real.apply(lambda s: s.index[s >= 10.0][0] if (s >= 10.0).any() else pd.NaT)
    still = real.iloc[-1]
    hold = final[(final >= 10.0) & still]
    touched = evermax[evermax >= 10.0]
    n_rc = len(touched) - len([c for c in touched.index if c in hold.index])
    return dict(mr=mr, real=real, final=final, evermax=evermax, first10x=first10x,
                hold=hold, touched=touched, n_uni=mr.shape[1], n_hold=len(hold),
                n_touch=len(touched), n_rc=n_rc)


print("===== Part A: 五年窗口 =====")
w5 = window_stats(START, END, "2021-09-01", "2021-11-01")
print(f"universe: {w5['n_uni']}, 终值>=10x: {w5['n_hold']} ({w5['n_hold']/w5['n_uni']*100:.2f}%), "
      f"曾达: {w5['n_touch']} ({w5['n_touch']/w5['n_uni']*100:.2f}%), 过山车: {w5['n_rc']}")

print("\n===== Part B: 十年窗口曾达补充 =====")
w10 = window_stats("2016-09-12", "2026-09-11", "2016-09-01", "2016-11-01")
print(f"universe: {w10['n_uni']}, 终值>=10x: {w10['n_hold']} ({w10['n_hold']/w10['n_uni']*100:.2f}%), "
      f"曾达: {w10['n_touch']} ({w10['n_touch']/w10['n_uni']*100:.2f}%), 过山车: {w10['n_rc']}")

# ---------- 五年名单明细 ----------
hold5 = w5["hold"]
meta = q("SELECT ts_code, name, industry FROM stocks WHERE ts_code = ANY(%(c)s)",
         {"c": hold5.index.tolist()}).set_index("ts_code")

# 起点估值: daily_basic 2021-09-06~09-12 最近一行
db = q("""
SELECT DISTINCT ON (ts_code) ts_code, trade_date, pe_ttm, pb, total_mv
FROM daily_basic
WHERE trade_date BETWEEN '2021-09-06' AND '2021-09-12'
  AND ts_code = ANY(%(c)s)
ORDER BY ts_code, trade_date DESC
""", {"c": w5["mr"].columns.tolist()}).set_index("ts_code")

# 2020 年报财务
fi20 = q("""
SELECT ts_code, roe, or_yoy, grossprofit_margin FROM financial_indicator
WHERE report_year=2020 AND report_type='4'
""").set_index("ts_code")

# income 2020/2025 年报 (盈利倍数)
inc = q("""
SELECT ts_code, report_year, revenue, n_income_attr_p FROM income
WHERE report_type='4' AND report_year IN (2020, 2025)
""")
inc20 = inc[inc["report_year"] == 2020].set_index("ts_code")
inc25 = inc[inc["report_year"] == 2025].set_index("ts_code")

rows = []
for code, ret in hold5.sort_values(ascending=False).items():
    m = meta.loc[code] if code in meta.index else None
    b = db.loc[code] if code in db.index else None
    f = fi20.loc[code] if code in fi20.index else None
    i2 = inc20.loc[code] if code in inc20.index else None
    i5 = inc25.loc[code] if code in inc25.index else None
    nm = float(m["name"]) if False else (m["name"] if m is not None else code)
    ni_mult = None
    if i2 is not None and i5 is not None and pd.notna(i2["n_income_attr_p"]) and i2["n_income_attr_p"] and i2["n_income_attr_p"] > 0 and pd.notna(i5["n_income_attr_p"]):
        ni_mult = round(float(i5["n_income_attr_p"]) / float(i2["n_income_attr_p"]), 1)
    rev_mult = None
    if i2 is not None and i5 is not None and pd.notna(i2["revenue"]) and i2["revenue"] and i2["revenue"] > 0 and pd.notna(i5["revenue"]):
        rev_mult = round(float(i5["revenue"]) / float(i2["revenue"]), 1)
    f10 = w5["first10x"].get(code)
    rows.append(dict(
        ts_code=code, name=nm, industry=(m["industry"] if m is not None else None),
        final_mult=round(float(ret), 1), ever_max=round(float(w5["evermax"][code]), 1),
        first_10x=(str(f10.date()) if pd.notna(f10) else None),
        pe=(round(float(b["pe_ttm"]), 1) if b is not None and pd.notna(b["pe_ttm"]) and b["pe_ttm"] > 0 else None),
        pb=(round(float(b["pb"]), 2) if b is not None and pd.notna(b["pb"]) else None),
        mv=(round(float(b["total_mv"]) / 1e4, 0) if b is not None and pd.notna(b["total_mv"]) else None),  # 万元->亿
        roe=(round(float(f["roe"]), 1) if f is not None and pd.notna(f["roe"]) else None),
        ory=(round(float(f["or_yoy"]), 1) if f is not None and pd.notna(f["or_yoy"]) else None),
        gm=(round(float(f["grossprofit_margin"]), 1) if f is not None and pd.notna(f["grossprofit_margin"]) else None),
        ni_mult=ni_mult, rev_mult=rev_mult))

print(f"\n=== 五年终值>=10x 名单 ({len(rows)} 只) ===")
df5 = pd.DataFrame(rows)
print(df5[["ts_code", "name", "industry", "final_mult", "first_10x", "pe", "pb", "mv", "roe", "ory", "ni_mult"]].to_string(index=False))

# ---------- 过山车示例 ----------
rc5 = [(c, w5["evermax"][c], w5["final"][c]) for c in w5["touched"].index if c not in hold5.index]
rc5.sort(key=lambda x: -x[1])
rc_meta = q("SELECT ts_code, name FROM stocks WHERE ts_code = ANY(%(c)s)",
            {"c": [x[0] for x in rc5[:40]]}).set_index("ts_code") if rc5 else None
rc_rows = []
for c, em, fi_ in rc5:
    nm = rc_meta.loc[c, "name"] if rc_meta is not None and c in rc_meta.index else c
    rc_rows.append(dict(ts_code=c, name=nm, ever_max=round(float(em), 1), final_mult=round(float(fi_), 1)))
print(f"\n过山车示例前 15 (曾达10x 未守住): {[ (r['name'], r['ever_max'], r['final_mult']) for r in rc_rows[:15]]}")

# ---------- 起点画像 vs 全市场 ----------
med = lambda s: float(pd.Series([x for x in s if x is not None and pd.notna(x)]).median()) if any(x is not None and pd.notna(x) for x in s) else None
summary5 = dict(
    n_uni=int(w5["n_uni"]), n_hold=int(w5["n_hold"]), n_touch=int(w5["n_touch"]), n_rc=int(w5["n_rc"]),
    pct_hold=round(w5["n_hold"] / w5["n_uni"] * 100, 2), pct_touch=round(w5["n_touch"] / w5["n_uni"] * 100, 2),
    hold_pe_med=med([r["pe"] for r in rows]), hold_pb_med=med([r["pb"] for r in rows]),
    hold_mv_med=med([r["mv"] for r in rows]), hold_roe_med=med([r["roe"] for r in rows]),
    hold_ory_med=med([r["ory"] for r in rows]), hold_gm_med=med([r["gm"] for r in rows]),
    hold_ni_mult_med=med([r["ni_mult"] for r in rows]), hold_rev_mult_med=med([r["rev_mult"] for r in rows]),
)
# 全市场基线 (2021-09)
base_db = db
base_fi = fi20
summary5["mkt_pe_med"] = round(float(base_db["pe_ttm"].dropna()[base_db["pe_ttm"] > 0].median()), 1)
summary5["mkt_pb_med"] = round(float(base_db["pb"].dropna().median()), 2)
summary5["mkt_mv_med"] = round(float(base_db["total_mv"].dropna().median()) / 1e4, 0)
summary5["mkt_roe_med"] = round(float(base_fi["roe"].dropna().median()), 1)
summary5["mkt_ory_med"] = round(float(base_fi["or_yoy"].dropna().median()), 1)
# 分位
def pct_rank(series, val):
    s = series.dropna()
    return float((s < val).sum() / len(s) * 100)
if rows:
    pe_s = base_db["pe_ttm"].dropna(); pe_s = pe_s[pe_s > 0]
    mv_s = base_db["total_mv"].dropna(); roe_s = base_fi["roe"].dropna(); ory_s = base_fi["or_yoy"].dropna()
    summary5["hold_pe_pct_med"] = round(med([pct_rank(pe_s, r["pe"]) for r in rows if r["pe"]]), 0)
    summary5["hold_mv_pct_med"] = round(med([pct_rank(mv_s, r["mv"] * 1e4) for r in rows if r["mv"]]), 0)
    summary5["hold_roe_pct_med"] = round(med([pct_rank(roe_s, r["roe"]) for r in rows if r["roe"] is not None]), 0)
    summary5["hold_ory_pct_med"] = round(med([pct_rank(ory_s, r["ory"]) for r in rows if r["ory"] is not None]), 0)

# 首次10x年份分布 & 行业分布
fy = {}
for r in rows:
    y = (r["first_10x"] or "")[:4]
    fy[y] = fy.get(y, 0) + 1
ind_d = {}
for r in rows:
    k = r["industry"] or "未知"
    ind_d[k] = ind_d.get(k, 0) + 1
summary5["first10x_years"] = fy
summary5["industry_dist"] = dict(sorted(ind_d.items(), key=lambda x: -x[1]))

print("\n=== 五年共同点摘要 ===")
print(json.dumps(summary5, ensure_ascii=False, indent=1))

# ---------- 2021 时点规则测试 ----------
# 组合条件全部基于 2021-09 时点可见: db(pe_ttm/pb/mv) + fi20(roe/or_yoy)
feat_df = base_db.join(fi20[["roe", "or_yoy"]], how="left")
feat_df = feat_df[feat_df.index.isin(w5["mr"].columns)]
hold_set = set(hold5.index)
touch_set = set(w5["touched"].index)

def rule_test(name, mask):
    sel = feat_df[mask].index.tolist()
    n = len(sel)
    h = len([c for c in sel if c in hold_set])
    t = len([c for c in sel if c in touch_set])
    prec = h / n * 100 if n else 0.0
    hits = [str(meta.loc[c, "name"]) if c in meta.index else c for c in sel if c in hold_set][:15]
    print(f"  {name:42s} 组合 {n:5d} 只 | 终值10x {h}/{len(hold_set)} | 曾达 {t}/{len(touch_set)} | 精度 {prec:.2f}%")
    return dict(rule=name, n=n, hit_hold=h, hit_touch=t, precision=round(prec, 2), hold_hits=hits)

pe = feat_df["pe_ttm"]; mv = feat_df["total_mv"]; roe = feat_df["roe"]; ory = feat_df["or_yoy"]
print("\n=== 2021-09-12 时点规则测试 ===")
base_prec = len(hold_set) / feat_df.shape[0] * 100
print(f"  随机基线: {base_prec:.2f}%  (universe {feat_df.shape[0]})")
tests = [
    rule_test("ROE>15% (质量派)", (roe > 15).fillna(False)),
    rule_test("营收增速>30% (成长派)", (ory > 30).fillna(False)),
    rule_test("PE<20 & PB<2 (便宜派)", ((pe > 0) & (pe < 20) & (feat_df["pb"] < 2)).fillna(False)),
    rule_test("市值<100亿 (小市值)", (mv < 100e4).fillna(False)),
    rule_test("ROE>10% & 增速>20%", ((roe > 10) & (ory > 20)).fillna(False)),
    rule_test("ROE>15% & 增速>30% (质量+成长)", ((roe > 15) & (ory > 30)).fillna(False)),
    rule_test("PE>60 (高估值派)", ((pe > 60)).fillna(False)),
    rule_test("PE<20 (纯便宜)", ((pe > 0) & (pe < 20)).fillna(False)),
]

# 十年窗口曾达名单也存一份(前60)
rc10 = [(c, float(w10["evermax"][c]), float(w10["final"][c])) for c in w10["touched"].index if c not in w10["hold"].index]
rc10.sort(key=lambda x: -x[1])
meta10 = q("SELECT ts_code, name FROM stocks WHERE ts_code = ANY(%(c)s)",
           {"c": [x[0] for x in rc10[:60]]}).set_index("ts_code") if rc10 else None
rc10_rows = [dict(ts_code=c, name=(meta10.loc[c, "name"] if meta10 is not None and c in meta10.index else c),
                  ever_max=round(em, 1), final_mult=round(fi_, 1)) for c, em, fi_ in rc10[:60]]

out = dict(
    window=[START, END], universe_n=int(w5["n_uni"]),
    ten_year_extra=dict(universe_n=int(w10["n_uni"]), n_hold=int(w10["n_hold"]),
                        n_touch=int(w10["n_touch"]), n_rc=int(w10["n_rc"]),
                        roller_coasters=rc10_rows),
    hold_list=rows, roller_coasters=rc_rows, summary=summary5, rule_tests=tests,
    base_precision=round(base_prec, 2),
)
json.dump(out, open("outputs/tenbagger_5y.json", "w"), ensure_ascii=False, indent=1, default=str)
print("\n已保存 outputs/tenbagger_5y.json")

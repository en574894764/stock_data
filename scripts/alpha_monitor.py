#!/usr/bin/env python3
"""信号质量监控 (可视化脚本 ①): α 预估准确性 → 分周期/分股票域 可视化 → 交互式 HTML
====================================================================================
需求承载: "每版策略预估的 α 在不同股票不同周期上的准确性可视化"

  ① IC 时序: 每期 IC 柱状 + 12 期滚动均线 + 累计 IC (版本叠加对比)
  ② 分年度 IC: 年×月 热力图 (衰减从哪开始)
  ③ 分股票域 IC: 按市值五分位 / 按波动五分位 的 IC 与 ICIR (预测在哪类股票上失效)
  ④ 十分位兑现: 各分位年化收益 (D10 倒挂高亮)
  ⑤ 年度 IC 汇总表: 均值/ICIR/正率

口径: T 日合成分 → T+1~T+21 前向收益, 截面 Spearman 秩相关 (与 factor_eval/alpha_accuracy 一致)

用法:
  python3 scripts/alpha_monitor.py --strategy prod_6f_eq --start 2019-01-01
  python3 scripts/alpha_monitor.py --strategy prod_6f_eq --strategy 'prod_6f_eq@win=92-98,topn=30'  # 版本对比
输出: outputs/alpha_monitor_<tag>_<start>_<end>.html
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import factor_eval as fe  # noqa: E402
import strategy_lib as sl  # noqa: E402

CDN = "https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js"


def monitor(strategy, fwides, weights, daily_ret, uni, list_dates, rebal) -> dict:
    """返回 ic(Series) / bucket_ic(dict) / decile_ret(Series)"""
    fwd = fe.fwd_from_daily(daily_ret)
    ln_mv, ivol = fwides.get("ln_mv"), fwides.get("ivol_60")
    ics, buckets = [], {"size": {q: [] for q in range(1, 6)}, "vol": {q: [] for q in range(1, 6)}}
    dec = {q: [] for q in range(1, 11)}
    dr_cols = daily_ret.columns
    for t in rebal:
        score, _ = sl.score_cross(fwides, weights, t, uni, list_dates)
        if score is None or t not in fwd.index:
            continue
        f = fwd.loc[t]
        common = score.dropna().index.intersection(f.dropna().index)
        if len(common) < 30:
            continue
        s, fr = score[common], f[common]
        ics.append((t, s.rank().corr(fr.rank())))
        # 分域: 市值/波动 五分位 (域内秩相关)
        for dim, mat in [("size", ln_mv), ("vol", ivol)]:
            if mat is None or t not in mat.index:
                continue
            b = mat.loc[t].reindex(common).dropna()
            if len(b) < 250:
                continue
            try:
                bq = pd.qcut(b.rank(method="first"), 5, labels=False) + 1
            except ValueError:
                continue
            for q in range(1, 6):
                sub = bq[bq == q].index
                if len(sub) < 50:
                    continue
                buckets[dim][q].append(s[sub].rank().corr(fr[sub].rank()))
        # 十分位兑现
        try:
            dq = pd.qcut(s.rank(method="first"), 10, labels=False) + 1
        except ValueError:
            continue
        for q in range(1, 11):
            sub = dq[dq == q].index
            if len(sub) >= 10:
                dec[q].append(fr[sub].mean())
    ic = pd.Series(dict(ics))
    bucket_stats = {}
    for dim in buckets:
        bucket_stats[dim] = {}
        for q, lst in buckets[dim].items():
            if lst:
                se = pd.Series(lst)
                bucket_stats[dim][q] = {"ic": se.mean(), "icir": se.mean() / se.std() if se.std() > 0 else np.nan}
    dec_s = pd.Series({q: np.mean(v) for q, v in dec.items() if v})
    return {"ic": ic, "buckets": bucket_stats, "decile": dec_s}


def d2s(idx):
    return [pd.Timestamp(d).strftime("%Y-%m-%d") for d in idx]


def build_payload(results, start, end) -> dict:
    first = results[0]
    ic = first["ic"]
    dates = d2s(ic.index)
    roll = ic.rolling(12).mean()
    cum = ic.cumsum()
    multi = len(results) > 1
    grid = {"left": 64, "right": 24, "top": 30, "bottom": 44}

    ic_series = [{"name": "IC", "type": "bar", "data": [round(float(v), 4) for v in ic.values],
                  "itemStyle": {"color": "#5470c6"}}]
    for r in results:
        ic_series.append({"name": r["label"] + " 滚动12期", "type": "line", "showSymbol": False,
                          "data": [round(float(v), 4) if np.isfinite(v) else None
                                   for v in r["ic"].rolling(12).mean().reindex(ic.index).values],
                          "lineStyle": {"width": 2}})
    if not multi:
        ic_series.append({"name": "IC", "type": "line", "showSymbol": False,
                          "data": [round(float(v), 4) for v in roll.values], "lineStyle": {"width": 2},
                          "itemStyle": {"color": "#f2c14e"}})
    cum_series = [{"name": r["label"], "type": "line", "showSymbol": False,
                   "data": [round(float(v), 4) for v in r["ic"].cumsum().reindex(ic.index).values],
                   "lineStyle": {"width": 2}} for r in results]

    # 年×月热力
    hm = []
    for t, v in ic.items():
        hm.append([int(t.year), int(t.month) - 1, round(float(v), 4)])
    years = sorted({h[0] for h in hm})

    # 分域 IC
    def bucket_bars(dim, key):
        b = first["buckets"].get(dim, {})
        return [{"name": f"P{q*20}", "value": round(b[q][key], 4) if q in b and np.isfinite(b[q][key]) else None}
                for q in range(1, 6)]

    # 十分位
    dec = first["decile"]
    dec_ann = {q: (1 + v) ** 12 - 1 for q, v in dec.items()}
    d10_bad = 10 in dec_ann and 9 in dec_ann and dec_ann[10] < dec_ann[9]

    # 年度汇总
    yr = []
    for y, g in ic.groupby(ic.index.year):
        yr.append({"year": int(y), "ic": g.mean(), "icir": g.mean() / g.std() if g.std() > 0 else np.nan,
                   "pos": (g > 0).mean(), "n": len(g)})

    charts = {
        "ic": {"backgroundColor": "transparent",
               "tooltip": {"trigger": "axis", "backgroundColor": "#1b2129",
                           "textStyle": {"color": "#d8dee9", "fontSize": 11}},
               "legend": {"textStyle": {"color": "#9aa4b2"}, "top": 2}, "grid": grid,
               "xAxis": {"type": "category", "data": dates},
               "yAxis": {"type": "value", "splitLine": {"lineStyle": {"color": "#222a33"}}},
               "series": ic_series, "dataZoom": [{"type": "inside"}, {"type": "slider", "height": 16, "bottom": 4}]},
        "cum": {"backgroundColor": "transparent",
                "tooltip": {"trigger": "axis", "backgroundColor": "#1b2129",
                            "textStyle": {"color": "#d8dee9", "fontSize": 11}},
                "legend": {"textStyle": {"color": "#9aa4b2"}, "top": 2}, "grid": grid,
                "xAxis": {"type": "category", "data": dates},
                "yAxis": {"type": "value", "splitLine": {"lineStyle": {"color": "#222a33"}}},
                "series": cum_series},
        "hm": {"backgroundColor": "transparent",
               "tooltip": {"backgroundColor": "#1b2129", "textStyle": {"color": "#d8dee9", "fontSize": 11}},
               "grid": {"left": 64, "right": 90, "top": 30, "bottom": 44},
               "xAxis": {"type": "category", "data": [f"{m}月" for m in range(1, 13)]},
               "yAxis": {"type": "category", "data": [str(y) for y in years]},
               "visualMap": {"min": -0.15, "max": 0.35, "calculable": True, "orient": "vertical", "right": 4,
                             "top": "center", "inRange": {"color": ["#2f9e6e", "#222a33", "#d94f4f"]}},
               "series": [{"type": "heatmap", "data": hm,
                           "label": {"show": True, "fontSize": 9, "color": "#d8dee9"}}]},
        "bsize": {"backgroundColor": "transparent",
                  "tooltip": {"backgroundColor": "#1b2129", "textStyle": {"color": "#d8dee9", "fontSize": 11}},
                  "title": {"text": "按市值五分位 (P1小→P5大)", "textStyle": {"color": "#9aa4b2", "fontSize": 12}},
                  "grid": grid, "xAxis": {"type": "category", "data": [f"P{q*20}" for q in range(1, 6)]},
                  "yAxis": {"type": "value", "splitLine": {"lineStyle": {"color": "#222a33"}}},
                  "series": [{"type": "bar", "data": bucket_bars("size", "ic"),
                              "itemStyle": {"color": "#5470c6"}}]},
        "bvol": {"backgroundColor": "transparent",
                 "tooltip": {"backgroundColor": "#1b2129", "textStyle": {"color": "#d8dee9", "fontSize": 11}},
                 "title": {"text": "按波动五分位 (P1低波→P5高波)", "textStyle": {"color": "#9aa4b2", "fontSize": 12}},
                 "grid": grid, "xAxis": {"type": "category", "data": [f"P{q*20}" for q in range(1, 6)]},
                 "yAxis": {"type": "value", "splitLine": {"lineStyle": {"color": "#222a33"}}},
                 "series": [{"type": "bar", "data": bucket_bars("vol", "ic"),
                             "itemStyle": {"color": "#c8a165"}}]},
        "dec": {"backgroundColor": "transparent",
                "tooltip": {"backgroundColor": "#1b2129", "textStyle": {"color": "#d8dee9", "fontSize": 11}},
                "title": {"text": "十分位年化收益 (D1最低分→D10最高分)" +
                                 ("  ⚠ D10 倒挂" if d10_bad else ""), "textStyle": {"color": "#9aa4b2", "fontSize": 12}},
                "grid": grid, "xAxis": {"type": "category", "data": [f"D{q}" for q in dec_ann]},
                "yAxis": {"type": "value", "splitLine": {"lineStyle": {"color": "#222a33"}},
                          "axisLabel": {"formatter": "{value}%"}},
                "series": [{"type": "bar", "data": [{"value": round(v * 100, 1),
                                                     "itemStyle": {"color": "#d94f4f" if q == 10 and d10_bad else "#67c23a"}}
                                                    for q, v in dec_ann.items()]}]},
    }
    return charts, yr


HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>信号质量监控 — {title}</title>
<script src="{cdn}"></script>
<style>
body{{font-family:-apple-system,'PingFang SC',sans-serif;margin:0;background:#0f1419;color:#d8dee9;}}
h1{{font-size:20px;padding:16px 24px 4px;}} h2{{font-size:15px;margin:28px 24px 8px;color:#8ab4f8;}}
.meta{{padding:0 24px 8px;color:#9aa4b2;font-size:12px;line-height:1.8;}}
.box{{margin:0 24px 16px;}} .chart{{width:100%;height:360px;}} .half{{display:inline-block;width:calc(50% - 30px);vertical-align:top;}}
table{{border-collapse:collapse;font-size:12px;margin:8px 24px;width:calc(100% - 48px);}}
th,td{{border:1px solid #2a313a;padding:4px 8px;text-align:right;}} th{{background:#1b2129;color:#8ab4f8;}}
td:first-child,th:first-child{{text-align:left;}} .pos{{color:#f56c6c;}} .neg{{color:#67c23a;}}
</style></head><body>
<h1>信号质量监控 — {title}</h1>
<div class="meta">{meta}</div>
<h2>① IC 时序 (柱=单期, 线=滚动12期 {versions})</h2><div class="box"><div id="ic" class="chart"></div></div>
<h2>② 累计 IC (版本对比)</h2><div class="box"><div id="cum" class="chart" style="height:300px;"></div></div>
<h2>③ 分年度 IC 热力 (年×月)</h2><div class="box"><div id="hm" class="chart" style="height:320px;"></div></div>
<h2>④ 分股票域 IC (预测在哪类股票上失效)</h2>
<div class="half"><div id="bsize" class="chart" style="height:300px;"></div></div>
<div class="half"><div id="bvol" class="chart" style="height:300px;"></div></div>
<h2>⑤ 十分位兑现 (选股区健康度)</h2><div class="box"><div id="dec" class="chart" style="height:300px;"></div></div>
<h2>⑥ 年度 IC 汇总</h2>{year_table}
<script>
var C = {charts};
['ic','cum','hm','bsize','bvol','dec'].forEach(function(id){{
  var c = echarts.init(document.getElementById(id)); c.setOption(C[id]);}});
window.addEventListener('resize', function(){{document.querySelectorAll('.chart').forEach(function(el){{
  var i = echarts.getInstanceByDom(el); if(i) i.resize();}});}});
</script></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", action="append", default=None,
                    help="strategy_id, 可加 @win=92-98,topn=30 覆盖; 可重复 (版本对比)")
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--tag", default=None)
    args = ap.parse_args()
    specs = args.strategy or ["prod_6f_eq"]

    conn = fe.get_conn()
    uni, list_dates = fe.load_universe_filter(conn)
    strategies = sl.load_strategies(conn, specs)
    end = args.end or pd.Timestamp.today().strftime("%Y-%m-%d")
    load_start = (pd.Timestamp(args.start) - pd.Timedelta(days=40)).strftime("%Y-%m-%d")
    daily_ret = fe.load_daily_returns(conn, load_start, end)
    rebal = fe.rebalance_dates(daily_ret.index, args.start, end)
    print(f"股票池 {len(uni)} | 调仓 {len(rebal)} 期")

    results = []
    needed = set()
    for st in strategies:
        needed |= set(st["cfg"]["factors"])
    needed |= {"ln_mv", "ivol_60"}  # 分域基准
    fwides = {n: sl.load_factor(conn, n, args.start, end) for n in sorted(needed)}
    for st in strategies:
        r = monitor(st, fwides, st["cfg"]["factors"], daily_ret, uni, list_dates, rebal)
        ic = r["ic"]
        results.append({**st, **r})
        print(f"[{st['label']}] IC均值 {ic.mean()*100:.1f}% ICIR "
              f"{ic.mean()/ic.std():.2f} 正率 {(ic>0).mean()*100:.0f}% ({len(ic)} 期)")
    conn.close()

    charts, yr = build_payload(results, args.start, end)
    yh = ("<tr><th>年份</th><th>IC均值</th><th>ICIR</th><th>IC正率</th><th>期数</th></tr>")
    for r in yr:
        yh += (f"<tr><td>{r['year']}</td><td class=\"{'pos' if r['ic']>0 else 'neg'}\">{r['ic']*100:.1f}%</td>"
               f"<td>{r['icir']:.2f}</td><td>{r['pos']*100:.0f}%</td><td>{r['n']}</td></tr>")

    tag = args.tag or "+".join(s["id"] for s in strategies).replace("@", "_").replace(",", "-")
    meta = (f"区间 {args.start} ~ {end} | 口径: 合成分 → 20日前向收益 截面秩相关 | "
            f"分域基准: ln_mv / ivol_60 当期五分位")
    html = HTML.format(title=strategies[0]["label"], cdn=CDN, meta=meta,
                       versions="多版本叠加" if len(results) > 1 else "",
                       year_table=f"<table>{yh}</table>",
                       charts=json.dumps(charts, ensure_ascii=False, default=str))
    out = os.path.join(REPO, "outputs", f"alpha_monitor_{tag}_{args.start.replace('-', '')}_{end.replace('-', '')}.html")
    with open(out, "w") as f:
        f.write(html)
    print(f"✅ 报告已生成: {out}")


if __name__ == "__main__":
    main()

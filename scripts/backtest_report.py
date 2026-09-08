#!/usr/bin/env python3
"""策略回测报告 (可视化脚本 ③): 历史回放 → 净值/回撤/买卖/换手/成本/超额 → 交互式 HTML
====================================================================================
需求承载: "完整决策结果——买卖记录 + 资金曲线 (历史某周期)"

回放口径 (与 factor_eval 一致处标注):
  - T 日截面打分 → T+1 起持有 20 交易日, 等权, 组内日收益取均值 (=日度再平衡等权)
  - 成本: 实际换手 × 2 × 单边成本 (factor_eval 用固定满换手, 此处更接近实盘)
  - 股票池: 沪深非 ST, 上市满 120 自然日, 截面 ≥50

用法:
  python3 scripts/backtest_report.py --strategy prod_6f_eq --start 2019-01-01
  python3 scripts/backtest_report.py --strategy 'prod_6f_eq@win=92-98,topn=30' --tag win92
  python3 scripts/backtest_report.py --strategy prod_6f_eq --strategy 'prod_6f_eq@win=92-98,topn=30'  # 版本对比
输出: outputs/backtest_report_<tag>_<start>_<end>.html
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


# ---------------------------------------------------------------- 回放引擎
def replay(strategy, factors, daily_ret, uni, list_dates, rebal) -> dict:
    cfg = strategy["cfg"]
    fweights, top_n = cfg["factors"], cfg.get("top_n", 30)
    window, cost = cfg.get("window"), cfg.get("cost_one_side", 0.0015)
    rets, trades, periods = [], [], []
    contrib = {}
    prev, rank_map = {}, {}
    dr_idx, dr_cols = daily_ret.index, daily_ret.columns

    for t in rebal:
        score, _ = sl.score_cross(factors, fweights, t, uni, list_dates)
        if score is None:
            continue
        sel, ranked = sl.select_stocks(score, top_n, window)
        if len(sel) < 5:
            continue
        pos_dates = [d for d in dr_idx if d > t][:fe.REBAL]
        if not pos_dates:
            break
        exec_d = pos_dates[0]
        new = {c: 1.0 / len(sel) for c in sel}
        rank_map = {c: i + 1 for i, c in enumerate(ranked.index)}
        turnover = 0.5 * sum(abs(new.get(c, 0) - prev.get(c, 0)) for c in set(prev) | set(new))
        for c in sorted(set(prev) - set(new)):
            trades.append((exec_d, c, "SELL", prev[c], round(float(score.get(c, np.nan)), 3),
                           rank_map.get(c)))
        for c in sorted(set(new) - set(prev)):
            trades.append((exec_d, c, "BUY", new[c], round(float(score.get(c, np.nan)), 3),
                           rank_map.get(c)))
        row_pos = dr_idx.get_indexer(pos_dates)
        stocks = [c for c in sel if c in dr_cols]
        col_pos = dr_cols.get_indexer(stocks)
        m = daily_ret.iloc[row_pos, col_pos].to_numpy(dtype=np.float64)
        with np.errstate(invalid="ignore"):
            dr = np.nanmean(m, axis=1)
        dr = np.where(np.isfinite(dr), dr, 0.0)
        dr[0] -= turnover * 2 * cost
        for d, r in zip(pos_dates, dr):
            rets.append((d, r))
        with np.errstate(invalid="ignore"):
            pr = np.nansum(m, axis=0)
        for c, r in zip(stocks, pr):
            contrib[c] = contrib.get(c, 0.0) + new[c] * (0.0 if not np.isfinite(r) else r)
        periods.append({"rebal": t, "exec": exec_d, "n": len(sel), "turnover": turnover,
                        "cost": turnover * 2 * cost, "period_ret": float(np.prod(1 + dr) - 1)})
        prev = new

    nav = (1 + pd.Series(dict(rets)).sort_index()).cumprod()
    return {"nav": nav, "trades": pd.DataFrame(trades, columns=["exec_date", "ts_code", "action",
                                                                "weight", "score", "rank"]),
            "periods": pd.DataFrame(periods), "contrib": pd.Series(contrib).sort_values(ascending=False)}


def attach_prices(conn, trades: pd.DataFrame):
    """交易表挂上成交日收盘价"""
    if trades.empty:
        trades["price"] = np.nan
        return
    prices = {}
    cur = conn.cursor()
    for d, grp in trades.groupby("exec_date"):
        codes = tuple(grp["ts_code"])
        ph = ",".join(["%s"] * len(codes))
        cur.execute(f"SELECT ts_code, close FROM daily_quote WHERE trade_date = %s AND ts_code IN ({ph})",
                    (d,) + codes)
        prices.update(dict(cur.fetchall()))
    cur.close()
    trades["price"] = [prices.get(c, np.nan) for c in trades["ts_code"]]
    trades["exec_date"] = pd.to_datetime(trades["exec_date"])


def bench_series(conn, symbol, index, start, end) -> pd.Series:
    df = pd.read_sql(f"SELECT trade_date, pct_chg FROM index_daily WHERE symbol='{symbol}' "
                     f"AND trade_date >= '{start}' AND trade_date <= '{end}'", conn)
    if df.empty:
        return pd.Series(dtype=float)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    r = df.set_index("trade_date")["pct_chg"].sort_index() / 100.0
    return (1 + r.reindex(index).fillna(0)).cumprod()


def yearly_stats(nav: pd.Series, bench300: pd.Series, periods: pd.DataFrame) -> list:
    rows = []
    ret = nav.pct_change().dropna()
    bret = bench300.pct_change().dropna() if len(bench300) else pd.Series(dtype=float)
    for y, g in ret.groupby(ret.index.year):
        seg = nav[nav.index.year == y]
        vol = g.std() * np.sqrt(244)
        dd = (seg / seg.cummax() - 1).min()
        ann = (seg.iloc[-1] / seg.iloc[0]) ** (244 / len(g)) - 1 if len(g) > 20 else seg.iloc[-1] / seg.iloc[0] - 1
        ex = None
        if len(bret):
            bg = bret[bret.index.year == y]
            if len(bg) > 20:
                bseg = bench300[bench300.index.year == y]
                bann = (bseg.iloc[-1] / bseg.iloc[0]) ** (244 / len(bg)) - 1
                ex = ann - bann
        pg = periods[periods["exec"].dt.year == y] if len(periods) else pd.DataFrame()
        rows.append({"year": int(y), "ann": ann, "vol": vol, "sharpe": ann / vol if vol > 0 else np.nan,
                     "dd": dd, "ex300": ex,
                     "turnover": pg["turnover"].mean() if len(pg) else np.nan,
                     "cost": pg["cost"].sum() * 100 if len(pg) else np.nan})
    return rows


def d2s(idx) -> list:
    return [pd.Timestamp(d).strftime("%Y-%m-%d") for d in idx]


def pct(v, nd=1):
    return "-" if v is None or (isinstance(v, float) and not np.isfinite(v)) else f"{v*100:.{nd}f}%"


# ---------------------------------------------------------------- HTML
HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>策略回测报告 — {title}</title>
<script src="{cdn}"></script>
<style>
body{{font-family:-apple-system,'PingFang SC',sans-serif;margin:0;background:#0f1419;color:#d8dee9;}}
h1{{font-size:20px;padding:16px 24px 4px;}} h2{{font-size:15px;margin:28px 24px 8px;color:#8ab4f8;}}
.meta{{padding:0 24px 8px;color:#9aa4b2;font-size:12px;line-height:1.8;}}
.box{{margin:0 24px 16px;}} .chart{{width:100%;height:380px;}}
table{{border-collapse:collapse;font-size:12px;margin:8px 24px;width:calc(100% - 48px);}}
th,td{{border:1px solid #2a313a;padding:4px 8px;text-align:right;white-space:nowrap;}}
th{{background:#1b2129;color:#8ab4f8;position:sticky;top:0;}} td:first-child,th:first-child{{text-align:left;}}
.pos{{color:#f56c6c;}} .neg{{color:#67c23a;}}  /* A股口径: 涨红跌绿 */
#filter{{margin:4px 24px;padding:4px 8px;background:#1b2129;border:1px solid #2a313a;color:#d8dee9;border-radius:4px;}}
.twrap{{max-height:480px;overflow:auto;}}
</style></head><body>
<h1>策略回测报告 — {title}</h1>
<div class="meta">{meta}</div>
<h2>① 净值曲线 (调仓点已标注)</h2><div class="box"><div id="nav" class="chart"></div></div>
<h2>② 回撤 (水下曲线)</h2><div class="box"><div id="dd" class="chart" style="height:260px;"></div></div>
<h2>③ 相对沪深300超额</h2><div class="box"><div id="ex" class="chart" style="height:300px;"></div></div>
<h2>④ 分年度收益对比</h2><div class="box"><div id="yr" class="chart" style="height:300px;"></div></div>
<h2>⑤ 每期换手率与成本</h2><div class="box"><div id="to" class="chart" style="height:280px;"></div></div>
<h2>⑥ 年度绩效表</h2>{year_table}
<h2>⑦ 个股贡献 Top / Bottom 15 (权重×区间收益累计)</h2>{contrib_table}
<h2>⑧ 交易明细 ({n_trades} 笔, 可筛选)</h2>
<input id="filter" placeholder="输入代码 / BUY / SELL / 日期 筛选…">
<div class="twrap"><table id="trades"><thead><tr><th>成交日</th><th>代码</th><th>方向</th><th>权重</th><th>得分</th><th>池内排名</th><th>成交价</th></tr></thead><tbody>{trade_rows}</tbody></table></div>
<script>
var C = {charts};
function mk(id, opt){{var c = echarts.init(document.getElementById(id)); c.setOption(opt); return c;}}
mk('nav', C.nav); mk('dd', C.dd); mk('ex', C.ex); mk('yr', C.yr); mk('to', C.to);
document.getElementById('filter').addEventListener('input', function(){{
  var q = this.value.toUpperCase(); var rows = document.querySelectorAll('#trades tbody tr');
  var n = 0;
  rows.forEach(function(r){{var hit = !q || r.textContent.toUpperCase().indexOf(q) >= 0;
    r.style.display = hit ? '' : 'none'; if(hit) n++;}});
}});
window.addEventListener('resize', function(){{document.querySelectorAll('.chart').forEach(function(el){{
  var i = echarts.getInstanceByDom(el); if(i) i.resize();}});}});
</script></body></html>"""


def build_chart_payload(results, bench300, bench1000, meta) -> dict:
    first = results[0]
    nav, periods = first["nav"], first["periods"]
    dates = d2s(nav.index)
    base = {"type": "line", "showSymbol": False, "connectNulls": True}
    grid = {"left": 64, "right": 24, "top": 32, "bottom": 48}
    ax = {"type": "category", "data": dates, "axisLabel": {"rotate": 0}}
    tip = {"trigger": "axis", "backgroundColor": "#1b2129", "textStyle": {"color": "#d8dee9", "fontSize": 11}}

    nav_series = [{"name": r["label"], "data": [round(float(v), 4) for v in r["nav"].reindex(nav.index).values],
                   **base, "lineStyle": {"width": 2}} for r in results]
    if len(bench300):
        nav_series.append({"name": "沪深300", "data": [round(float(v), 4) for v in bench300.values],
                           **base, "itemStyle": {"color": "#9aa4b2"}})
    if len(bench1000):
        nav_series.append({"name": "中证1000", "data": [round(float(v), 4) for v in bench1000.values],
                           **base, "itemStyle": {"color": "#c8a165"}})
    # 调仓点: scatter on strategy nav
    pmap = {pd.Timestamp(r["exec"]).strftime("%Y-%m-%d"): r for _, r in periods.iterrows()}
    pts = []
    for d in dates:
        if d in pmap:
            r = pmap[d]
            pts.append({"value": [d, round(float(nav.loc[pd.Timestamp(d)]), 4)],
                        "turnover": f"{r['turnover']*100:.0f}%", "cost": f"{r['cost']*100:.2f}%",
                        "n": int(r["n"]), "ret": f"{r['period_ret']*100:.1f}%"})
    nav_series.append({"name": "调仓", "type": "scatter", "data": pts, "symbolSize": 5,
                       "itemStyle": {"color": "#f2c14e"},
                       "tooltip": {"formatter": "@@SCATTER_FMT@@"}})

    dd = (nav / nav.cummax() - 1) * 100
    ex_nav = nav / bench300 if len(bench300) else None
    ex_ret = (nav.pct_change() - (bench300.pct_change() if len(bench300) else 0)).dropna()
    roll_ex = ex_ret.rolling(120).mean().dropna() * 244 * 100 if len(ex_ret) else None

    yr_rows = yearly_stats(nav, bench300, periods)
    years = [str(r["year"]) for r in yr_rows]
    strat_y = [round(r["ann"] * 100, 1) for r in yr_rows]
    b300_y = [round((r["ann"] - r["ex300"]) * 100, 1) if r["ex300"] is not None else None for r in yr_rows]
    b1000_y = []
    if len(bench1000):
        bret = bench1000.pct_change().dropna()
        bseg_nav = bench1000
        for y in [r["year"] for r in yr_rows]:
            g = bret[bret.index.year == y]
            seg = bseg_nav[bseg_nav.index.year == y]
            b1000_y.append(round(((seg.iloc[-1] / seg.iloc[0]) ** (244 / len(g)) - 1) * 100, 1) if len(g) > 20 else None)
    else:
        b1000_y = [None] * len(yr_rows)

    pdates = d2s(periods["exec"])
    charts = {
        "nav": {"backgroundColor": "transparent", "tooltip": {"trigger": "axis", "axisPointer": {"type": "cross"},
                "backgroundColor": "#1b2129", "textStyle": {"color": "#d8dee9", "fontSize": 11}},
                "legend": {"textStyle": {"color": "#9aa4b2"}, "top": 4},
                "grid": grid, "xAxis": ax,
                "yAxis": {"type": "value", "scale": True, "splitLine": {"lineStyle": {"color": "#222a33"}}},
                "series": nav_series},
        "dd": {"backgroundColor": "transparent", "tooltip": tip, "grid": grid, "xAxis": ax,
               "yAxis": {"type": "value", "max": 0, "splitLine": {"lineStyle": {"color": "#222a33"}},
                         "axisLabel": {"formatter": "{value}%"}},
               "series": [{"name": "回撤", "type": "line", "showSymbol": False, "data": [round(float(v), 2) for v in dd.values],
                           "areaStyle": {"color": "rgba(103,194,58,0.25)"}, "itemStyle": {"color": "#67c23a"}}]},
        "ex": {"backgroundColor": "transparent", "tooltip": tip,
               "legend": {"textStyle": {"color": "#9aa4b2"}, "top": 4}, "grid": grid, "xAxis": ax,
               "yAxis": [{"type": "value", "scale": True, "splitLine": {"lineStyle": {"color": "#222a33"}}},
                         {"type": "value", "splitLine": {"show": False}}],
               "series": ([{"name": "累计超额净值(组合/300)", "type": "line", "showSymbol": False,
                            "data": [round(float(v), 4) for v in ex_nav.values], "lineStyle": {"width": 2},
                            "itemStyle": {"color": "#8ab4f8"}}] if ex_nav is not None else []) +
                          ([{"name": "滚动6月年化超额", "type": "line", "showSymbol": False, "yAxisIndex": 1,
                             "data": [round(float(v), 1) if np.isfinite(v) else None
                                      for v in roll_ex.reindex(nav.index).values],
                             "itemStyle": {"color": "#f2c14e"},
                             "markLine": {"data": [{"yAxis": 0}], "lineStyle": {"color": "#555"}}}] if roll_ex is not None else [])},
        "yr": {"backgroundColor": "transparent", "tooltip": tip, "legend": {"textStyle": {"color": "#9aa4b2"}, "top": 4},
               "grid": grid, "xAxis": {"type": "category", "data": years},
               "yAxis": {"type": "value", "splitLine": {"lineStyle": {"color": "#222a33"}},
                         "axisLabel": {"formatter": "{value}%"}},
               "series": [{"name": "组合", "type": "bar", "data": strat_y, "itemStyle": {"color": "#f56c6c"}},
                          {"name": "沪深300", "type": "bar", "data": b300_y, "itemStyle": {"color": "#9aa4b2"}},
                          {"name": "中证1000", "type": "bar", "data": b1000_y, "itemStyle": {"color": "#c8a165"}}]},
        "to": {"backgroundColor": "transparent", "tooltip": tip, "legend": {"textStyle": {"color": "#9aa4b2"}, "top": 4},
               "grid": grid, "xAxis": {"type": "category", "data": pdates},
               "yAxis": [{"type": "value", "splitLine": {"lineStyle": {"color": "#222a33"}},
                          "axisLabel": {"formatter": "{value}%"}}],
               "series": [{"name": "单期换手率", "type": "bar",
                           "data": [round(float(v) * 100, 1) for v in periods["turnover"]],
                           "itemStyle": {"color": "#5470c6"}},
                          {"name": "单期成本(bp)", "type": "line", "yAxisIndex": 0,
                           "data": [round(float(v) * 10000, 1) for v in periods["cost"]],
                           "itemStyle": {"color": "#f2c14e"}}]},
    }
    return charts, yr_rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", action="append", default=None, help="strategy_id, 可加 @win=92-98,topn=30 覆盖; 可重复")
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
    print(f"股票池 {len(uni)} | 调仓 {len(rebal)} 期 ({rebal[0].date()} ~ {rebal[-1].date()})")

    results = []
    for st in strategies:
        factors = {n: sl.load_factor(conn, n, args.start, end) for n in st["cfg"]["factors"]}
        r = replay(st, factors, daily_ret, uni, list_dates, rebal)
        attach_prices(conn, r["trades"])
        results.append({**st, **r})
        stt = fe.nav_stats(r["nav"])
        print(f"[{st['label']}] 年化 {pct(stt['ann_ret'])} 夏普 {stt['sharpe']:.2f} "
              f"回撤 {pct(stt['max_dd'])} 交易 {len(r['trades'])} 笔")

    bench300 = bench_series(conn, "000300.SH", results[0]["nav"].index, args.start, end)
    bench1000 = bench_series(conn, "000852.SH", results[0]["nav"].index, args.start, end)
    charts, yr_rows = build_chart_payload(results, bench300, bench1000, {})
    conn.close()

    # 年度表
    yh = "<tr><th>年份</th><th>年化</th><th>波动</th><th>夏普</th><th>最大回撤</th><th>超额vs300</th><th>平均换手</th><th>年成本</th></tr>"
    for r in yr_rows:
        yh += (f"<tr><td>{r['year']}</td><td class=\"{'pos' if r['ann']>0 else 'neg'}\">{pct(r['ann'])}</td>"
               f"<td>{pct(r['vol'])}</td><td>{r['sharpe']:.2f}</td><td class=\"neg\">{pct(r['dd'])}</td>"
               f"<td class=\"{'pos' if (r['ex300'] or 0)>0 else 'neg'}\">{pct(r['ex300'])}</td>"
               f"<td>{pct(r['turnover'])}</td><td>{r['cost']:.0f}bp</td></tr>")

    # 贡献表
    cb = results[0]["contrib"]
    ch = "<tr><th>代码</th><th>累计贡献</th></tr>"
    for c, v in list(cb.head(15).items()) + list(cb.tail(15).sort_values().items()):
        ch += f"<tr><td>{c}</td><td class=\"{'pos' if v>0 else 'neg'}\">{v*100:.1f}%</td></tr>"

    # 交易明细
    tr = results[0]["trades"]
    rows = ""
    for _, t in tr.iterrows():
        cls = "pos" if t["action"] == "BUY" else "neg"
        px = f"{t['price']:.2f}" if pd.notna(t["price"]) else "-"
        rk = int(t["rank"]) if pd.notna(t["rank"]) else "-"
        rows += (f"<tr><td>{t['exec_date'].strftime('%Y-%m-%d')}</td><td>{t['ts_code']}</td>"
                 f"<td class=\"{cls}\">{t['action']}</td><td>{t['weight']*100:.1f}%</td>"
                 f"<td>{t['score']:.2f}</td><td>{rk}</td><td>{px}</td></tr>")

    tag = args.tag or "+".join(s["id"] for s in strategies).replace("@", "_").replace(",", "-")
    meta = (f"区间 {args.start} ~ {end} | 调仓 {fe.REBAL} 交易日 | 单边成本 "
            f"{strategies[0]['cfg'].get('cost_one_side', 0.0015)*100:.2f}% (按实际换手计) | "
            f"选股 {'P%.0f-P%.0f 窗口' % tuple(strategies[0]['cfg']['window']) if strategies[0]['cfg'].get('window') else 'Top%d' % strategies[0]['cfg'].get('top_n', 30)} | "
            f"股票池: 沪深非ST 上市满120日")
    html = HTML.format(title=strategies[0]["label"], cdn=CDN, meta=meta, year_table=f"<table>{yh}</table>",
                       contrib_table=f"<table>{ch}</table>", n_trades=len(tr), trade_rows=rows,
                       charts=json.dumps(charts, ensure_ascii=False, default=str))
    # JSON 无法序列化 JS 函数 → 占位符替换 (调仓点悬浮提示)
    html = html.replace('"formatter": "@@SCATTER_FMT@@"',
                        '"formatter": function(p){var d=p.data; return d.name?d.name:(p.value[0]+"<br/>换手 "+d.turnover'
                        '+" 成本 "+d.cost+"<br/>持仓 "+d.n+" 只<br/>区间收益 "+d.ret);}')
    out = os.path.join(REPO, "outputs", f"backtest_report_{tag}_{args.start.replace('-', '')}_{end.replace('-', '')}.html")
    with open(out, "w") as f:
        f.write(html)
    print(f"✅ 报告已生成: {out}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""周一操作清单 + 历史三年 min_var 资金曲线 (组合优化落地后首份实操视图)
====================================================================================
Part 1: 假设周一 (下一交易日) 无持仓, prod_6f_eq / prod_lgbm_neu 将买入的完整清单
        (代码/名称/权重/金额/股数, 按 09-11 收盘价估算, 虚拟资金 100 万)
Part 2: 两策略 min_var 加权回测 (与生产配置一致: cap 10% / 窗口 60 日 / min_weight 0.5%)
        最近三年每期实际动作 + 资金曲线 vs 中证1000 (HTML)

用法: python3 scripts/monday_and_3y_backtest.py
输出: reports/monday_actions_<date>.md + outputs/minvar_3y_backtest.html
"""
import os
import sys

import numpy as np
import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
sys.path.insert(0, REPO)

import factor_eval as fe  # noqa: E402
import strategy_lib as sl  # noqa: E402
from generate_signals import compose_score, load_factor_cross, load_strategy  # noqa: E402

STRATS = ["prod_6f_eq", "prod_lgbm_neu"]
VIRTUAL_CASH = 1_000_000
COST = 0.0015
HIST = 60
MIN_W = 0.005
CAP = 0.10
SINCE = "2023-09-01"      # 展示起点 (三年)
WARM = "2023-05-01"       # 回测热身起点 (含 2-3 期建仓)
CDN = "https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js"


def get_names(conn) -> dict:
    cur = conn.cursor()
    cur.execute("SELECT ts_code, name FROM stocks")
    out = dict(cur.fetchall())
    cur.close()
    return out


def get_closes(conn, date) -> dict:
    cur = conn.cursor()
    cur.execute("SELECT ts_code, close FROM daily_quote WHERE trade_date = %s", (date,))
    out = {r[0]: float(r[1]) for r in cur.fetchall() if r[1]}
    cur.close()
    return out


def fmt_w(v):
    return f"{v*100:.1f}%"


# ---------------------------------------------------------------- Part 1: 周一清单
def monday_list(conn, sid, names, closes) -> dict:
    cur = conn.cursor()
    strategy = load_strategy(cur, sid)
    cur.close()
    cfg = strategy["config"]
    cross, factor_date = load_factor_cross(cur2(conn), cfg["factors"])
    t = pd.Timestamp(factor_date)
    uni, list_dates = fe.load_universe_filter(conn)
    keep = [c for c in cross.index if c in uni and list_dates.get(c) is not None
            and t > pd.Timestamp(list_dates[c]) + pd.Timedelta(days=cfg["min_history_days"])]
    pool = cross.loc[keep]
    score = compose_score(pool, cfg["factors"]).dropna().sort_values(ascending=False)
    target = score.head(cfg["top_n"])

    hist = sl.load_recent_returns(conn, list(target.index), cfg.get("cov_window", HIST))
    weights = sl.optimize_weights(hist, cfg.get("weighting", "min_var"),
                                  cfg.get("weight_cap", CAP))
    weights = weights.reindex(target.index).fillna(0.0)
    weights = weights[weights >= cfg.get("min_weight", MIN_W)]
    weights = sl.renormalize_with_cap(weights, cfg.get("weight_cap", CAP))

    rows = []
    for c in weights.index:
        value = VIRTUAL_CASH * weights[c]
        px = closes.get(c)
        vol = int(round(value / px / 100) * 100) if px and px > 0 else None
        rows.append({"code": c, "name": names.get(c, "?"), "score": float(target[c]),
                     "rank": int(target.index.get_loc(c)) + 1, "weight": float(weights[c]),
                     "value": value, "px": px, "vol": vol})
    return {"sid": sid, "name": strategy["name"], "factor_date": factor_date,
            "pool": len(pool), "rows": rows}


def cur2(conn):
    return conn.cursor()


# ---------------------------------------------------------------- Part 2: 三年回测
def run_minvar(conn, st, daily_ret, uni, list_dates, rebal, end) -> tuple:
    fweights, top_n = st["cfg"]["factors"], st["cfg"].get("top_n", 30)
    factors = {n: sl.load_factor(conn, n, "2019-01-01", end) for n in fweights}
    dr_idx, dr_cols = daily_ret.index, daily_ret.columns
    rets, actions = [], []
    prev_w = {}
    for t in rebal:
        if t < pd.Timestamp(WARM):
            continue
        score, _ = sl.score_cross(factors, fweights, t, uni, list_dates)
        if score is None:
            continue
        sel, _ = sl.select_stocks(score, top_n)
        if len(sel) < 5:
            continue
        pos_dates = [d for d in dr_idx if d > t][:fe.REBAL]
        if not pos_dates:
            break
        hist = daily_ret.loc[:t].iloc[-HIST:][[c for c in sel if c in dr_cols]]
        if hist.shape[1] < 5 or hist.shape[0] < 20:
            continue
        w = sl.optimize_weights(hist, "min_var", CAP)
        w = pd.Series(w.values, index=hist.columns)
        w = w[w >= MIN_W]
        w = sl.renormalize_with_cap(w, CAP)
        stocks, wmap = w.index.tolist(), dict(w)
        turnover = 0.5 * sum(abs(wmap.get(c, 0) - prev_w.get(c, 0))
                             for c in set(wmap) | set(prev_w))
        row_pos = dr_idx.get_indexer(pos_dates)
        col_pos = dr_cols.get_indexer(stocks)
        m = daily_ret.iloc[row_pos, col_pos].to_numpy(dtype=np.float64)
        with np.errstate(invalid="ignore"):
            dr = m @ w.values
        dr = np.where(np.isfinite(dr), dr, 0.0)
        dr[0] -= turnover * 2 * COST
        buys = [(c, wmap[c]) for c in stocks if prev_w.get(c, 0) < MIN_W]
        sells = [c for c in prev_w if c not in wmap]
        actions.append({"rebal": t, "exec": pos_dates[0], "n": len(stocks),
                        "buys": buys, "sells": sells, "turnover": turnover,
                        "ret": float(np.prod(1 + dr) - 1)})
        for d, r in zip(pos_dates, dr):
            rets.append((d, float(r)))
        prev_w = wmap
    nav = (1 + pd.Series(dict(rets)).sort_index()).cumprod()
    return nav, actions


def stats(nav: pd.Series) -> dict:
    ret = nav.pct_change().dropna()
    if len(ret) < 60:
        return {}
    years = len(ret) / 244
    ann = (nav.iloc[-1] / nav.iloc[0]) ** (1 / years) - 1
    vol = ret.std() * np.sqrt(244)
    dd = (nav / nav.cummax() - 1).min()
    return {"ann": ann, "sharpe": ann / vol if vol > 0 else np.nan, "dd": dd}


def pct(v, nd=1):
    return "-" if v is None or not np.isfinite(v) else f"{v*100:+.{nd}f}%"


# ---------------------------------------------------------------- HTML
HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>min_var 三年回测 — {title}</title>
<script src="{cdn}"></script>
<style>
body{{font-family:-apple-system,'PingFang SC',sans-serif;margin:0;background:#0f1419;color:#d8dee9;}}
h1{{font-size:20px;padding:16px 24px 4px;}} h2{{font-size:15px;margin:28px 24px 8px;color:#8ab4f8;}}
.meta{{padding:0 24px 8px;color:#9aa4b2;font-size:12px;line-height:1.8;}}
.box{{margin:0 24px 16px;}} .chart{{width:100%;height:420px;}}
table{{border-collapse:collapse;font-size:12px;margin:8px 24px;width:calc(100% - 48px);}}
th,td{{border:1px solid #2a313a;padding:4px 8px;text-align:right;white-space:nowrap;}}
th{{background:#1b2129;color:#8ab4f8;position:sticky;top:0;}} td:first-child,th:first-child{{text-align:left;}}
.pos{{color:#f56c6c;}} .neg{{color:#67c23a;}}
#filter{{margin:4px 24px;padding:4px 8px;background:#1b2129;border:1px solid #2a313a;color:#d8dee9;border-radius:4px;}}
.twrap{{max-height:520px;overflow:auto;}}
</style></head><body>
<h1>min_var 加权 · 三年回测 ({since} ~ {end})</h1>
<div class="meta">与生产配置一致: 选股 Top30 → Ledoit-Wolf Σ + 最小方差 + 单票≤10% + 剔除&lt;0.5% 持仓 | 单边成本 0.15% | 执行=T+1 开盘</div>
<h2>① 资金曲线 (归一化, vs 中证1000)</h2><div class="box"><div id="nav" class="chart"></div></div>
<h2>② 每期调仓动作汇总</h2>{period_table}
<h2>③ 完整交易明细 ({n_trades} 条, 可筛选)</h2>
<input id="filter" placeholder="输入代码 / 策略 / 日期 筛选…">
<div class="twrap"><table id="trades"><thead><tr><th>策略</th><th>执行日</th><th>代码</th><th>方向</th><th>权重</th></tr></thead><tbody>{trade_rows}</tbody></table></div>
<script>
var C = {charts};
echarts.init(document.getElementById('nav')).setOption(C.nav);
document.getElementById('filter').addEventListener('input', function(){{
  var q = this.value.toUpperCase(); var rows = document.querySelectorAll('#trades tbody tr');
  rows.forEach(function(r){{r.style.display = !q || r.textContent.toUpperCase().indexOf(q) >= 0 ? '' : 'none';}});
}});
window.addEventListener('resize', function(){{var i = echarts.getInstanceByDom(document.getElementById('nav')); if(i) i.resize();}});
</script></body></html>"""


def main():
    end = pd.Timestamp.today().strftime("%Y-%m-%d")
    conn = fe.get_conn()
    names = get_names(conn)
    closes = get_closes(conn, "2026-09-11")

    # ---- Part 1: 周一清单
    md = [f"# 周一操作清单 (无持仓假设, 信号截面 2026-09-11, 执行 2026-09-14 开盘)\n",
          f"- 虚拟资金 100 万 | 加权 = min_var (单票≤10%, 剔除<0.5%) | 股数按 09-11 收盘价估算 (整百股)\n"]
    for sid in STRATS:
        r = monday_list(conn, sid, names, closes)
        md.append(f"\n## {r['name']} ({sid})\n")
        md.append(f"- 股票池 {r['pool']} 只 → Top30 → min_var 剔零后 **{len(r['rows'])} 只**, "
                  f"信号日 {r['factor_date']}\n")
        md.append("| 代码 | 名称 | 合成分 | 池内排名 | 权重 | 金额(元) | 09-11收盘 | 股数 |")
        md.append("|---|---|---|---|---|---|---|---|")
        tot_v = 0
        for row in r["rows"]:
            tot_v += row["value"]
            md.append(f"| {row['code']} | {row['name']} | {row['score']:.2f} | {row['rank']} "
                      f"| {fmt_w(row['weight'])} | {row['value']:,.0f} "
                      f"| {row['px'] if row['px'] else '-'} | {row['vol'] if row['vol'] else '-'} |")
        md.append(f"\n合计 {len(r['rows'])} 只, 投入 {tot_v:,.0f} 元 ({tot_v/VIRTUAL_CASH*100:.1f}%)")
        print(f"[{sid}] 周一买入 {len(r['rows'])} 只")

    out_md = os.path.join(REPO, "reports", "monday_actions_20260914.md")
    with open(out_md, "w") as f:
        f.write("\n".join(md) + "\n")

    # ---- Part 2: 三年回测
    uni, list_dates = fe.load_universe_filter(conn)
    daily_ret = fe.load_daily_returns(conn, "2015-10-01", end)
    rebal = fe.rebalance_dates(daily_ret.index, "2019-01-01", end)  # 2019 网格 (lgbm 稀疏兼容)
    strategies = sl.load_strategies(conn, STRATS)

    navs, all_actions = {}, {}
    for st in strategies:
        nav, actions = run_minvar(conn, st, daily_ret, uni, list_dates, rebal, end)
        nav = nav[nav.index >= pd.Timestamp(SINCE)]
        navs[st["sid"]] = nav / nav.iloc[0]
        all_actions[st["sid"]] = [a for a in actions if a["exec"] >= pd.Timestamp(SINCE)]
        s = stats(nav)
        print(f"[{st['sid']}] 三年: 年化 {pct(s['ann'])} 夏普 {s['sharpe']:.2f} 回撤 {pct(s['dd'])} "
              f"| {len(all_actions[st['sid']])} 期")

    # 基准中证1000
    cur = conn.cursor()
    cur.execute("SELECT trade_date, pct_chg FROM index_daily WHERE symbol='000852.SH' "
                "AND trade_date >= %s", (SINCE,))
    bdf = pd.DataFrame(cur.fetchall(), columns=["trade_date", "pct_chg"])
    cur.close()
    bdf["pct_chg"] = pd.to_numeric(bdf["pct_chg"], errors="coerce") / 100.0
    bdf["trade_date"] = pd.to_datetime(bdf["trade_date"])
    bench = (1 + bdf.set_index("trade_date")["pct_chg"].sort_index()).cumprod()
    conn.close()

    # 汇总表
    md += [f"\n\n---\n\n# 三年 min_var 回测汇总 ({SINCE} ~ {end})\n",
           "| 策略 | 年化 | 夏普 | 最大回撤 | 同期中证1000 |",
           "|---|---|---|---|---|"]
    b_ann = bench.iloc[-1] ** (244 / len(bench)) - 1
    for sid in STRATS:
        s = stats(navs[sid])
        md.append(f"| {sid} | {pct(s['ann'])} | {s['sharpe']:.2f} | {pct(s['dd'])} | {pct(b_ann)} |")
    md.append(f"\n每期动作明细见 HTML: outputs/minvar_3y_backtest.html")
    with open(out_md, "w") as f:
        f.write("\n".join(md) + "\n")

    # ---- HTML
    dates = [d.strftime("%Y-%m-%d") for d in navs[STRATS[0]].index]
    base = {"type": "line", "showSymbol": False, "connectNulls": True}
    series = [{"name": sid, "data": [round(float(v), 4) for v in navs[sid].reindex(navs[STRATS[0]].index).values],
               **base, "lineStyle": {"width": 2}} for sid in STRATS]
    series.append({"name": "中证1000", "data": [round(float(v), 4) for v in bench.reindex(navs[STRATS[0]].index).ffill().fillna(1).values],
                   **base, "itemStyle": {"color": "#9aa4b2"}})
    charts = {"nav": {"backgroundColor": "transparent",
                      "tooltip": {"trigger": "axis", "backgroundColor": "#1b2129",
                                  "textStyle": {"color": "#d8dee9", "fontSize": 11}},
                      "legend": {"textStyle": {"color": "#9aa4b2"}, "top": 4},
                      "grid": {"left": 64, "right": 24, "top": 32, "bottom": 48},
                      "xAxis": {"type": "category", "data": dates},
                      "yAxis": {"type": "value", "scale": True,
                                "splitLine": {"lineStyle": {"color": "#222a33"}}},
                      "series": series}}

    # 每期动作汇总表
    ph = "<tr><th>策略</th><th>执行日</th><th>持仓</th><th>买入</th><th>卖出</th><th>换手</th><th>期收益</th></tr>"
    trade_rows = ""
    n_trades = 0
    for sid in STRATS:
        for a in all_actions[sid]:
            ph += (f"<tr><td>{sid}</td><td>{a['exec'].date()}</td><td>{a['n']}</td>"
                   f"<td>{len(a['buys'])}</td><td>{len(a['sells'])}</td>"
                   f"<td>{a['turnover']*100:.0f}%</td>"
                   f"<td class=\"{'pos' if a['ret'] > 0 else 'neg'}\">{a['ret']*100:+.1f}%</td></tr>")
            for c, w in a["buys"]:
                cls = "pos" if w > 0 else ""
                trade_rows += (f"<tr><td>{sid}</td><td>{a['exec'].date()}</td><td>{c}</td>"
                               f"<td class=\"pos\">BUY</td><td>{w*100:.1f}%</td></tr>")
                n_trades += 1
            for c in a["sells"]:
                trade_rows += (f"<tr><td>{sid}</td><td>{a['exec'].date()}</td><td>{c}</td>"
                               f"<td class=\"neg\">SELL</td><td>-</td></tr>")
                n_trades += 1

    html = HTML.format(title="+".join(STRATS), cdn=CDN, since=SINCE, end=end,
                       charts=str(charts).replace("'", '"').replace("None", "null")
                       .replace("True", "true").replace("False", "false"),
                       period_table=f"<table>{ph}</table>", trade_rows=trade_rows,
                       n_trades=n_trades)
    out_html = os.path.join(REPO, "outputs", "minvar_3y_backtest.html")
    with open(out_html, "w") as f:
        f.write(html)

    print(f"\n✅ 周一清单: {out_md}\n✅ 三年回测: {out_html}")


if __name__ == "__main__":
    main()

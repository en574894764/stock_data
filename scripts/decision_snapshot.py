#!/usr/bin/env python3
"""调仓决策快照 (可视化脚本 ②): 某一期调仓的完整决策上下文 → 交互式 HTML
====================================================================================
需求承载: "一个策略在不同标的上选出的持仓决策结果"

  ① 全市场打分分布 + 选股窗口/TopN 边界标注
  ② 入选股 × 因子 z 贡献热力表 (每只股靠什么入选)
  ③ 入选/落选边界对比 (最后几名入选 vs 最前几名落选, 差在哪个因子)
  ④ 持仓迁移 (vs 上一调仓期: 保留/新进/移出)

用法:
  python3 scripts/decision_snapshot.py                       # 最近一次 signal_log 调仓
  python3 scripts/decision_snapshot.py --date 2026-09-04     # 指定调仓日
  python3 scripts/decision_snapshot.py --strategy 'prod_6f_eq@win=92-98,topn=30'
输出: outputs/decision_snapshot_<strategy>_<date>.html
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


def pick_date(conn, sid, override, factor_index):
    """决策日: --date 优先; 否则 signal_log 最近调仓日; 再否则最新因子日"""
    if override:
        ts = pd.Timestamp(override)
        prior = [d for d in factor_index if d <= ts]
        if not prior:
            raise SystemExit(f"--date 早于因子数据起点: {override}")
        return prior[-1]
    cur = conn.cursor()
    cur.execute("SELECT MAX(trade_date) FROM signal_log WHERE strategy_id = %s", (sid,))
    d = cur.fetchone()[0]
    cur.close()
    if d is not None:
        prior = [x for x in factor_index if x <= pd.Timestamp(d)]
        if prior:
            return prior[-1]
    return factor_index[-1]


def prev_rebal(daily_index, d, n=fe.REBAL):
    """上一调仓日: 日历上往前 n 个交易日"""
    prior = [x for x in daily_index if x < d]
    return prior[-n] if len(prior) >= n else None


HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>调仓决策快照 — {title} @ {date}</title>
<script src="{cdn}"></script>
<style>
body{{font-family:-apple-system,'PingFang SC',sans-serif;margin:0;background:#0f1419;color:#d8dee9;}}
h1{{font-size:20px;padding:16px 24px 4px;}} h2{{font-size:15px;margin:28px 24px 8px;color:#8ab4f8;}}
.meta{{padding:0 24px 8px;color:#9aa4b2;font-size:12px;line-height:1.8;}}
.box{{margin:0 24px 16px;}} .chart{{width:100%;height:340px;}}
table{{border-collapse:collapse;font-size:12px;margin:8px 24px;width:calc(100% - 48px);}}
th,td{{border:1px solid #2a313a;padding:4px 7px;text-align:right;white-space:nowrap;}}
th{{background:#1b2129;color:#8ab4f8;position:sticky;top:0;}} td:first-child,th:first-child{{text-align:left;}}
.twrap{{max-height:560px;overflow:auto;}}
.tag{{display:inline-block;padding:1px 7px;border-radius:3px;font-size:11px;margin-right:4px;}}
.keep{{background:#2f5e3f;}} .add{{background:#7a3b3b;}} .out{{background:#4a4a55;}}
.cell{{display:inline-block;min-width:44px;padding:1px 5px;border-radius:3px;}}
</style></head><body>
<h1>调仓决策快照 — {title}</h1>
<div class="meta">调仓日 {date} | {meta}</div>
<h2>① 全市场打分分布与选股边界</h2><div class="box"><div id="hist" class="chart"></div></div>
<h2>② 入选持仓 × 因子 z 贡献 (绿=正向贡献 红=负向; 数值=权重×z)</h2>
<div class="twrap">{hold_table}</div>
<h2>③ 入选 / 落选边界对比</h2>{border_table}
<h2>④ 持仓迁移 (vs 上一期 {prev_date})</h2>{migrate}
<script>
var C = {charts};
var c = echarts.init(document.getElementById('hist')); c.setOption(C.hist);
window.addEventListener('resize', function(){{var i=echarts.getInstanceByDom(document.getElementById('hist')); if(i)i.resize();}});
</script></body></html>"""


def zcolor(v):
    """A股口径: 正贡献红(涨), 负贡献绿"""
    if v is None or not np.isfinite(v):
        return "-"
    r = max(0, min(255, int(255 - v * 40)))
    g = max(0, min(255, int(255 + v * 40)))
    b = 160
    return (f"<span class=\"cell\" style=\"background:rgba({r},{g},{b},0.85);color:#10151b\">"
            f"{v:+.2f}</span>")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="prod_6f_eq", help="strategy_id, 可加 @win=92-98,topn=30")
    ap.add_argument("--date", default=None, help="调仓日 YYYY-MM-DD (默认最近一次 signal_log)")
    args = ap.parse_args()

    conn = fe.get_conn()
    uni, list_dates = fe.load_universe_filter(conn)
    strategy = sl.load_strategies(conn, [args.strategy])[0]
    cfg = strategy["cfg"]
    top_n, window = cfg.get("top_n", 30), cfg.get("window")
    factor_names = list(cfg["factors"])
    fwides = {n: sl.load_factor(conn, n, "2018-01-01", "2026-12-31") for n in factor_names}

    d = pick_date(conn, strategy["sid"], args.date, fwides[factor_names[0]].index)
    dp = prev_rebal(fwides[factor_names[0]].index, d)
    print(f"决策日 {d.date()} | 上一调仓 {dp.date() if dp is not None else '无'}")

    score, zdf = sl.score_cross(fwides, cfg["factors"], d, uni, list_dates)
    if score is None:
        raise SystemExit("该日截面不足 50, 换 --date")
    sel, ranked = sl.select_stocks(score, top_n, window)
    print(f"截面 {len(score)} 只 | 入选 {len(sel)} 只: {', '.join(sel[:5])} ...")

    # ① 打分分布 + 边界
    s = score.dropna()
    hist, edges = np.histogram(s, bins=60)
    labels = [f"{edges[i]:.1f}" for i in range(len(hist))]
    colors = ["#3a4450"] * len(hist)
    sel_scores = s[s.index.isin(sel)]
    lo, hi = sel_scores.min(), sel_scores.max()
    for i in range(len(hist)):
        c0, c1 = edges[i], edges[i + 1]
        if c1 >= lo and c0 <= hi:
            colors[i] = "#d94f4f" if (window or True) else "#d94f4f"
    charts = {"hist": {
        "backgroundColor": "transparent",
        "tooltip": {"backgroundColor": "#1b2129", "textStyle": {"color": "#d8dee9", "fontSize": 11}},
        "title": {"text": f"全市场 {len(s)} 只打分分布 (红=入选区间 {lo:.1f} ~ {hi:.1f})",
                  "textStyle": {"color": "#9aa4b2", "fontSize": 12}},
        "grid": {"left": 64, "right": 24, "top": 44, "bottom": 40},
        "xAxis": {"type": "category", "data": labels, "name": "合成分"},
        "yAxis": {"type": "value", "splitLine": {"lineStyle": {"color": "#222a33"}}},
        "series": [{"type": "bar", "data": [{"value": int(v), "itemStyle": {"color": colors[i]}}
                                            for i, v in enumerate(hist)]}],
    }}

    # ② 入选热力表: 每因子贡献 = w*z
    rank_map = {c: i + 1 for i, c in enumerate(ranked.index)}
    hdr = "<tr><th>#</th><th>代码</th><th>合成分</th>" + "".join(f"<th>{n}</th>" for n in factor_names) + "</tr>"
    rows = ""
    for i, c in enumerate(sel):
        zr = zdf.loc[c] if c in zdf.index else pd.Series(dtype=float)
        cells = "".join(f"<td>{zcolor(float(zr[n])) if n in zr.index and pd.notna(zr[n]) else '-'}</td>"
                        for n in factor_names)
        rows += f"<tr><td>{i+1}</td><td>{c}</td><td><b>{score[c]:+.2f}</b></td>{cells}</tr>"
    hold_table = f"<table><thead>{hdr}</thead><tbody>{rows}</tbody></table>"

    # ③ 边界对比: 最后 5 入选 vs 最前 5 落选 (同窗口口径)
    border_pool = ranked.index.tolist()
    sel_set = set(sel)
    in_edge = [c for c in border_pool if c in sel_set][-5:]
    out_edge = [c for c in border_pool if c not in sel_set][:5]
    bh = ("<tr><th>组</th><th>代码</th><th>合成分</th><th>池内排名</th>" +
          "".join(f"<th>{n}</th>" for n in factor_names) + "</tr>")
    def brow(tag, c):
        zr = zdf.loc[c] if c in zdf.index else pd.Series(dtype=float)
        cells = "".join(f"<td>{zcolor(float(zr[n])) if n in zr.index and pd.notna(zr[n]) else '-'}</td>"
                        for n in factor_names)
        return (f"<tr><td>{tag}</td><td>{c}</td><td>{score[c]:+.2f}</td>"
                f"<td>{rank_map.get(c, '-')}</td>{cells}</tr>")
    for c in in_edge:
        bh += brow("入选末位", c)
    for c in out_edge:
        bh += brow("落选首位", c)
    border_table = f"<table><thead>{bh}</thead></table>"

    # ④ 迁移
    keep_html, add_html, out_html = "-", "-", "-"
    if dp is not None:
        pscore, _ = sl.score_cross(fwides, cfg["factors"], dp, uni, list_dates)
        if pscore is not None:
            psel, _ = sl.select_stocks(pscore, top_n, window)
            keep, add, out = set(sel) & set(psel), set(sel) - set(psel), set(psel) - set(sel)
            def lst(x):
                xs = sorted(x)
                return f"{len(xs)} 只: " + ", ".join(xs) if xs else "无"
            keep_html, add_html, out_html = lst(keep), lst(add), lst(out)
    migrate = (f"<table><tr><th>保留</th><td>{keep_html}</td></tr>"
               f"<tr><th>新进</th><td>{add_html}</td></tr><tr><th>移出</th><td>{out_html}</td></tr></table>")
    conn.close()

    meta = (f"选股 {'P%.0f-P%.0f 窗口' % tuple(window) if window else 'Top%d' % top_n} | "
            f"截面 {len(s)} 只 | 红块=因子正向贡献 绿块=负向 (A股口径)")
    html = HTML.format(title=strategy["label"], date=d.strftime("%Y-%m-%d"), cdn=CDN, meta=meta,
                       prev_date=dp.strftime("%Y-%m-%d") if dp is not None else "—",
                       hold_table=hold_table, border_table=border_table, migrate=migrate,
                       charts=json.dumps(charts, ensure_ascii=False, default=str))
    out = os.path.join(REPO, "outputs",
                       f"decision_snapshot_{args.strategy.replace('@', '_').replace(',', '-')}_{d.strftime('%Y%m%d')}.html")
    with open(out, "w") as f:
        f.write(html)
    print(f"✅ 报告已生成: {out}")


if __name__ == "__main__":
    main()

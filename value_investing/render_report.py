#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
价值投资日报 · HTML 渲染
─────────────────────────────────────────────────────────
把 step3 的扫描结果渲染成一份独立 HTML（无外部依赖、可离线打开）：
  · 顶部：交易日 / 基准年报 / 当前参数快照
  · 概览：四档数量与分布条
  · 焦点：深度折价且趋势向上的标的
  · 四档明细表：市值、两个合理估值、折价率、ROE/负债率、增速、趋势、走势迷你图

配色遵循 A 股习惯：红涨绿跌。
"""

from __future__ import annotations

import html
from pathlib import Path

import numpy as np
import pandas as pd

import valuelib as vl

UP = "#d9333f"    # 涨 —— 红
DOWN = "#0f9d58"  # 跌 —— 绿
MUTED = "#7b8494"
INK = "#1c2024"
LINE = "#e6e9ee"


def _pct(x, digits=1, signed=True) -> str:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return '<span style="color:%s">—</span>' % MUTED
    c = UP if x > 0 else (DOWN if x < 0 else MUTED)
    s = f"{x*100:+.{digits}f}%" if signed else f"{x*100:.{digits}f}%"
    return f'<span style="color:{c};font-variant-numeric:tabular-nums">{s}</span>'


def _num(x, digits=2, suffix="") -> str:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return f'<span style="color:{MUTED}">—</span>'
    return f'<span style="font-variant-numeric:tabular-nums">{x:,.{digits}f}{suffix}</span>'


def _trend_badge(t: str) -> str:
    if not isinstance(t, str):
        return f'<span style="color:{MUTED}">—</span>'
    if "上升" in t:
        bg, fg = "#fdecec", UP
    elif "下降" in t:
        bg, fg = "#e9f7f0", DOWN
    else:
        bg, fg = "#f2f4f7", "#5b6472"
    return (f'<span style="background:{bg};color:{fg};padding:2px 8px;border-radius:10px;'
            f'font-size:12px;white-space:nowrap">{html.escape(t)}</span>')


def build_html(df: pd.DataFrame, meta: dict, cfg: dict) -> str:
    td = meta["trade_date"]
    base_year = meta["base_year"]
    sc, val, mon = cfg["screen"], cfg["valuation"], cfg["monitor"]
    summary = vl.tier_summary(df)
    total = len(df)

    def card(row) -> str:
        t = row["档位"]
        color = next((x.get("color", MUTED) for x in mon["tiers"] if x["label"] == t), MUTED)
        up = row["上升趋势占比"]
        return f"""
        <div class="card" style="border-top:3px solid {color}">
          <div class="c-label" style="color:{color}">{html.escape(t)}</div>
          <div class="c-num">{int(row['数量'])}<span class="c-unit">只</span></div>
          <div class="c-sub">占比 {vl.fmt_pct(row['占比'],1)} · 上升趋势 {vl.fmt_pct(up,0)}</div>
          <div class="c-sub">平均折价 {vl.fmt_pct(row['平均折价'],1)}</div>
        </div>"""

    cards = "".join(card(r) for _, r in summary.iterrows()
                    if r["档位"] in [t["label"] for t in mon["tiers"]])

    # 分布条
    segs = []
    for _, r in summary.iterrows():
        if r["档位"] not in [t["label"] for t in mon["tiers"]] or not r["数量"]:
            continue
        color = next((x.get("color", MUTED) for x in mon["tiers"] if x["label"] == r["档位"]), MUTED)
        w = r["占比"] * 100
        segs.append(f'<div class="seg" style="width:{w:.2f}%;background:{color}" '
                    f'title="{html.escape(r["档位"])} {int(r["数量"])}只 {w:.1f}%">'
                    f'{html.escape(r["档位"])} {int(r["数量"])}</div>')
    dist_bar = f'<div class="distbar">{"".join(segs)}</div>'

    # 焦点：折价大 + 趋势向上
    focus = df[(df["趋势"].str.contains("上升", na=False)) & (df["市值比_三年后"] <= 0.7)] \
        .sort_values("市值比_三年后").head(12)
    if focus.empty:
        focus_html = '<p class="muted">本期没有「折价 > 30% 且趋势向上」的标的。</p>'
    else:
        rows = "".join(f"""<tr>
          <td class="mono">{html.escape(str(r['代码']))}</td>
          <td class="nm">{html.escape(str(r['名称']))}</td>
          <td class="muted">{html.escape(str(r['行业']))}</td>
          <td>{_num(r['当前市值'],1)}</td>
          <td>{_num(r['三年后合理估值'],1)}</td>
          <td>{_pct(r['折价率_三年后'])}</td>
          <td>{_pct(r['上行空间'])}</td>
          <td>{_trend_badge(r['趋势'])}</td>
          <td>{vl.sparkline(r['_curve'].tail(60) if isinstance(r['_curve'], pd.Series) else [], color=UP)}</td>
        </tr>""" for _, r in focus.iterrows())
        focus_html = f"""<table class="tbl">
          <thead><tr><th>代码</th><th>名称</th><th>行业</th><th>市值(亿)</th>
          <th>三年后合理估值(亿)</th><th>折价率</th><th>上行空间</th><th>趋势</th><th>60日走势</th></tr></thead>
          <tbody>{rows}</tbody></table>"""

    # 分档明细
    sections = []
    for tcfg in mon["tiers"]:
        label = tcfg["label"]
        sub = df[df["档位"] == label]
        color = tcfg.get("color", MUTED)
        if sub.empty:
            sections.append(f'<section><h3 style="border-left:4px solid {color}">'
                            f'{html.escape(label)} <span class="muted">· 0 只</span></h3>'
                            f'<p class="muted">无标的。</p></section>')
            continue

        trs = []
        for _, r in sub.iterrows():
            line_color = UP if (r["近60日"] or 0) > 0 else DOWN
            trs.append(f"""<tr>
              <td class="mono">{html.escape(str(r['代码']))}</td>
              <td class="nm">{html.escape(str(r['名称']))}</td>
              <td class="muted ind">{html.escape(str(r['行业']))}</td>
              <td>{_num(r['close'],2)}</td>
              <td>{_num(r['当前市值'],1)}</td>
              <td>{_num(r['去年年报合理估值'],1)}</td>
              <td>{_num(r['三年后合理估值'],1)}</td>
              <td>{_num(r['市值比_三年后'],2)}</td>
              <td><b>{_pct(r['折价率_三年后'])}</b></td>
              <td>{_num(r['ROE'],1,'%')}</td>
              <td>{_num(r['资产负债率'],1,'%')}</td>
              <td>{_num(r['年化增速']*100 if pd.notna(r['年化增速']) else np.nan,1,'%')}</td>
              <td>{_trend_badge(r['趋势'])}</td>
              <td>{_pct(r['近5日'])}</td>
              <td>{_pct(r['近20日'])}</td>
              <td>{_pct(r['近60日'])}</td>
              <td>{vl.sparkline(r['_curve'].tail(60) if isinstance(r['_curve'], pd.Series) else [], color=line_color)}</td>
            </tr>""")

        sections.append(f"""
        <section>
          <h3 style="border-left:4px solid {color}">{html.escape(label)}
            <span class="muted">· {len(sub)} 只 · 平均折价 {vl.fmt_pct(sub['折价率_三年后'].mean(),1)}</span></h3>
          <div class="scroll">
          <table class="tbl">
            <thead><tr>
              <th>代码</th><th>名称</th><th>行业</th><th>现价</th><th>市值(亿)</th>
              <th>去年年报合理估值(亿)</th><th>三年后合理估值(亿)</th><th>市值/三年后估值</th>
              <th>折价率(三年后)</th><th>ROE</th><th>负债率</th><th>年化增速</th>
              <th>趋势</th><th>近5日</th><th>近20日</th><th>近60日</th><th>60日走势</th>
            </tr></thead>
            <tbody>{"".join(trs)}</tbody>
          </table></div>
        </section>""")

    chip = lambda k, v: f'<span class="chip"><b>{k}</b>{v}</span>'
    chips = "".join([
        chip("基准年报", f"{base_year} 年报"),
        chip("选股 ROE", f"≥ {sc['roe_min']}%"),
        chip("选股负债率", f"≤ {sc['debt_to_assets_max']}%"),
        chip("合理 PE", f"{val['pe_multiple']}×"),
        chip("外推", f"{val['projection_years']} 年"),
        chip("增速口径", f"{val['growth']['prefer_points']}点→{val['growth']['fallback_points']}点→默认{val['growth']['default_rate']:.0%}"),
        chip("增速保护", f"[{val['growth']['min_rate']:.0%}, {val['growth']['max_rate']:.0%}]"),
        chip("股票池", " ".join(sc["markets"])),
        chip("分档依据", "市值 / 三年后合理估值"),
    ])

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>价值投资日报 {td} · 财报低估扫描</title>
<style>
  :root {{ --ink:{INK}; --muted:{MUTED}; --line:{LINE}; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:#f6f7f9; color:var(--ink);
        font-family:-apple-system,"PingFang SC","Helvetica Neue",Arial,sans-serif;
        font-size:13px; line-height:1.55; -webkit-font-smoothing:antialiased; }}
  .wrap {{ max-width:1560px; margin:0 auto; padding:28px 24px 60px; }}
  header h1 {{ margin:0 0 6px; font-size:24px; letter-spacing:.3px; }}
  header .sub {{ color:var(--muted); font-size:13px; margin-bottom:16px; }}
  .chips {{ display:flex; flex-wrap:wrap; gap:8px; margin-bottom:22px; }}
  .chip {{ background:#fff; border:1px solid var(--line); border-radius:8px;
           padding:5px 10px; font-size:12px; color:var(--muted); }}
  .chip b {{ color:var(--ink); font-weight:600; margin-right:6px; }}
  .cards {{ display:grid; grid-template-columns:repeat(4,1fr); gap:14px; margin-bottom:18px; }}
  .card {{ background:#fff; border:1px solid var(--line); border-radius:12px; padding:14px 16px; }}
  .c-label {{ font-size:13px; font-weight:700; margin-bottom:6px; }}
  .c-num {{ font-size:28px; font-weight:700; font-variant-numeric:tabular-nums; }}
  .c-unit {{ font-size:13px; font-weight:400; color:var(--muted); margin-left:4px; }}
  .c-sub {{ font-size:12px; color:var(--muted); margin-top:2px; }}
  .distbar {{ display:flex; height:30px; border-radius:8px; overflow:hidden;
              margin-bottom:26px; background:#fff; border:1px solid var(--line); }}
  .seg {{ color:#fff; font-size:12px; display:flex; align-items:center; justify-content:center;
          white-space:nowrap; overflow:hidden; }}
  section {{ background:#fff; border:1px solid var(--line); border-radius:12px;
             padding:16px 18px; margin-bottom:18px; }}
  section h3 {{ margin:0 0 12px; font-size:15px; padding-left:10px; }}
  h3 .muted, .muted {{ color:var(--muted); font-weight:400; font-size:12px; }}
  .scroll {{ overflow-x:auto; }}
  table.tbl {{ border-collapse:collapse; width:100%; min-width:1180px; }}
  .tbl th {{ text-align:left; font-size:11.5px; color:var(--muted); font-weight:600;
             border-bottom:1px solid var(--line); padding:7px 8px; white-space:nowrap; }}
  .tbl td {{ padding:7px 8px; border-bottom:1px solid #f2f4f7; white-space:nowrap; }}
  .tbl tbody tr:hover {{ background:#fafbfc; }}
  .mono {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; }}
  .nm {{ font-weight:600; }}
  .ind {{ max-width:110px; overflow:hidden; text-overflow:ellipsis; }}
  footer {{ color:var(--muted); font-size:12px; line-height:1.9; padding:0 4px; }}
  footer code {{ background:#eceef1; padding:1px 5px; border-radius:4px; font-size:11.5px; }}
</style></head>
<body><div class="wrap">
<header>
  <h1>价值投资日报 · 财报低估扫描</h1>
  <div class="sub">交易日 {td} &nbsp;·&nbsp; 基准年报 {base_year} 年 &nbsp;·&nbsp;
    合格 list {total} 只（估值可用）&nbsp;·&nbsp; 生成于 {pd.Timestamp.now():%Y-%m-%d %H:%M}</div>
</header>
<div class="chips">{chips}</div>
<div class="cards">{cards}</div>
{dist_bar}

<section>
  <h3>焦点 · 折价 &gt; 30% 且趋势向上</h3>
  {focus_html}
</section>

{''.join(sections)}

<footer>
  <b>口径说明</b><br>
  · 选股：<code>financial_indicator</code> 年报（report_type='4'），ROE ≥ {sc['roe_min']}%、资产负债率 ≤ {sc['debt_to_assets_max']}%，
    {('剔除 ST/退市') if sc.get('exclude_st') else ''}，股票池 {" ".join(sc['markets'])}。<br>
  · 合理估值 = 归母净利润 × {val['pe_multiple']} 倍 PE。去年年报口径用 FY{base_year} 利润；三年后口径用 FY{base_year-4}~FY{base_year} 的利润序列拟合年化增速外推 {val['projection_years']} 年。<br>
  · 增速拟合优先 {val['growth']['prefer_points']} 个年报点（CAGR），不足退到 {val['growth']['fallback_points']} 个点，仍不足用默认 {val['growth']['default_rate']:.0%}；最后做 [{val['growth']['min_rate']:.0%}, {val['growth']['max_rate']:.0%}] 上下限保护。<br>
  · 分档：<code>mv_ratio = 当前总市值 / 三年后合理估值</code>；mv_ratio ≤ 0.5 非常低估、0.5~0.7 低估、0.7~1.0 一般低估、&gt;1.0 高估。<br>
  · 趋势：由收盘价 MA20/MA60 位置、近 20 日涨跌、MA20 斜率四条件打分（4/3=上升，2=震荡，1/0=下降）。<br>
  · 本报告为纯财报静态估算，PE 倍数与增速均为主观假设，<b>不构成投资建议</b>。
</footer>
</div></body></html>"""


def render(df: pd.DataFrame, meta: dict, cfg: dict, path: str | Path | None = None) -> Path:
    out = Path(path) if path else vl.REPORT_DIR / f"value_report_{meta['trade_date']:%Y%m%d}.html"
    out.write_text(build_html(df, meta, cfg), encoding="utf-8")
    return out


if __name__ == "__main__":
    import step3_daily_monitor as s3
    cfg = vl.load_config()
    df, meta = s3.scan(cfg)
    print(render(df, meta, cfg))

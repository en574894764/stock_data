#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成《十倍股早期识别方法论手册》HTML 报告"""
import html
import json
import pandas as pd

OUT = "outputs/十倍股早期识别方法论手册.html"

bt = json.load(open("outputs/tenbagger_screen_backtest.json"))
gap = json.load(open("outputs/netprofit_gap_test.json"))
cand = pd.read_csv("outputs/tenbagger_candidates_latest.csv")
recent_gap = pd.read_csv("outputs/netprofit_gap_recent.csv")

# ---------- 表格 ----------
def cand_rows(df, n=63):
    rows = []
    for i, r in enumerate(df.head(n).itertuples(), 1):
        pe = f"{r.pe_ttm:.0f}" if pd.notna(r.pe_ttm) else "—"
        pb = f"{r.pb:.1f}" if pd.notna(r.pb) else "—"
        ret12 = f"{r.ret12m*100:.0f}%" if pd.notna(r.ret12m) else "—"
        npy = f"{r.netprofit_yoy:.0f}%" if pd.notna(r.netprofit_yoy) else "—"
        rows.append(
            f"<tr><td>{i}</td><td class='l'><b>{html.escape(r.name)}</b></td>"
            f"<td class='l'>{html.escape(str(r.industry))}</td>"
            f"<td class='num'>{r.mv_yi:.0f}</td><td class='num'>{pe}</td>"
            f"<td class='num'>{r.roe:.1f}%</td><td class='num'>{r.or_yoy:.0f}%</td>"
            f"<td class='num'>{npy}</td><td class='num'>{r.grossprofit_margin:.0f}%</td>"
            f"<td class='num'>{ret12}</td></tr>")
    return "\n".join(rows)


def gap_rows(df, n=40):
    rows = []
    for i, r in enumerate(df.head(n).itertuples(), 1):
        mv = f"{r.mv_yi:.0f}" if pd.notna(r.mv_yi) else "—"
        pe = f"{r.pe_ttm:.0f}" if pd.notna(r.pe_ttm) else "—"
        ory = f"{r.or_yoy:.0f}%" if pd.notna(r.or_yoy) else "—"
        rt = {1: "Q1", 2: "中报", 3: "Q3", 4: "年报"}.get(int(r.report_type), "?")
        rows.append(
            f"<tr><td>{i}</td><td class='l'><b>{html.escape(r.name)}</b></td>"
            f"<td class='l'>{html.escape(str(r.industry))}</td><td>{r.sig_date}</td>"
            f"<td>{rt}</td><td class='num'>{r.npy:.0f}%</td><td class='num'>{ory}</td>"
            f"<td class='num'>{mv}</td><td class='num'>{pe}</td></tr>")
    return "\n".join(rows)


# 回测结果表（5 组网格）
bt_rows = []
for r in bt["results"]:
    p = r["params"]
    desc = f"增速≥{p['g_min']}%/ROE≥{p['r_min']}%/毛利≥{p['m_min']}%/市值≤{p['mv_max']}亿"
    if p["use_ind"]:
        desc += " +行业景气"
    if p["use_mom"]:
        desc += " +动量"
    hits = "、".join(r["hit_names"]) if r["hit_names"] else "—"
    bt_rows.append(
        f"<tr><td class='l'>{desc}</td><td class='num'>{r['n']}</td>"
        f"<td class='num'>{r['hit']}/{r['hit_total']}</td>"
        f"<td class='num'>{r['precision']}%</td><td class='num'>{r['lift']}x</td>"
        f"<td class='num'>{r['port_mult']}x</td><td class='l' style='font-size:12px'>{hits}</td></tr>")
bt_rows = "\n".join(bt_rows)

# 断层统计表
gs = {s["name"]: s for s in gap["stats"]}
gap_stat_rows = f"""
<tr><td class='l'>全部信号</td><td class='num'>{gs['全部信号']['n']}</td><td class='num'>{gs['全部信号']['mean_1y']}%</td><td class='num'>{gs['全部信号']['median_1y']}%</td><td class='num'>{gs['全部信号']['win_rate']}%</td><td class='num'>{gs['全部信号']['median_excess']}%</td></tr>
<tr><td class='l'>连续断层（12个月内≥2次）</td><td class='num'>{gs['连续断层(12个月内>=2次)']['n']}</td><td class='num'>{gs['连续断层(12个月内>=2次)']['mean_1y']}%</td><td class='num'>{gs['连续断层(12个月内>=2次)']['median_1y']}%</td><td class='num'>{gs['连续断层(12个月内>=2次)']['win_rate']}%</td><td class='num'>{gs['连续断层(12个月内>=2次)']['median_excess']}%</td></tr>
<tr><td class='l'>首次断层</td><td class='num'>{gs['首次断层']['n']}</td><td class='num'>{gs['首次断层']['mean_1y']}%</td><td class='num'>{gs['首次断层']['median_1y']}%</td><td class='num'>{gs['首次断层']['win_rate']}%</td><td class='num'>{gs['首次断层']['median_excess']}%</td></tr>"""

# 候选行业分布图数据
ind_vc = cand["industry"].value_counts().head(10)
ind_names = json.dumps(ind_vc.index.tolist(), ensure_ascii=False)
ind_vals = json.dumps(ind_vc.values.tolist())

# 断层信号 1 年收益分布（直方图）
sig_all = pd.DataFrame(gap["signals"])
rets = sig_all["ret_1y"].dropna() * 100
bins = list(range(-100, 301, 25))
counts = [int(((rets >= b) & (rets < b + 25)).sum()) for b in bins]
bin_labels = json.dumps([f"{b}~{b+25}" for b in bins])
bin_counts = json.dumps(counts)

CSS = """
:root{--bg:#f7f8fa;--card:#fff;--ink:#1a2332;--mut:#5c6675;--line:#e3e7ee;
--red:#c0392b;--green:#1a7a4a;--blue:#2456a6;--amber:#b9770e;--soft:#eef2f8}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--ink);font:15px/1.8 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:36px 24px 80px}
header.hero{background:linear-gradient(135deg,#16213e,#2456a6);color:#fff;border-radius:14px;padding:38px 40px;margin-bottom:26px}
.hero h1{font-size:27px;margin-bottom:10px}.hero .sub{opacity:.85;font-size:14.5px;max-width:820px}
.meta{margin-top:16px;font-size:12.5px;opacity:.7}
h2{font-size:21px;margin:38px 0 14px;padding-left:12px;border-left:4px solid var(--blue)}
h3{font-size:16.5px;margin:24px 0 10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:22px 24px;margin:14px 0}
table{border-collapse:collapse;width:100%;font-size:13px;margin:6px 0}
th{background:var(--soft);padding:8px 9px;text-align:center;font-weight:600;white-space:nowrap}
td{padding:7px 9px;border-bottom:1px solid var(--line);text-align:center}
td.l{text-align:left}td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
tr:hover td{background:#f4f7fc}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:16px 0}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.kpi .v{font-size:23px;font-weight:700}.kpi .l{font-size:12.5px;color:var(--mut);margin-top:2px}
.kpi .d{font-size:12px;color:var(--mut)}
.red{color:var(--red)}.green{color:var(--green)}.blue{color:var(--blue)}.amber{color:var(--amber)}
.callout{border-left:4px solid var(--amber);background:#fdf6e9;padding:12px 16px;border-radius:0 10px 10px 0;margin:12px 0;font-size:14px}
.callout.blue{border-color:var(--blue);background:#eef4fc}
.callout.red{border-color:var(--red);background:#fcedec}
.callout.green{border-color:var(--green);background:#ecf7f1}
.chart{width:100%;height:330px;margin:6px 0 2px}
.scroll{max-height:520px;overflow-y:auto;border:1px solid var(--line);border-radius:10px}
.tag{display:inline-block;background:var(--soft);color:var(--blue);border-radius:5px;padding:1px 8px;font-size:12px;margin:1px 3px 1px 0}
.step{display:flex;gap:14px;margin:12px 0}
.step .no{flex:0 0 34px;height:34px;border-radius:50%;background:var(--blue);color:#fff;display:flex;align-items:center;justify-content:center;font-weight:700}
.step .bd{flex:1}.step .bd b{font-size:15px}
code{background:#eef1f6;padding:1px 6px;border-radius:5px;font-size:12.5px;font-family:Menlo,monospace}
ol,ul{padding-left:22px}li{margin:5px 0}
.disc{font-size:12px;color:var(--mut);margin-top:40px;border-top:1px solid var(--line);padding-top:14px;line-height:1.7}
.chk li{list-style:none;position:relative;padding-left:26px}
.chk li:before{content:"☐";position:absolute;left:0;color:var(--blue);font-size:15px}
"""

HTML_DOC = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>十倍股早期识别方法论手册</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
<style>{CSS}</style></head>
<body><div class="wrap">

<header class="hero">
  <h1>十倍股早期识别方法论手册</h1>
  <div class="sub">基于本地量化库三窗口十倍股解剖（5/10/20 年共 206 只终值十倍股）、筛选漏斗 2016 时点回测、
  2,437 个净利润断层信号实测的可落地操作体系。核心立场：十倍股无法被任何静态规则"预测"，
  但可以被一套<b>动态筛选 + 人工深研 + 赔率组合</b>的体系"接住"。</div>
  <div class="meta">数据窗口：2016-09 ~ 2026-09 · 候选列表 as of 2026-09-11 · 生成于 2026-09-12 · 仅供研究参考</div>
</header>

<div class="kpis">
  <div class="kpi"><div class="v red">0.76%</div><div class="l">十年十倍股命中率</div><div class="d">20 / 2,637（2016→2026）</div></div>
  <div class="kpi"><div class="v blue">2.7%</div><div class="l">静态漏斗最优精度</div><div class="d">2016 回测，4.7 倍于随机</div></div>
  <div class="kpi"><div class="v blue">1.74x</div><div class="l">漏斗组合 10 年回报</div><div class="d">vs 沪深300 1.38x（等权持有）</div></div>
  <div class="kpi"><div class="v amber">16.5%</div><div class="l">断层信号 1 年均值</div><div class="d">2,437 个信号，中位仅 3.0%</div></div>
  <div class="kpi"><div class="v red">63 只</div><div class="l">当下静态候选池</div><div class="d">as of 2026-09-11</div></div>
  <div class="kpi"><div class="v red">108 条</div><div class="l">当下断层观察池</div><div class="d">2026-04 以来中报季信号</div></div>
</div>

<h2>一、先建立正确预期：这套方法能做什么、不能做什么</h2>
<div class="card">
<div class="callout red"><b>不能做的：</b>在 2016 年的时点，用当时一切可见的财务与行情数据，最优静态规则也只能把十倍股命中率
从 0.57% 提升到 2.7%（4.7 倍）。净利润断层这类动态信号，20 只十年十倍股里也只提前捕捉到 6 只（30%）。
<b>任何宣称能稳定"选出"十倍股的方法都是过度承诺。</b>十倍股的核心变量——产业趋势拐点——不在历史报表里。</div>
<div class="callout green"><b>能做的：</b>①把 5,000 只股票压缩到 <b>60~110 只的研究范围</b>，让有限的人工研究精力花在命中率
4-5 倍于随机的池子里；②静态候选池即使不命中十倍股，<b>等权持有 10 年 1.74x 也跑赢沪深300 的 1.38x</b>——
池子本身有正期望；③用净利润断层信号做<b>动态确认</b>（信号后 1 年均值 +16.5%，右尾极厚），
在产业趋势起来时保证你在场；④用明确的卖出纪律规避 2/3 的"过山车"命运。</div>
<p style="margin-top:8px">一句话：<b>十倍股是"研究范围管理 + 赔率 + 耐心"的游戏，不是"预测"的游戏。</b>本手册的目标是把你的研究范围
变成全市场质量最高的一小块，并把"接住"的流程标准化。</p>
</div>

<h2>二、实证基础：三个关键实验</h2>

<h3>2.1 实验一：静态筛选漏斗 · 2016 时点回测（无前视偏差）</h3>
<div class="card">
<p>在 2016-09-12 截面，仅用当日可见数据（2016 中报 + 当日行情估值），对 2,108 只基础股票池逐层过滤，
观察 2016→2026 十年十倍股（12 只，剔除次新/ST 后的可比口径）的命中情况：</p>
<table>
<tr><th class='l'>漏斗参数</th><th>组合</th><th>命中</th><th>精度</th><th>vs随机</th><th>组合10年</th><th>命中明细</th></tr>
{bt_rows}
</table>
<div class="callout" style="margin-top:10px"><b>三个发现：</b>
①静态漏斗整体跑赢市场（1.50~1.79x vs 1.38x），是合格的"正期望研究池"；
②<b>行业景气层有效</b>（精度 1.25%→2.7%，组合 80→37 只）；
③<b>动量层对十年持有是负贡献</b>——它筛掉了牧原这类"尚未启动"的未来十倍股。
动量（追涨确认）属于<b>加仓信号</b>而不是<b>入池条件</b>，这是两层不同的东西，混用会两头落空。</div>
</div>

<h3>2.2 实验二：净利润断层信号 · 2,437 个信号全样本实测</h3>
<div class="card">
<p>信号定义：财报净利增速 ≥50% + 披露次日向上跳空（最低价 &gt; 前日最高价）+ 放量 ≥1.5 倍 20 日均量 + 收阳。
信号日收盘买入，持有 1 年（2016-09 ~ 2025-09 信号窗口）：</p>
<table>
<tr><th class='l'>信号分层</th><th>样本</th><th>1年均值</th><th>1年中位</th><th>胜率</th><th>超额中位(vs沪深300)</th></tr>
{gap_stat_rows}
</table>
<div id="c_gapdist" class="chart"></div>
<div class="callout"><b>解读：</b>均值 16.5% 与中位数 3.0% 的巨大裂口 = 收益高度幂律化，少数信号贡献绝大部分收益。
这正是"断层信号池 + 分散买入 + 拿住右尾"策略的统计基础。注意：研报宣称的 38% 复合收益是特定窗口的连续三次断层组合，
我们的全样本复现不支持那个数字——<b>断层是"观察池生成器"，不是自动提款机</b>。</div>
<div class="callout blue"><b>对十倍股的早期覆盖：</b>20 只十年十倍股中 6 只在 2016-09~2021-06 发出过断层信号：
沪电股份（2017-03，首次信号后 10 年 15.8x）、牧原股份（2017-04，12.3x）、恒立液压（2017-10，10.4x）、
胜宏科技（2018-08，31.4x）、紫金矿业（2018-08，11.8x）、天孚通信（2021-04，22.9x）。
<b>首次信号日期距离它们成为十倍股，平均还有 5-8 年</b>——这就是"早期"的含义。</div>
</div>

<h3>2.3 实验三：十倍股解剖的跨窗口铁律（此前三轮研究汇总）</h3>
<div class="card">
<table>
<tr><th class='l'>规律</th><th>5年(2021→26)</th><th>10年(2016→26)</th><th>20年(2006→26)</th></tr>
<tr><td class='l'>终值≥10x 数量 / 概率</td><td class='num'>18只 / 0.41%</td><td class='num'>20只 / 0.76%</td><td class='num'>168只 / 12.5%</td></tr>
<tr><td class='l'>净利中位倍数</td><td class='num'>+8.5x</td><td class='num'>+21.5x</td><td class='num'>+22.3x</td></tr>
<tr><td class='l'>起点 PE 分位</td><td class='num'>66（贵）</td><td class='num'>62（贵）</td><td class='num'>44（中间）</td></tr>
<tr><td class='l'>便宜派规则（PE&lt;20&PB&lt;2）精度</td><td class='num red'>0%</td><td class='num red'>0%</td><td class='num red'>8%（负选股）</td></tr>
<tr><td class='l'>曾达10x后过山车比例</td><td class='num'>49%</td><td class='num'>56%</td><td class='num'>67%</td></tr>
<tr><td class='l'>行业集中度</td><td class='l'>AI算力链 ~13/18</td><td class='l'>电子 12/20</td><td class='l'>56 行业分散</td></tr>
</table>
<p style="margin-top:8px"><b>对方法论的直接推论：</b>①盈利驱动是唯一硬通货 → 深研必须回答"利润从哪来、能否持续放大"；
②别在便宜股里找十倍股 → 估值容忍度要给足（PEG 框架）；③产业趋势是最大单一变量 → 趋势定位是体系的第一层；
④过山车率 49-67% → 卖出纪律与选股同等重要。</p>
</div>

<h3>2.4 关键体检：这套规则对十年十倍股的召回率与准确率</h3>
<div class="card">
<p>把现行漏斗（市值≤500亿 / 营收增速≥15% / ROE≥8% / 毛利≥25% / 行业景气前1/3，无动量层）放回 2016-09-12，
让 20 只十年十倍股逐只过堂（无前视偏差）：</p>
<table>
<tr><th class='l'>指标</th><th>数值</th><th class='l'>说明</th></tr>
<tr><td class='l'><b>准确率（精度）</b></td><td class='num red'><b>2.70%</b>（1/37）</td><td class='l'>随机基线 0.60%，<b>lift 4.5 倍</b>——池子质量是合格的</td></tr>
<tr><td class='l'><b>召回率（全口径）</b></td><td class='num red'><b>5%</b>（1/20）</td><td class='l'>唯一全规则通过者：牧原股份</td></tr>
<tr><td class='l'><b>召回率（可比池口径）</b></td><td class='num'>9.1%（1/11）</td><td class='l'>剔除 9 只 L0 层就出局的次新/数据缺失股后</td></tr>
<tr><td class='l'><b>体系级召回（+断层信号并集）</b></td><td class='num amber'><b>30%</b>（6/20）</td><td class='l'>断层补进天孚/恒立/沪电/胜宏/紫金——全是"起点财务平庸"型，恰好补静态漏斗的盲区</td></tr>
</table>

<h3 style="margin-top:18px">召回损失分解：19 只是被谁拦下的</h3>
<table>
<tr><th class='l'>拦截层</th><th>拦截数</th><th class='l'>被拦的十倍股（2016 时点真实财务）</th></tr>
<tr><td class='l'>L0 次新/数据（上市&lt;2年或缺财报）</td><td class='num red'><b>9</b></td>
<td class='l'>中际旭创、新易盛、胜宏科技、兆易创新、天孚通信、长芯博创、香农芯创、泰晶科技、振华股份——<b>45% 的十倍股是 2014-2016 次新股</b></td></tr>
<tr><td class='l'>ROE ≥ 8%</td><td class='num'>9</td>
<td class='l'>沪电(0.9%)、北方华创(1.1%)、恒立液压(0.8%)、东山精密(5.5%)、天华新能(1.4%)、卫星化学(0.5%)…——起点 ROE 平庸是常态</td></tr>
<tr><td class='l'>营收增速 ≥ 15%</td><td class='num'>8</td>
<td class='l'>沪电(12.7%)、山西汾酒(7.9%)、恒立(7.7%)、生益科技(8.6%)、思源电气(11.9%)…</td></tr>
<tr><td class='l'>毛利率 ≥ 25%</td><td class='num'>7</td>
<td class='l'>沪电(14.7%)、恒立(19.9%)、生益(20.0%)、紫金(11.0%)…</td></tr>
<tr><td class='l'>行业景气前 1/3</td><td class='num'>7</td>
<td class='l'>北方华创、恒立、思源、天华新能、紫金、汾酒、卫星化学</td></tr>
<tr><td class='l'>市值 ≤ 500 亿</td><td class='num'>1</td>
<td class='l'>紫金矿业（694 亿）——市值门槛几乎无害</td></tr>
</table>

<h3 style="margin-top:18px">参数敏感性：召回与精度的权衡（2016 回测）</h3>
<table>
<tr><th class='l'>参数组合</th><th>池子</th><th>召回</th><th>精度</th><th>lift</th><th class='l'>命中</th></tr>
<tr><td class='l'>现行（增速15/ROE8/毛利25）</td><td class='num'>37</td><td class='num'>5%</td><td class='num'><b>2.70%</b></td><td class='num'>4.9x</td><td class='l'>牧原</td></tr>
<tr><td class='l'>宽松（增速10/ROE5/毛利20）</td><td class='num'>108</td><td class='num'>5%</td><td class='num'>0.93%</td><td class='num'>1.7x</td><td class='l'>牧原</td></tr>
<tr><td class='l'>成长优先（增速25/无ROE/毛利20）</td><td class='num'>184</td><td class='num'>5%</td><td class='num'>0.54%</td><td class='num'>1.0x</td><td class='l'>牧原</td></tr>
<tr><td class='l'>现行 + 次新通道（上市≥3个月）</td><td class='num'>40</td><td class='num'>5%</td><td class='num'>2.50%</td><td class='num'>3.8x</td><td class='l'>天孚通信</td></tr>
<tr><td class='l'><b>宽松 + 次新通道</b></td><td class='num'>125</td><td class='num red'><b>15%</b></td><td class='num'>2.40%</td><td class='num'><b>3.6x</b></td><td class='l'>新易盛、天孚通信、胜宏科技（全是 AI 硬件链）</td></tr>
<tr><td class='l'>成长优先 + 次新</td><td class='num'>213</td><td class='num'>10%</td><td class='num'>0.94%</td><td class='num'>1.4x</td><td class='l'>天孚、胜宏</td></tr>
</table>

<div class="callout red" style="margin-top:12px"><b>四个硬结论：</b>
①<b>召回的最大杀手是"上市满2年"（-45%）</b>，其次是 ROE/增速/毛利质量门槛——十倍股起点的财务报表大多平庸甚至难看，
现行参数本质上是"高精度、低召回"档：它保证池子干净，代价是 20 只里只接得住 1 只。<br>
②<b>放宽质量门槛不划算</b>：池子膨胀 3-5 倍，lift 从 4.9x 掉到 1.0-1.7x，召回却几乎不涨——质量门槛拦掉的是真噪音。<br>
③<b>唯一有效的召回杠杆是次新通道</b>：宽松参数+次新股池，召回 5%→15% 且 lift 仍有 3.6 倍，命中的新易盛/天孚/胜宏全是后来 AI 链主力。<br>
④<b>断层信号是不可替代的召回补充</b>（+5 只净增量，且全是静态规则天生接不住的类型）——这就是体系里"动态层"存在的理由。
即便体系级召回 30%，也意味着 10 只里注定漏 7 只：<b>所以必须靠 15-30 只的组合和赔率取胜，任何单票押注都与这套方法的统计现实矛盾。</b></div>
</div>

<h2>三、方法论体系：五层操作框架</h2>
<div class="card">

<div class="step"><div class="no">1</div><div class="bd"><b>产业趋势定位（人工，每半年更新）</b>
<div>目标：锁定 2~4 个处于<b>渗透率 5%→30% 黄金期</b>的产业方向。判断依据（广发策略框架的三大映射信号）：
<span class="tag">政策映射：五年规划/会议首次提及+量化目标</span>
<span class="tag">海外映射：美股龙头动向/巨头 Capex/现象级产品</span>
<span class="tag">一级市场映射：PE/VC 融资与 IPO 方向</span><br>
渗透率信息来源：行业协会数据、券商行业深度报告、龙头公司财报中的销量/行业总量。
<b>纪律：渗透率 &lt;5% 只观察不重仓（主题期），&gt;30% 停止加仓（内卷期）。</b>
当前（2026-09）处于黄金期的方向示例：AI 算力硬件（光模块/PCB/液冷）、具身智能、创新药出海、半导体设备国产化。</div></div></div>

<div class="step"><div class="no">2</div><div class="bd"><b>量化候选池（脚本，每季财报后重跑）</b>
<div>静态漏斗（已在 2016 回测验证为正期望池）：<code>scripts/tenbagger_screen.py</code><br>
条件：非 ST / 上市满 2 年 · 市值 ≤500 亿 · 最近中报营收增速 ≥15% · ROE ≥8% · 毛利率 ≥25% · 行业景气前 1/3。
当下（2026-09-11）输出 <b>63 只</b>，见 §五。用途：这是你的<b>研究范围</b>，不是买入清单。</div></div></div>

<div class="step"><div class="no">3</div><div class="bd"><b>动态信号确认（脚本，每季财报季扫一遍）</b>
<div>净利润断层观察池：<code>scripts/netprofit_gap_test.py</code>（信号：净利增速≥50% + 跳空 + 放量≥1.5× + 收阳）。
当下 2026 中报季输出 <b>108 条</b>，见 §六。用途：趋势启动的确认器——同一产业方向内多家公司同时出现断层，
就是行业景气的铁证（如 2026 中报季光模块/PCB 公司批量断层）。<b>断层股优先进入深研队列。</b></div></div></div>

<div class="step"><div class="no">4</div><div class="bd"><b>人工深研（你自己做，每只 2-4 小时）</b>
<div>对候选池 ∩ 断层池 ∩ 趋势方向的交集股票，按 §四 的 checklist 逐项查资料打分。
深研的本质是回答一个问题：<b>"这家公司未来 5-10 年的净利润能否放大 10-20 倍？"</b>
（十年十倍股的净利中位放大 21.5 倍——股价只是利润的镜像。）</div></div></div>

<div class="step"><div class="no">5</div><div class="bd"><b>组合构建与纪律（执行层）</b>
<div>①初建 15~30 只组合，单票 3~7%，趋势方向内集中（趋势外不配）；<br>
②<b>确认加仓</b>：持仓涨 50%+ 且逻辑未证伪时再评估加仓（东吴实证：涨 50% 后成为十倍股的概率 2%→22%）——
宁可少赚前 50%，换取胜率 10 倍提升；<br>
③<b>拿住吃幂律</b>：十年十倍股 13/20 在 2024 年后才兑现，剔除最高 3 周收益减半——不在场 = 前功尽弃；<br>
④<b>卖出三条件</b>（任一触发）：产业逻辑证伪（渗透率见顶/技术路线被颠覆/龙头地位丢失）·
连续两期业绩证伪（增速大幅低于预期且无合理解释）· 单票泡沫化（牛市后段曾达 5x+ 的持仓分批止盈 1/3~1/2，防过山车——三窗口过山车率 49~67%）。</div></div></div>
</div>

<h2>四、人工深研 Checklist（每只候选股逐项核查）</h2>
<div class="card">
<h3>A. 产业与空间（回答：池子够不够大）</h3>
<ul class="chk">
<li>所处产业的当前渗透率多少？5-30% 区间吗？（查券商行业深度/协会数据）</li>
<li>行业空间（TAM）能否支撑公司收入再放大 5-10 倍？</li>
<li>驱动因素是什么：技术奇点 / 政策量化目标 / 人口结构？能否说出具体的"海外映射"事件？</li>
</ul>
<h3>B. 竞争格局（回答：为什么是这家）</h3>
<ul class="chk">
<li>行业 CR3/CR5 多少？公司排第几？份额在升还是降？</li>
<li>客户结构：是否进入头部客户供应链（如英伟达链/特斯拉链/华为链）？</li>
<li>有无"卡脖子"环节的国产替代属性？国内营收占比在快速提升吗？</li>
</ul>
<h3>C. 商业模式与财务质量（回答：利润是不是真的、能否放大）</h3>
<ul class="chk">
<li>毛利率 ≥25% 且稳定/上行？净利率趋势？（毛利率是定价权的直接证据）</li>
<li>净利润增长是主业驱动吗？（剔除卖资产/补贴/并表——看扣非增速）</li>
<li>经营现金流 / 净利润 ≥ 0.8？应收和存货有没有异常膨胀？</li>
<li>ROE ≥8%？（十年十倍股起点 ROE 分位 76，但不苛求 15%+）</li>
</ul>
<h3>D. 成长引擎（回答：增速能否持续）</h3>
<ul class="chk">
<li>在手订单 / 合同负债是否高增长？（断层股重点看：这次超预期是一次性还是趋势起点）</li>
<li>产能扩张计划？Capex 在投什么？</li>
<li>连续两期以上增速上行（二阶导为正）吗？</li>
</ul>
<h3>E. 管理层与治理（回答：能不能信任）</h3>
<ul class="chk">
<li>实控人持股比例与质押率？有无大额减持/商誉暴雷/财务造假前科？</li>
<li>历史上说过的话兑现了吗？（翻 3 年前年报"经营计划"对照实际）</li>
</ul>
<h3>F. 估值容忍度（回答：现在买贵不贵——注意不是"便不便宜"）</h3>
<ul class="chk">
<li>十倍股起点 PE 中位 62-81 倍：贵不是问题，<b>增速配不上估值才是问题</b>。算 PEG = PE/未来 3 年预期复合增速，&lt;1.5 可接受，&lt;1 优秀</li>
<li>对比自身历史 PE 分位：&lt;30% 分位的断层股是最优买点（广发金工验证：断层+低估值分位复合 27.6%）</li>
</ul>
<div class="callout amber" style="margin-top:12px"><b>真假断层辨别（避坑）：</b>
✅ 真断层 = 扣非主业驱动 + 超券商一致预期 + 放量 + 缺口 3-10 日不回补 + 位置不高（公告前 30 日涨幅不大）+ 板块景气共振。<br>
❌ 假断层 = 卖资产扭亏 / 缩量跳空 / 快速回补缺口 / 股价已提前翻倍 / 增速高但低于一致预期（利好兑现）。</div>
</div>

<h2>五、当下静态候选池（63 只 · as of 2026-09-11）</h2>
<div class="card">
<p>条件：非 ST / 上市满 2 年 / 市值 ≤500 亿 / 2026 中报营收增速 ≥15% / ROE ≥8% / 毛利率 ≥25% / 行业景气前 1/3。
行业分布高度集中在当前的产业趋势方向（专用机械+半导体+元器件+电气设备 = AI 硬件与机器人链）：</p>
<div id="c_ind" class="chart"></div>
<p style="font-size:13px;color:var(--mut)">完整 CSV：<code>outputs/tenbagger_candidates_latest.csv</code>（按营收增速降序）。<b>这是研究范围，不是买入建议。</b></p>
<div class="scroll"><table>
<tr><th>#</th><th class='l'>名称</th><th class='l'>行业</th><th>市值(亿)</th><th>PE</th><th>ROE</th><th>营收增速</th><th>净利增速</th><th>毛利率</th><th>近12月涨幅</th></tr>
{cand_rows(cand)}
</table></div>
</div>

<h2>六、当下净利润断层观察池（108 条 · 2026-04 以来）</h2>
<div class="card">
<p>2026 中报季净利增速 ≥50% + 跳空 + 放量的全部信号。按信号日降序，前 40 条如下，
完整 CSV：<code>outputs/netprofit_gap_recent.csv</code>。</p>
<div class="callout blue"><b>用法：</b>与 §五候选池、与你的产业趋势判断取交集；同一行业内出现 3 家以上断层 = 行业景气实锤，
该行业内的所有候选股升一级优先级。下一步深研从"断层 + 趋势方向 + 市值 &lt;500 亿"的交集开始。</div>
<div class="scroll"><table>
<tr><th>#</th><th class='l'>名称</th><th class='l'>行业</th><th>信号日</th><th>报告期</th><th>净利增速</th><th>营收增速</th><th>市值(亿)</th><th>PE</th></tr>
{gap_rows(recent_gap)}
</table></div>
</div>

<h2>七、每季执行 SOP（照做即可）</h2>
<div class="card">
<table>
<tr><th>时间</th><th class='l'>动作</th><th class='l'>工具/命令</th></tr>
<tr><td>4月底·8月底·10月底<br>(财报季结束)</td><td class='l'>①重跑静态漏斗更新候选池<br>②扫当季净利润断层信号</td>
<td class='l'><code>python3 scripts/tenbagger_screen.py --asof &lt;最新交易日&gt;</code><br><code>python3 scripts/netprofit_gap_test.py</code>（改信号窗口）</td></tr>
<tr><td>每半年（1月/7月）</td><td class='l'>产业趋势定位复盘：渗透率、三大映射信号、拥挤度（板块成交额占比分位）</td><td class='l'>人工：券商策略报告/协会数据/美股巨头财报</td></tr>
<tr><td>持续</td><td class='l'>对交集股做 §四 checklist 深研；每只归档一份研究笔记（含证伪条件）</td><td class='l'>人工：年报/调研纪要/产业链访谈</td></tr>
<tr><td>触发式</td><td class='l'>持仓涨 50%+ → 按确认规则评估加仓；触发卖出三条件 → 执行</td><td class='l'>纪律清单</td></tr>
</table>
</div>

<h2>八、诚实的边界</h2>
<div class="card">
<ol>
<li><b>命中率天花板</b>：这套体系的目标是"研究池命中率 4-5 倍于随机 + 正期望收益"，不是"每年抓一只十倍股"。
2016 回测的最优静态池 37 只命中 1 只——这就是基准现实。</li>
<li><b>产业趋势判断是人工环节，会犯错</b>。2016 年判断"电子是未来"的人赚到大钱，判断"传媒影视是未来"的人没有。
用 2-4 个方向分散这个判断风险，用渗透率数据替代叙事。</li>
<li><b>断层信号有近一半跑输指数</b>（超额胜率 51%）。它缩小范围、确认趋势，但不免除深研。</li>
<li><b>幸存者偏差提醒</b>：本体系基于 206 只终值十倍股的共性归纳，执行中遇到的一切"这次不一样"都应先回到数据检验。</li>
<li><b>最大风险不是选错股，是拿不住和卖不对</b>：三窗口过山车率 49-67%，13/20 的十倍股前七年表现平庸。
组合层面的耐心（低换手）与纪律（证伪才卖）贡献了这套体系一半以上的价值。</li>
</ol>
</div>

<div class="disc">
<b>免责声明：</b>本报告基于公开数据与历史量化统计，所涉方法、候选列表与个股信息仅供研究参考，不构成任何投资建议或要约。
历史表现不预示未来收益，回测结果存在样本选择与过拟合风险。市场有风险，投资需谨慎；任何投资决策应结合自身风险承受能力独立作出，
必要时咨询持牌专业机构。<br><br>
数据：本地 PostgreSQL 量化库（tushare/akshare 源），2016-09 ~ 2026-09；脚本：<code>scripts/tenbagger_screen.py</code> ·
<code>scripts/netprofit_gap_test.py</code> · 结果 JSON/CSV 见 outputs/ 目录，全部可复现。
</div>
</div>

<script>
var AX = {{axisLabel:{{color:'#5c6675',fontSize:11}}, axisLine:{{lineStyle:{{color:'#c9d2e0'}}}}, splitLine:{{lineStyle:{{color:'#eef1f6'}}}}}};
function mk(id, opt){{var el=document.getElementById(id); if(!el) return; var c=echarts.init(el); c.setOption(opt); window.addEventListener('resize', function(){{c.resize();}});}}

mk('c_ind', {{
  title: {{text:'候选池行业分布（63只, Top10）', left:'center', textStyle:{{fontSize:13}}}},
  tooltip: {{}},
  grid: {{left:90, right:50, top:40, bottom:25}},
  xAxis: Object.assign({{type:'value'}}, AX),
  yAxis: Object.assign({{type:'category', data:{ind_names}.reverse()}}, AX),
  series: [{{type:'bar', data:{ind_vals}.slice().reverse(),
    itemStyle:{{color:'#2456a6', borderRadius:[0,4,4,0]}},
    label:{{show:true, position:'right', color:'#1a2332'}}}}]
}});

mk('c_gapdist', {{
  title: {{text:'净利润断层信号后 1 年收益分布（2,437 个信号）——右尾即幂律', left:'center', textStyle:{{fontSize:13}}}},
  tooltip: {{}},
  grid: {{left:60, right:30, top:40, bottom:40}},
  xAxis: Object.assign({{type:'category', data:{bin_labels}, axisLabel:{{color:'#5c6675',fontSize:10,rotate:45}}}}, {{axisLine:AX.axisLine, splitLine:AX.splitLine}}),
  yAxis: Object.assign({{type:'value', name:'信号数'}}, AX),
  series: [{{type:'bar', data:{bin_counts},
    itemStyle:{{color:function(p){{var i=p.dataIndex; return i<4?'#1a7a4a':(i<8?'#9aa4b8':'#c0392b');}}, borderRadius:[3,3,0,0]}},
    markLine:{{data:[{{xAxis:'0~25', label:{{formatter:'盈亏线', fontSize:10}}}}], lineStyle:{{color:'#1a2332', type:'dashed'}}}} }}]
}});
</script>
</body></html>"""

with open(OUT, "w", encoding="utf-8") as f:
    f.write(HTML_DOC)
print("->", OUT, f"({len(HTML_DOC)/1024:.0f} KB)")

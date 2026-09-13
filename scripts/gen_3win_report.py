# -*- coding: utf-8 -*-
"""三口径(5/10/20年)十倍股汇总报告"""
import json, html
from datetime import datetime

D5 = json.load(open("outputs/tenbagger_5y.json"))
D10 = json.load(open("outputs/tenbagger_analysis.json"))
D20 = json.load(open("outputs/tenbagger_20y.json"))
R5 = json.load(open("outputs/tenbagger_5y_rules_h2.json"))   # 2021中报口径
R10 = json.load(open("outputs/tenbagger_10y_rules_h2.json")) # 2016中报口径

s5, s10, s20 = D5["summary"], D10["common_summary"], D20["common_summary"]
base20 = D20["market_baseline"]
t10 = D10["tenbaggers"]; t20 = D20["hold_list"]; t5 = D5["hold_list"]

NOW = datetime.now().strftime("%Y-%m-%d %H:%M")

# ---- 名单行 ----
def row5(r, i):
    g = lambda k, f="{}": (f.format(r[k]) if r.get(k) is not None else "—")
    return (f"<tr><td>{i}</td><td class='l'>{html.escape(str(r['name']))}</td>"
            f"<td class='l'>{html.escape(str(r['industry'] or '—'))}</td>"
            f"<td class='num'><b>{r['final_mult']}x</b></td><td class='num'>{g('ever_max','{:.1f}x')}</td>"
            f"<td>{(r.get('first_10x') or '—')[:7]}</td><td class='num'>{g('pe')}</td><td class='num'>{g('pb')}</td>"
            f"<td class='num'>{g('mv','{:.0f}')}</td><td class='num'>{g('roe')}</td><td class='num'>{g('ni_mult','{:.1f}x')}</td></tr>")

def row10(r, i):
    g = lambda k, f="{}": (f.format(r[k]) if r.get(k) is not None else "—")
    return (f"<tr><td>{i}</td><td class='l'>{html.escape(str(r['name']))}</td>"
            f"<td class='l'>{html.escape(str(r['industry'] or '—'))}</td>"
            f"<td class='num'><b>{r['total_ret']}x</b></td>"
            f"<td>{(r.get('first_10x_date') or '—')[:7]}</td><td class='num'>{g('pe_start')}</td><td class='num'>{g('pb_start')}</td>"
            f"<td class='num'>{g('mv_start','{:.0f}')}</td><td class='num'>{g('roe16_ann')}</td><td class='num'>{g('ni_mult','{:.1f}x')}</td></tr>")

def row20(r, i):
    g = lambda k, f="{}": (f.format(r[k]) if r.get(k) is not None else "—")
    return (f"<tr><td>{i}</td><td class='l'>{html.escape(str(r['name']))}</td>"
            f"<td class='l'>{html.escape(str(r['industry'] or '—'))}</td>"
            f"<td class='num'><b>{r['final_mult']}x</b></td><td>{(r.get('first_10x') or '—')[:7]}</td>"
            f"<td class='num'>{g('pe','{:.0f}')}</td><td class='num'>{g('pb')}</td>"
            f"<td class='num'>{g('mv','{:.0f}')}</td><td class='num'>{g('roe')}</td><td class='num'>{g('ni_mult','{:.1f}x')}</td></tr>")

rows5 = "\n".join(row5(r, i+1) for i, r in enumerate(t5))
rows10 = "\n".join(row10(r, i+1) for i, r in enumerate(t10))
rows20_top = "\n".join(row20(r, i+1) for i, r in enumerate(t20[:30]))
rows20_all = "\n".join(row20(r, i+1) for i, r in enumerate(t20))

# 过山车行
rc5 = D5["roller_coasters"]
rc5_sorted = sorted(rc5, key=lambda x: -(x["ever_max"] - x["final_mult"]))
rc5_rows = "\n".join(f"<tr><td class='l'>{html.escape(str(r['name']))}</td><td class='num'>{r['ever_max']}x</td>"
                     f"<td class='num'><b class='neg'>{r['final_mult']}x</b></td></tr>" for r in rc5_sorted[:12])
rc10 = D5["ten_year_extra"]["roller_coasters"]
rc10_sorted = sorted(rc10, key=lambda x: -(x["ever_max"] - x["final_mult"]))
rc10_rows = "\n".join(f"<tr><td class='l'>{html.escape(str(r['name']))}</td><td class='num'>{r['ever_max']}x</td>"
                      f"<td class='num'><b class='neg'>{r['final_mult']}x</b></td></tr>" for r in rc10_sorted[:12])
rc20 = D20["roller_coasters"]
rc20_sorted = sorted(rc20, key=lambda x: -(x["ever_max"] - x["final_mult"]))
rc20_rows = "\n".join(f"<tr><td class='l'>{html.escape(str(r['name']))}</td><td class='num'>{r['ever_max']}x</td>"
                      f"<td class='num'><b class='neg'>{r['final_mult']}x</b></td></tr>" for r in rc20_sorted[:12])

# 规则精度矩阵（用中报口径的 5y/10y + 20y 年报口径）
RULE_ORDER = ["ROE>15% (质量派)", "营收增速>30% (成长派)", "PE<20 & PB<2 (便宜派)",
              "市值<100亿 (小市值)", "ROE>10% & 增速>20%", "ROE>15% & 增速>30% (质量+成长)", "PE<20 (纯便宜)"]
def rmap(rl):
    return {r["rule"]: r for r in rl}
m5, m10 = rmap(R5), rmap(R10)
# 20y 硬编码（2005年报口径, 之前运行输出）
m20 = {
    "ROE>15% (质量派)": (158, 24, 15.19),
    "营收增速>30% (成长派)": (298, 35, 11.74),
    "PE<20 & PB<2 (便宜派)": (175, 14, 8.00),
    "市值<100亿 (小市值)": (1120, 137, 12.23),
    "ROE>10% & 增速>20%": (182, 33, 18.13),
    "ROE>15% & 增速>30% (质量+成长)": (76, 13, 17.11),
    "PE<20 (纯便宜)": (None, None, None),
}
base = {"5y": 0.42, "10y": 0.79, "20y": 12.42}
rule_matrix_rows = ""
for rname in RULE_ORDER:
    r5 = m5.get(rname, {}); r10_ = m10.get(rname, {})
    n5, h5, p5 = r5.get("n"), r5.get("hit_hold"), r5.get("precision")
    n10, h10_, p10 = r10_.get("n"), r10_.get("hit_hold"), r10_.get("precision")
    n20, h20_, p20 = m20.get(rname, (None,)*3)
    def cell(n, h, p, b):
        if p is None: return "<td class='num sub'>—</td>"
        mult = p / b if b else 0
        col = "pos" if mult >= 1.5 else ("neg" if mult < 0.9 else "")
        return f"<td class='num'>{n} 只中 {h} · <b class='{col}'>{p:.2f}%</b>（{mult:.1f}x）</td>"
    short = rname.split("(")[0].strip()
    rule_matrix_rows += (f"<tr><td class='l'>{html.escape(rname)}</td>"
                         f"{cell(n5,h5,p5,base['5y'])}{cell(n10,h10_,p10,base['10y'])}{cell(n20,h20_,p20,base['20y'])}</tr>")

# 三口径核心数字
N = dict(
    u5=4353, u10=2652, u20=1348,
    h5=18, h10=20, h20=168,
    p5=0.41, p10=0.75, p20=12.5,
    t5=35, t10=45, t20=504,
    tp5=0.80, tp10=1.70, tp20=37.4,
    rc5=17, rc10=25, rc20=336,
    rcp5=49, rcp10=56, rcp20=67,
    ann5=58.5, ann10=26.0, ann20=12.9,
)
# 起点 PE/ROE
pe5, pe10, pe20 = 49.5, 81.3, 29.9
pem5, pem10, pem20 = 35.3, 62.8, 34.8
pep5, pep10, pep20 = 66, 62, 44
roep5, roep10, roep20 = 30, 76, 59
# 行业 No.1
ind5_1 = "通信设备（光模块/光通信）9/18 = 50%"
ind10_1 = "电子硬件链 12/20 = 60%"
ind20_1 = "电气设备/通信设备 各9/168 = 5%"
# 净利倍数
ni5, ni10, ni20 = 8.5, 21.5, 22.3

# 首次10x年份（5y 与 10y）
fy5 = s5["first10x_years"]
fy10 = s10["10x股 首次10x年份分布"]

HTML = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>A股十倍股三口径全景：五年·十年·二十年</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
<style>
  :root {{
    --bg:#f6f7f9; --card:#fff; --ink:#1c2333; --sub:#5a6478;
    --accent:#c0392b; --accent2:#2c5f8a; --pos:#c0392b; --neg:#1a7a4a;
    --line:#e4e7ee; --chip:#eef2f7;
  }}
  * {{box-sizing:border-box;margin:0;padding:0;}}
  body {{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--ink);line-height:1.7;padding:24px;}}
  .wrap {{max-width:1100px;margin:0 auto;}}
  header {{background:linear-gradient(135deg,#232a3d 0%,#3d3450 100%);color:#fff;border-radius:14px;padding:34px 38px;margin-bottom:20px;}}
  header h1 {{font-size:26px;margin-bottom:8px;}}
  header .meta {{color:#b8bfd4;font-size:13px;}}
  .tri {{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-top:22px;}}
  .tri .col {{background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.14);border-radius:10px;padding:14px 16px;}}
  .tri .col .t {{font-size:14px;font-weight:700;margin-bottom:6px;}}
  .tri .big {{font-size:24px;font-weight:700;}}
  .tri .k {{font-size:12px;color:#b8bfd4;}}
  section {{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:26px 30px;margin-bottom:20px;}}
  h2 {{font-size:19px;padding-bottom:10px;border-bottom:2px solid var(--line);margin-bottom:18px;}}
  h2 .no {{color:var(--accent);margin-right:8px;}}
  h3 {{font-size:15px;margin:18px 0 10px;color:var(--accent2);}}
  p {{margin:8px 0;}}
  .sub {{color:var(--sub);font-size:13px;}}
  table {{width:100%;border-collapse:collapse;font-size:13px;margin:10px 0;}}
  th {{background:var(--chip);text-align:center;padding:8px 10px;font-weight:600;white-space:nowrap;}}
  td {{padding:6px 10px;border-bottom:1px solid var(--line);text-align:center;white-space:nowrap;}}
  td.l {{text-align:left;}} td.num {{font-variant-numeric:tabular-nums;}}
  .neg {{color:var(--neg);}} .pos {{color:var(--pos);}}
  .charts {{display:grid;grid-template-columns:1fr 1fr;gap:18px;}}
  .chart {{height:330px;width:100%;}}
  .chart-full {{height:360px;width:100%;}}
  .grid2 {{display:grid;grid-template-columns:1fr 1fr;gap:16px;}}
  .card {{background:var(--chip);border-radius:10px;padding:16px 18px;}}
  .card h4 {{font-size:14px;margin-bottom:8px;}}
  .card ul {{padding-left:18px;font-size:13px;}}
  .card li {{margin:4px 0;}}
  .fold {{max-height:420px;overflow-y:auto;border:1px solid var(--line);border-radius:8px;}}
  .fold table {{margin:0;}}
  .concl {{border-left:4px solid var(--accent);background:#fdf6f5;padding:14px 18px;border-radius:0 8px 8px 0;margin:12px 0;}}
  .concl b {{color:var(--accent);}}
  .wbadge {{display:inline-block;padding:2px 12px;border-radius:12px;font-size:12px;color:#fff;margin-right:6px;}}
  footer {{text-align:center;color:var(--sub);font-size:12px;padding:16px 0 30px;}}
  @media (max-width:760px) {{ .charts,.grid2,.tri {{grid-template-columns:1fr;}} }}
</style>
</head>
<body>
<div class="wrap">

<header>
  <h1>🎯 A股十倍股三口径全景</h1>
  <div class="meta">五年（2021-09→2026-09）｜十年（2016-09→2026-09）｜二十年（2006-09→2026-09）｜ 数据：本地 PostgreSQL，月度复权净值，含退市股（无幸存者偏差）｜ 生成于 {NOW}</div>
  <div class="tri">
    <div class="col"><div class="t">五年口径</div>
      <div class="big">{N['h5']} 只 <span style="font-size:14px">/ {N['u5']:,}</span></div>
      <div class="k">终值≥10x · {N['p5']}% · 隐含年化 {N['ann5']}%</div></div>
    <div class="col"><div class="t">十年口径</div>
      <div class="big">{N['h10']} 只 <span style="font-size:14px">/ {N['u10']:,}</span></div>
      <div class="k">终值≥10x · {N['p10']}% · 隐含年化 {N['ann10']}%</div></div>
    <div class="col"><div class="t">二十年口径</div>
      <div class="big">{N['h20']} 只 <span style="font-size:14px">/ {N['u20']:,}</span></div>
      <div class="k">终值≥10x · {N['p20']}% · 隐含年化 {N['ann20']}%</div></div>
  </div>
</header>

<section>
  <h2><span class="no">壹</span>三口径总对比：一张表看懂</h2>
  <table>
    <tr><th style="width:20%">维度</th><th>五年（2021→2026）</th><th>十年（2016→2026）</th><th>二十年（2006→2026）</th></tr>
    <tr><td class="l">起点样本</td><td class="num">{N['u5']:,}</td><td class="num">{N['u10']:,}</td><td class="num">{N['u20']:,}</td></tr>
    <tr><td class="l">终值≥10x</td><td class="num"><b>{N['h5']} 只（{N['p5']}%）</b></td><td class="num"><b>{N['h10']} 只（{N['p10']}%）</b></td><td class="num"><b>{N['h20']} 只（{N['p20']}%）</b></td></tr>
    <tr><td class="l">曾摸到≥10x</td><td class="num">{N['t5']} 只（{N['tp5']}%）</td><td class="num">{N['t10']} 只（{N['tp10']}%）</td><td class="num">{N['t20']} 只（{N['tp20']}%）</td></tr>
    <tr><td class="l">坐过山车（曾达未守住）</td><td class="num neg">{N['rc5']} 只（{N['rcp5']}%）</td><td class="num neg">{N['rc10']} 只（{N['rcp10']}%）</td><td class="num neg">{N['rc20']} 只（{N['rcp20']}%）</td></tr>
    <tr><td class="l">隐含年化收益</td><td class="num">{N['ann5']}%</td><td class="num">{N['ann10']}%</td><td class="num">{N['ann20']}%</td></tr>
    <tr><td class="l">起点 PE 中位（vs 市场）</td><td class="num pos">{pe5} 倍（vs {pem5}，分位 {pep5}）</td><td class="num pos">{pe10} 倍（vs {pem10}，分位约 {pep10}）</td><td class="num">{pe20} 倍（vs {pem20}，分位 {pep20}）</td></tr>
    <tr><td class="l">起点 ROE 分位</td><td class="num neg">{roep5}（低于市场！）</td><td class="num">{roep10}</td><td class="num">{roep20}</td></tr>
    <tr><td class="l">行业集中度 No.1</td><td class="l">{ind5_1}</td><td class="l">{ind10_1}</td><td class="l">{ind20_1}（56 个行业）</td></tr>
    <tr><td class="l">净利倍数中位</td><td class="num pos">+{ni5}x</td><td class="num pos">+{ni10}x</td><td class="num pos">+{ni20}x</td></tr>
    <tr><td class="l">兑现节奏</td><td class="l">全部在 2025-26 集中兑现</td><td class="l">13/20 在 2024 年后</td><td class="l">多轮牛市（2015 年兑现 51 只）</td></tr>
    <tr><td class="l">龙头代表</td><td class="l">协创数据 26.7x · 中际旭创 26.4x</td><td class="l">中际旭创 106.7x · 长芯博创 67.7x</td><td class="l">格力电器 70.4x · 茅台 66.8x</td></tr>
  </table>
  <div class="concl">
    三口径放在一起，规律呈现完美的「<b>窗口—产业—起点</b>」结构：<br>
    ① <b>窗口越短，十倍越稀缺</b>（0.41% → 0.75% → 12.5%），因为隐含年化从 58.5% 降到 12.9%；<br>
    ② <b>窗口越短，越依赖单轮产业趋势</b>：五年组和十年组都是 AI/电子链一家独大（50-60%），二十年组 56 个行业百花齐放；<br>
    ③ <b>窗口越短，起点越「贵而差」</b>：五年组起点 ROE 分位仅 30（财务差于市场）、PE 分位 66（更贵）——短十倍赌的是「产业β+困境反转」，不是「好公司」；<br>
    ④ <b>过山车率随窗口上升</b>（49%→56%→67%）：曾达 10x 的股票，时间越长越大概率还回去；<br>
    ⑤ 唯一全口径不变式：<b>盈利倍数扛全部涨幅</b>（+8.5x / +21.5x / +22.3x），以及——<b>便宜与十倍股在三个口径全部负相关或零相关</b>。
  </div>
</section>

<section>
  <h2><span class="no">贰</span>清单①：五年口径（{N['h5']} 只，2021-09→2026-09）</h2>
  <table>
    <tr><th>#</th><th>名称</th><th>行业</th><th>终值</th><th>峰值</th><th>首次10x</th><th>起点PE</th><th>起点PB</th><th>市值(亿)</th><th>起点ROE%</th><th>净利倍数</th></tr>
    {rows5}
  </table>
  <p class="sub">起点=2021-09-10（daily_basic：pe_ttm/pb/总市值）+ 2020 年报 ROE；净利倍数=2025/2020 年报归母净利。起点 PE/PB 为 NaN 的多为亏损股（PE 无意义）——寒武纪、松发股份等起点就是亏损或微利。</p>
  <div class="concl">
    五年组的画像：<b>18 只里 9 只是光模块/光通信</b>（中际旭创、新易盛、天孚、长芯博创、长飞、仕佳、剑桥、太辰光、光库），加上寒武纪（AI 芯片）、协创（算力服务器）——<b>清一色 AI 算力产业链</b>。起点 ROE 中位仅 5.85%（市场 10.6%），起点亏损股占 1/3。全部 18 只的首次 10x 都发生在 <b>2025 年之后</b>：2021-2024 蛰伏三年，AI 行情两年兑现。
  </div>
  <div class="grid2">
    <div>
      <h3>五年口径的「过山车」（曾达未守住 17 只）</h3>
      <table>
        <tr><th>名称</th><th>曾达</th><th>终值</th></tr>
        {rc5_rows}
      </table>
    </div>
    <div>
      <h3>特征小结</h3>
      <div class="card">
        <ul>
          <li><b>起点画像</b>：PE 49.5 倍（分位 66）/ PB 3.96 / ROE 分位 30——<b>贵，且财务差于市场中位</b></li>
          <li><b>行业</b>：通信设备 9/18（50%），AI 算力链合计约 13/18</li>
          <li><b>盈利</b>：净利中位 +8.5 倍（五年内！）——涨得快的十倍靠「利润从 0 到 1」</li>
          <li><b>兑现</b>：2025 年 7 只 + 2026 年 11 只 = 全部；没有一只是在 2021-2024 达成的</li>
          <li><b>等待</b>：中位要先扛 3 年不涨（甚至下跌），最后 1-2 年主升浪</li>
        </ul>
      </div>
    </div>
  </div>
</section>

<section>
  <h2><span class="no">叁</span>清单②：十年口径（{N['h10']} 只，2016-09→2026-09）</h2>
  <table>
    <tr><th>#</th><th>名称</th><th>行业</th><th>终值</th><th>首次10x</th><th>起点PE</th><th>起点PB</th><th>市值(亿)</th><th>起点ROE%</th><th>净利倍数</th></tr>
    {rows10}
  </table>
  <p class="sub">起点=2016-09（daily_basic）+ 2016 中报/2015 年报；净利倍数=2025/2015 年报归母净利。</p>
  <div class="concl">
    十年组：<b>电子硬件链 12/20</b>（中际旭创、长芯博创、新易盛、胜宏、沪电、深南、生益、寒武纪、北方华创、兆易…），配牧原、汾酒、紫金、恒立各一。起点 PE 中位 <b>81 倍</b>（vs 市场 63）——比五年组还贵；但起点 ROE 分位 76（高于五年组的 30）：十年十倍要「贵+不差」，五年十倍只要「贵+有故事」。13/20 直到 2024 年后才兑现，净利中位 +21.5x。
  </div>
  <div class="grid2">
    <div>
      <h3>十年口径的「过山车」（曾达未守住 25 只，前 12）</h3>
      <table>
        <tr><th>名称</th><th>曾达</th><th>终值</th></tr>
        {rc10_rows}
      </table>
    </div>
    <div>
      <h3>特征小结</h3>
      <div class="card">
        <ul>
          <li><b>起点画像</b>：PE 81 倍（分位约 62）/ ROE 分位 76 / 市值 82 亿≈市场持平——「贵但基本面及格」</li>
          <li><b>行业</b>：电子硬件 60%，行业集中度最高</li>
          <li><b>盈利</b>：净利 +21.5x / EPS +25.2x；中际旭创 EPS +370x、PE 183→53（以盈利消化高估值）</li>
          <li><b>兑现</b>：幂律——剔掉涨幅最高 3 周，十年收益减半；13/20 在 2024 后才 10x</li>
          <li><b>例外样本</b>：牧原（起点 ROE 53%/PE 16，全场唯一「便宜的好公司」）</li>
        </ul>
      </div>
    </div>
  </div>
</section>

<section>
  <h2><span class="no">肆</span>清单③：二十年口径（{N['h20']} 只，2006-09→2026-09）</h2>
  <p>Top 30：</p>
  <table>
    <tr><th>#</th><th>名称</th><th>行业</th><th>终值</th><th>首次10x</th><th>起点PE</th><th>起点PB</th><th>市值(亿)</th><th>起点ROE%</th><th>净利倍数</th></tr>
    {rows20_top}
  </table>
  <details>
    <summary style="cursor:pointer;color:var(--accent2);font-size:13px;padding:6px 0;">▸ 展开全部 {N['h20']} 只</summary>
    <div class="fold">
    <table>
      <tr><th>#</th><th>名称</th><th>行业</th><th>终值</th><th>首次10x</th><th>起点PE</th><th>起点PB</th><th>市值(亿)</th><th>起点ROE%</th><th>净利倍数</th></tr>
      {rows20_all}
    </table>
    </div>
  </details>
  <div class="grid2" style="margin-top:12px;">
    <div>
      <h3>二十年口径的「过山车」（336 只，前 12）</h3>
      <table>
        <tr><th>名称</th><th>曾达</th><th>终值</th></tr>
        {rc20_rows}
      </table>
    </div>
    <div>
      <h3>特征小结</h3>
      <div class="card">
        <ul>
          <li><b>起点画像「中间化」</b>：PE 30（分位 44）/ PB 2.05≈市场 / ROE 6.7%（分位 59）/ 市值分位 43——<b>没有任何极端特征</b></li>
          <li><b>行业</b>：56 个行业，前九名各 6-9 只（电气设备、通信、制药、家电、白酒、航空、黄金、煤炭、半导体）</li>
          <li><b>盈利</b>：净利 +22.3x；格力 +57x、浪潮 +288x</li>
          <li><b>兑现</b>：多轮牛熊接力（2015 年一年兑现 51 只）；中位等待 6-8 年</li>
          <li><b>过山车</b>：336/504 = 67% 的曾达者没守住（万科 16.1x→2.2x）</li>
        </ul>
      </div>
    </div>
  </div>
</section>

<section>
  <h2><span class="no">伍</span>共同特征：三口径交叉验证</h2>
  <div class="charts">
    <div id="c_prob" class="chart"></div>
    <div id="c_start" class="chart"></div>
  </div>
  <div class="charts">
    <div id="c_ind" class="chart"></div>
    <div id="c_rc" class="chart"></div>
  </div>
  <h3>四条跨口径铁律</h3>
  <div class="grid2">
    <div class="card">
      <h4>铁律① 盈利增长是唯一硬驱动</h4>
      <ul>
        <li>净利倍数中位：五年 +8.5x → 十年 +21.5x → 二十年 +22.3x</li>
        <li>三口径估值端贡献都接近零或为负（中际旭创 PE 183→53）</li>
        <li>十倍股的本质 = <b>盈利十倍</b>，股价只是会计</li>
      </ul>
    </div>
    <div class="card">
      <h4>铁律② 「便宜」与十倍股彻底绝缘</h4>
      <ul>
        <li>便宜派（PE&lt;20&amp;PB&lt;2）精度：五年 <b class='neg'>0.00%</b>、十年 <b class='neg'>0.00%</b>、二十年 <b class='neg'>8.0%（低于随机 12.4%）</b></li>
        <li>三口径、六次测试、零命中——统计上这是「负选股」</li>
        <li>但便宜股组合亏钱吗？不——它只是与十倍股无关（防守有效、进攻无效，与价值因子报告一致）</li>
      </ul>
    </div>
    <div class="card">
      <h4>铁律③ 起点估值「不便宜」是必要非充分条件</h4>
      <ul>
        <li>起点 PE 分位：五年 66 / 十年 62 / 二十年 44——没有一个口径的十倍股起点是「市场最便宜的一半」</li>
        <li>短窗口（5/10 年）贵得更多：赌产业趋势的资本已经把预期定价进去了</li>
        <li>长窗口（20 年）起点平庸化：因为它们的 10x 来自多轮周期，而非起点定价</li>
      </ul>
    </div>
    <div class="card">
      <h4>铁律④ 窗口越短，起点质量信号越失效</h4>
      <ul>
        <li>起点 ROE 分位：五年 <b class='neg'>30</b>（差于市场）/ 十年 76 / 二十年 59</li>
        <li>五年十倍股起点 1/3 是亏损股——买的是「利润从 0 到 1」，静态报表无从判断</li>
        <li>时间越长，「持续的高 ROE」越能沉淀为复利（二十年组的 ROE 分位 59 来自「后来变好」而非「起点就好」）</li>
      </ul>
    </div>
  </div>
</section>

<section>
  <h2><span class="no">陆</span>识别规则矩阵：同一个规则，三个时点的成绩单</h2>
  <p>每个口径都用<b>起点时点全部可见信息</b>测试（五年/十年用最近中报+当期估值；二十年用 2005 年报，2006 中报覆盖不足）：</p>
  <table>
    <tr><th style="width:26%">规则</th><th>五年（基线 0.42%）</th><th>十年（基线 0.79%）</th><th>二十年（基线 12.42%）</th></tr>
    {rule_matrix_rows}
  </table>
  <div id="c_rules" class="chart-full"></div>
  <div class="concl">
    规则有效性的形态是「<b>倒U</b>」：二十年口径所有规则都「弱有效」（精度 1.2-1.5 倍随机，因为基数高）；十年口径「质量+成长」信号最强（ROE&gt;10%&amp;增速&gt;20% 精度 2.78% = 3.5 倍随机；严格版 6.67% = 8.4 倍但组合仅 15 只、个例依赖牧原）；<b>五年口径所有质量类规则全部归零</b>（2021 时点的十倍股起点大多亏损或微利）。<br>
    跨口径看：<b>「质量+成长」是唯一在长窗口持续有效的静态规则</b>（二十年精度 18.1%、十年 2.8-6.7%）——但它在短窗口失效，且永远漏掉 80% 以上的十倍股。<b>没有一条静态规则能跨三个口径保持强有效。</b>
  </div>
  <p class="sub">注：财务报告期选择对结果影响很大（十年口径用 2015 年报时质量+成长为 0 命中，用 2016 中报时为 1-2 命中）——这本身就是「静态规则脆弱性」的证据。</p>
</section>

<section>
  <h2><span class="no">柒</span>方法论：按口径分别给答案</h2>
  <div class="grid2">
    <div class="card">
      <h4>五年十倍（年化 58.5%）：产业趋势的即期押注</h4>
      <ul>
        <li><b>唯一路径</b>：押中一轮正在爆发的超级产业（本轮 = AI 算力：18 只里约 13 只）</li>
        <li><b>静态财务完全无用</b>：所有质量/估值规则精度为零——起点看「订单、产能、产业渗透率」这类报表外的信息</li>
        <li><b>可行做法</b>：①产业趋势定位（算力资本开支、技术渗透曲线）；②产业链宽筛（不选个股，买链条 15-30 只）；③<b>追涨确认</b>（东吴复盘：涨 50% 后成为十倍股概率从 2%→22%，5x→10x 概率 43%）</li>
        <li><b>代价</b>：0.41% 命中率 + 三年蛰伏期；组合化是唯一风险控制手段</li>
      </ul>
    </div>
    <div class="card">
      <h4>十年十倍（年化 26%）：趋势 + 质量的接力</h4>
      <ul>
        <li><b>产业趋势仍是主因</b>（电子硬件 60%），但「质量+成长」开始有信号（3.5 倍随机）</li>
        <li><b>可行做法</b>：①在产业趋势内用 ROE&gt;10%&amp;增速&gt;20% 做质量门限（漏掉牧原式个例但提升期望）；②小市值内选择（9/20 起点市值&lt;100 亿）；③接受起点贵（PE 分位 62），用「盈利消化估值」的框架验证（EPS 增速 &gt;&gt; PE 降幅）</li>
        <li><b>兑现耐心</b>：13/20 在第 8-10 年才达成；前半程收益贡献极低</li>
      </ul>
    </div>
  </div>
  <div class="grid2" style="margin-top:14px;">
    <div class="card">
      <h4>二十年十倍（年化 12.9%）：时间 × 存活 × 卖出纪律</h4>
      <ul>
        <li><b>概率高达 12.5%</b>（八年一遇）——比选股更重要的是<b>仓位和耐心</b>：中位等 6-8 年、中途必有 -50% 级回撤</li>
        <li><b>可行做法</b>：①质量门限宽筛做底仓（ROE&gt;10%&amp;增速&gt;20%，精度 18% = 1.5 倍随机，组合 30-50 只）；②每年检视持仓行业是否踩在当轮趋势（家电→白酒→黄金→AI 的接力）；③<b>卖出 = 证伪</b>（产业逻辑终结如万科，才卖）</li>
        <li><b>关键纪律</b>：67% 的曾达者还回去了——牛市后段对「曾达 5x+」的持仓部分止盈，是对幂律最现实的妥协</li>
      </ul>
    </div>
    <div class="card">
      <h4>统一框架（六层体系，三口径通用）</h4>
      <ul>
        <li><b>1. 产业趋势定位</b>（β 之源）：五年的 AI、十年的电子、二十年的多轮接力——没有产业趋势就没有十倍</li>
        <li><b>2. 宽筛组合</b>：承认静态精度上限（5 年 0%、10 年 3-8 倍随机、20 年 1.5 倍），用组合赔率替代个股确定性</li>
        <li><b>3. 追涨确认</b>：涨 50% 后概率跃升数倍——放弃「从 1x 买满 10x」的执念</li>
        <li><b>4. 等得起</b>：仓位设计必须让人扛得过 3 年蛰伏 / -50% 回撤</li>
        <li><b>5. 拿住吃幂律</b>：剔掉最高 3 周收益减半——下车成本极高</li>
        <li><b>6. 卖出 = 证伪 + 泡沫兑现</b>：产业终结才清仓；牛市后段部分止盈</li>
      </ul>
    </div>
  </div>
  <div class="concl">
    与既有体系的衔接：价值因子（双门限 6.3%/夏普 0.36）与 prod_lgbm_neu 负责底仓与防守；十倍股体系是进攻端的「产业趋势卫星仓」。两者按 80/20 或 70/30 混合，风险预算内追求幂律尾部。
  </div>
</section>

<footer>
  数据来源：本地 PostgreSQL（tushare，2006-2026；2010-2015 已回补）｜ 脚本：tenbagger_5y.py / tenbagger_analysis.py / tenbagger_20y.py（规则测试见 *_rules_h2.json）｜ 结果：outputs/tenbagger_5y.json 等<br>
  本报告基于公开数据与量化统计，仅供研究参考，不构成投资建议。市场有风险，投资需谨慎。过往表现不预示未来收益。
</footer>

</div>
<script>
var AX = {{axisLine: {{lineStyle: {{color:'#8a93a8'}}}}, axisLabel: {{color:'#5a6478'}}}};
function mk(id, opt) {{
  var el = document.getElementById(id); if (!el) return;
  var c = echarts.init(el);
  c.setOption(Object.assign({{textStyle: {{fontFamily: '-apple-system, PingFang SC, sans-serif'}}}}, opt));
  window.addEventListener('resize', function(){{c.resize();}});
}}

mk('c_prob', {{
  title: {{text: '十倍股概率随窗口拉长（对数轴）', left:'center', textStyle:{{fontSize:13}}}},
  tooltip: {{trigger:'axis'}},
  grid: {{left:50, right:20, top:60, bottom:40}},
  xAxis: Object.assign({{type:'category', data:['五年','十年','二十年']}}, AX),
  yAxis: Object.assign({{type:'log', axisLabel:{{formatter:'{{c}}%'}}}}, AX),
  series: [
    {{name:'终值≥10x 概率', type:'bar', data:[{N['p5']}, {N['p10']}, {N['p20']}], itemStyle:{{color:'#c0392b'}}, label:{{show:true, position:'top', formatter:'{{c}}%'}}}},
    {{name:'曾达≥10x 概率', type:'bar', data:[{N['tp5']}, {N['tp10']}, {N['tp20']}], itemStyle:{{color:'#b0894d'}}, label:{{show:true, position:'top', formatter:'{{c}}%'}}}}
  ],
  legend: {{top:28}}
}});

mk('c_start', {{
  title: {{text: '起点画像：PE 分位 & ROE 分位（50=市场中位）', left:'center', textStyle:{{fontSize:13}}}},
  tooltip: {{}},
  grid: {{left:50, right:20, top:60, bottom:40}},
  xAxis: Object.assign({{type:'category', data:['五年','十年','二十年']}}, AX),
  yAxis: Object.assign({{type:'value', max:100}}, AX),
  series: [
    {{name:'起点PE分位', type:'bar', data:[{pep5}, {pep10}, {pep20}], itemStyle:{{color:'#c0392b'}}, label:{{show:true, position:'top'}}}},
    {{name:'起点ROE分位', type:'bar', data:[{roep5}, {roep10}, {roep20}], itemStyle:{{color:'#2c5f8a'}}, label:{{show:true, position:'top'}}}}
  ],
  legend: {{top:28}},
  markLine: {{data:[{{yAxis:50}}]}}
}});

mk('c_ind', {{
  title: {{text: '行业集中度：第一大行业占比', left:'center', textStyle:{{fontSize:13}}}},
  tooltip: {{}},
  grid: {{left:60, right:30, top:50, bottom:40}},
  xAxis: Object.assign({{type:'category', data:['五年\\n(通信设备/AI链)','十年\\n(电子硬件)','二十年\\n(电气设备)']}}, AX),
  yAxis: Object.assign({{type:'value', max:60, axisLabel:{{formatter:'{{c}}%'}}}}, AX),
  series: [{{type:'bar', data:[50, 60, 5], itemStyle:{{color:'#b0894d'}},
    label:{{show:true, position:'top', formatter:function(p){{return p.value+'%';}}}}}}]
}});

mk('c_rc', {{
  title: {{text: '过山车率：曾达10x未守住的比例', left:'center', textStyle:{{fontSize:13}}}},
  tooltip: {{}},
  grid: {{left:50, right:30, top:50, bottom:40}},
  xAxis: Object.assign({{type:'category', data:['五年','十年','二十年']}}, AX),
  yAxis: Object.assign({{type:'value', max:80, axisLabel:{{formatter:'{{c}}%'}}}}, AX),
  series: [{{type:'bar', data:[{N['rcp5']}, {N['rcp10']}, {N['rcp20']}], itemStyle:{{color:'#1a7a4a'}},
    label:{{show:true, position:'top', formatter:function(p){{return p.value+'%';}}}}}}]
}});

mk('c_rules', {{
  title: {{text: '十年口径：各规则精度 vs 随机基线（0.79%）的倍数', left:'center', textStyle:{{fontSize:13}}}},
  tooltip: {{}},
  grid: {{left:170, right:60, top:40, bottom:30}},
  xAxis: Object.assign({{type:'value'}}, AX),
  yAxis: Object.assign({{type:'category', data:['ROE>10%&增速>20%','ROE>15%&增速>30%','ROE>15% 质量派','增速>30% 成长派','PE<20&PB<2 便宜派'].reverse()}}, AX),
  series: [{{type:'bar', data:[3.5, 8.4, 2.5, 1.2, 0],
    itemStyle:{{color:function(p){{return p.value>=2?'#c0392b':(p.value<1?'#1a7a4a':'#9aa4b8');}}}},
    label:{{show:true, position:'right', formatter:function(p){{return p.value>0?p.value.toFixed(1)+'x':'零命中';}}}}}}]
}});

document.querySelectorAll('details').forEach(function(d){{ d.addEventListener('toggle', function(){{ window.dispatchEvent(new Event('resize')); }}); }});
</script>
</body>
</html>
"""

out = "outputs/a股十倍股三口径全景.html"
open(out, "w").write(HTML)
print("written", out, len(HTML), "chars")

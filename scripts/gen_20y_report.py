# -*- coding: utf-8 -*-
"""从 outputs/tenbagger_20y.json + tenbagger_analysis.json 生成二十年十倍股 HTML 报告"""
import json, html
from datetime import datetime

D20 = json.load(open("outputs/tenbagger_20y.json"))
D10 = json.load(open("outputs/tenbagger_analysis.json"))

hold = D20["hold_list"]
rc = D20["roller_coasters"]
base = D20["market_baseline"]
cs = D20["common_summary"]

n_uni = D20["universe_n"]
n_hold = D20["n_hold"]
n_touch = D20["n_touched"]
n_rc = len(rc)

# 十年组对照数据
t10 = D10["tenbaggers"]
cs10 = D10["common_summary"]

fmt = lambda x, d=1: (f"{x:.{d}f}" if isinstance(x, (int, float)) else "—")

# ---------- 名单表格（全部168只） ----------
def row_html(r, rank):
    pe = f"{r['pe']:.0f}" if r.get("pe") else "—"
    pb = f"{r['pb']:.2f}" if r.get("pb") else "—"
    mv = f"{r['mv']:.0f}" if r.get("mv") else "—"
    roe = f"{r['roe']:.1f}" if r.get("roe") else "—"
    ni = f"{r['ni_mult']:.0f}x" if r.get("ni_mult") else "—"
    y10 = str(r["first_10x"])[:7] if r.get("first_10x") else "—"
    ev = f"{r['ever_max']:.0f}x" if r.get("ever_max") else "—"
    return (f"<tr><td>{rank}</td><td class='l'>{html.escape(r['name'])}</td>"
            f"<td class='l'>{html.escape(r['industry'] or '—')}</td>"
            f"<td class='num'><b>{r['final_mult']:.1f}x</b></td><td class='num'>{ev}</td>"
            f"<td>{y10}</td><td class='num'>{pe}</td><td class='num'>{pb}</td>"
            f"<td class='num'>{mv}</td><td class='num'>{roe}</td><td class='num'>{ni}</td></tr>")

rows_all = "\n".join(row_html(r, i + 1) for i, r in enumerate(hold))
rows_top30 = "\n".join(row_html(r, i + 1) for i, r in enumerate(hold[:30]))

# 过山车表（前30示例 + 统计）
rc_sorted = sorted(rc, key=lambda x: -(x["ever_max"] - x["final_mult"]))
rc_rows = "\n".join(
    f"<tr><td class='l'>{html.escape(r['name'])}</td><td class='num'>{r['ever_max']:.1f}x</td>"
    f"<td class='num'><b class='neg'>{r['final_mult']:.1f}x</b></td>"
    f"<td class='num neg'>还回 {r['ever_max'] - r['final_mult']:.0f} 倍</td></tr>"
    for r in rc_sorted[:24])

# 行业分布 top15
ind_sorted = sorted(cs["行业分布"].items(), key=lambda x: -x[1])[:15]
ind_labels = json.dumps([k for k, _ in ind_sorted], ensure_ascii=False)
ind_values = json.dumps([v for _, v in ind_sorted])

# 首次10x年份
fy = {k: v for k, v in cs["首次10x年份分布"].items()}
fy_labels = json.dumps(list(fy.keys()))
fy_values = json.dumps(list(fy.values()))

# 起点画像对比（10x股 vs 全市场中位）
profile_labels = json.dumps(["市值(亿)", "PE", "PB", "ROE%", "营收增速%"])
profile_10x = json.dumps([round(cs["20x股 mv 中位(亿)"],1), round(cs["20x股 pe 中位"],1), round(cs["20x股 pb 中位"],2), round(cs["20x股 roe05 中位"],1), round(cs["20x股 ory05 中位"],1)])
profile_mkt = json.dumps([round(base["mv_median"],1), round(base["pe_median"],1), round(base["pb_median"],2), round(base["roe_median"],1), round(base["ory_median"],1)])

# 分位雷达
pct_labels = json.dumps(["市值分位", "PE分位", "ROE分位", "营收增速分位"])
pct_values = json.dumps([round(cs["20x股 mv 分位中位"],1), round(cs["20x股 pe 分位中位"],1), round(cs["20x股 roe 分位中位"],1), round(cs["20x股 ory 分位中位"],1)])

# 规则测试数据（硬编码自运行输出）
rules = [
    ("ROE>10% & 营收增速>20%（质量+成长）", 182, 33, 60, 18.13),
    ("ROE>15% & 增速>30%（严格质量+成长）", 76, 13, 27, 17.11),
    ("ROE>15%（质量派）", 158, 24, 50, 15.19),
    ("ROE>15% & PE<30（质量+合理价）", 134, 20, 42, 14.93),
    ("市值<50亿（小市值）", 1120, 137, 429, 12.23),
    ("营收增速>30%（纯成长派）", 298, 35, 103, 11.74),
    ("PE<20 & PB<2（便宜派）", 175, 14, 47, 8.00),
    ("PEG<1（林奇派）", 1, 0, 0, 0.00),
]
rule_rows = "\n".join(
    f"<tr><td class='l'>{n}</td><td class='num'>{c}</td><td class='num'>{h}</td>"
    f"<td class='num'>{t}</td><td class='num'>{p:.1f}%</td>"
    f"<td class='num'>{'+' if p>=12.42 else ''}{p-12.42:.1f}pp</td></tr>"
    for n, c, h, t, p in rules)

rule_names = json.dumps([r[0].split("（")[0] for r in rules], ensure_ascii=False)
rule_prec = json.dumps([r[4] for r in rules])

# 质量成长命中名单
qg_hits = ['云南白药', '格力电器', '神火股份', '思源电气', '三花智控', '生益科技', '万华化学', '华阳股份', '浙江龙盛', '华鲁恒升', '片仔癀', '贵州茅台', '山东黄金']

# 十年 vs 二十年对比表
cmp_rows = [
    ("起点窗口", "2016-09 → 2026-09", "2006-09 → 2026-09"),
    ("起点样本（已上市）", f"{D10['universe_n']:,} 只", f"{n_uni:,} 只"),
    ("终值≥10x", f"{len(t10)} 只（{len(t10)/D10['universe_n']*100:.2f}%）", f"{n_hold} 只（{n_hold/n_uni*100:.1f}%）"),
    ("曾摸到≥10x", "未统计", f"{n_touch} 只（{n_touch/n_uni*100:.1f}%）"),
    ("坐过山车（曾达未守住）", "未统计", f"{n_rc} 只（占曾达 {n_rc/n_touch*100:.0f}%）"),
    ("起点 PE 中位（vs 全市场）", f"81 倍（vs 63）", f"{cs['20x股 pe 中位']:.0f} 倍（vs {base['pe_median']:.0f}）"),
    ("起点市值中位（vs 全市场）", "82 亿（vs 84）", f"{cs['20x股 mv 中位(亿)']:.0f} 亿（vs {base['mv_median']:.0f}）"),
    ("起点 ROE 中位（vs 全市场）", "分位 76，绝对值平庸", f"{cs['20x股 roe05 中位']:.1f}%（vs {base['roe_median']:.1f}%）"),
    ("行业结构", "电子硬件链 12/20（单轮产业趋势）", "前 9 大行业各 6-9 只（多轮周期叠加）"),
    ("静态规则最强精度", "1.98%（随机 3.4 倍）", "18.1%（随机 1.5 倍）"),
    ("净利倍数中位", "+21.5x", f"+{cs['20x股 净利倍数中位']:.0f}x"),
    ("典型兑现节奏", "13/20 在 2024 后才达 10x", "首次 10x 集中于 2010/2014/2015/2017（多轮牛熊）"),
]
cmp_html = "\n".join(
    f"<tr><td class='l'>{a}</td><td class='l'>{b}</td><td class='l'>{c}</td></tr>" for a, b, c in cmp_rows)

NOW = datetime.now().strftime("%Y-%m-%d %H:%M")

HTML = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>A股二十年十倍股全景解剖（2006-2026）</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
<style>
  :root {{
    --bg: #f6f7f9; --card: #ffffff; --ink: #1c2333; --sub: #5a6478;
    --accent: #c0392b; --accent2: #2c5f8a; --pos: #c0392b; --neg: #1a7a4a;
    --line: #e4e7ee; --chip: #eef2f7;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         background: var(--bg); color: var(--ink); line-height: 1.7; padding: 24px; }}
  .wrap {{ max-width: 1080px; margin: 0 auto; }}
  header {{ background: linear-gradient(135deg, #232a3d 0%, #3d3450 100%); color: #fff;
           border-radius: 14px; padding: 34px 38px; margin-bottom: 20px; }}
  header h1 {{ font-size: 26px; margin-bottom: 8px; }}
  header .meta {{ color: #b8bfd4; font-size: 13px; }}
  .hero {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 14px; margin: 22px 0 4px; }}
  .hero .cell {{ background: rgba(255,255,255,.08); border: 1px solid rgba(255,255,255,.14);
               border-radius: 10px; padding: 14px 16px; }}
  .hero .v {{ font-size: 26px; font-weight: 700; }}
  .hero .k {{ font-size: 12px; color: #b8bfd4; }}
  section {{ background: var(--card); border: 1px solid var(--line); border-radius: 12px;
            padding: 26px 30px; margin-bottom: 20px; }}
  h2 {{ font-size: 19px; padding-bottom: 10px; border-bottom: 2px solid var(--line); margin-bottom: 18px; }}
  h2 .no {{ color: var(--accent); margin-right: 8px; }}
  h3 {{ font-size: 15px; margin: 18px 0 10px; color: var(--accent2); }}
  p {{ margin: 8px 0; color: var(--ink); }}
  .sub {{ color: var(--sub); font-size: 13px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; margin: 10px 0; }}
  th {{ background: var(--chip); text-align: center; padding: 8px 10px; font-weight: 600; white-space: nowrap; }}
  td {{ padding: 6px 10px; border-bottom: 1px solid var(--line); text-align: center; white-space: nowrap; }}
  td.l {{ text-align: left; }}
  td.num {{ font-variant-numeric: tabular-nums; }}
  .neg {{ color: var(--neg); }}
  .pos {{ color: var(--pos); }}
  .charts {{ display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }}
  .chart {{ height: 340px; width: 100%; }}
  .chart-full {{ height: 360px; width: 100%; }}
  .grid2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
  .card {{ background: var(--chip); border-radius: 10px; padding: 16px 18px; }}
  .card h4 {{ font-size: 14px; margin-bottom: 8px; }}
  .card ul {{ padding-left: 18px; font-size: 13px; }}
  .card li {{ margin: 4px 0; }}
  .fold {{ max-height: 430px; overflow-y: auto; border: 1px solid var(--line); border-radius: 8px; }}
  .fold table {{ margin: 0; }}
  .tag {{ display: inline-block; background: var(--chip); border-radius: 5px; padding: 1px 8px;
         font-size: 12px; margin: 2px; }}
  .concl {{ border-left: 4px solid var(--accent); background: #fdf6f5; padding: 14px 18px;
           border-radius: 0 8px 8px 0; margin: 12px 0; }}
  .concl b {{ color: var(--accent); }}
  footer {{ text-align: center; color: var(--sub); font-size: 12px; padding: 16px 0 30px; }}
  @media (max-width: 760px) {{ .charts, .grid2 {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>
<div class="wrap">

<header>
  <h1>📊 A股二十年十倍股全景解剖</h1>
  <div class="meta">窗口 2006-09-12 → 2026-09-11 ｜ 起点 1348 只（2006-09 前已上市，含后来退市者）｜ 数据：本地 PostgreSQL（tushare 口径，复权收益）｜ 生成于 {NOW}</div>
  <div class="hero">
    <div class="cell"><div class="v">{n_uni:,}</div><div class="k">起点样本</div></div>
    <div class="cell"><div class="v">{n_hold}</div><div class="k">终值≥10x（{n_hold/n_uni*100:.1f}%）</div></div>
    <div class="cell"><div class="v">{n_touch}</div><div class="k">曾摸到≥10x（{n_touch/n_uni*100:.1f}%）</div></div>
    <div class="cell"><div class="v">{n_rc}</div><div class="k">坐了过山车（占曾达 {n_rc/n_touch*100:.0f}%）</div></div>
  </div>
</header>

<section>
  <h2><span class="no">壹</span>一句话结论</h2>
  <div class="concl">
    二十年窗口里，<b>每 8 只股票就有 1 只（12.5%）终值十倍</b>——是十年窗口（0.76%）的 <b>16 倍</b>；但<b>每 3 只曾摸到 10x 的股票，有 2 只会把它还回去</b>（336/504）。
    时间是十倍股最大的盟友，<b>起点时它们几乎没有任何可识别的极端特征</b>（PE 分位 44 / 市值分位 43 / ROE 分位 59，全部"中间偏上"）——
    二十倍股是被「活着 + 多轮产业周期 + 盈利增长 22 倍」熬出来的，不是被起点财务选出来的。
  </div>
  <p class="sub">口径说明：月度复权净值（pct_chg 连乘，已验证含除权修正）；「曾达」= 任意月末净值 ≥10x；退市股以最后真实交易月定格（曾达统计包含退市者，无幸存者偏差）。</p>
</section>

<section>
  <h2><span class="no">贰</span>名单：终值 ≥10x 的 {n_hold} 只</h2>
  <p>前三名：<b>格力电器 70.4x</b>、浪潮信息 69.3x、贵州茅台 66.8x。Top 30：</p>
  <table>
    <tr><th>#</th><th>名称</th><th>行业</th><th>终值</th><th>历史峰值</th><th>首次10x</th><th>起点PE</th><th>起点PB</th><th>市值(亿)</th><th>起点ROE%</th><th>净利倍数</th></tr>
    {rows_top30}
  </table>
  <details>
    <summary style="cursor:pointer;color:var(--accent2);font-size:13px;padding:6px 0;">▸ 展开全部 {n_hold} 只（可滚动）</summary>
    <div class="fold">
    <table>
      <tr><th>#</th><th>名称</th><th>行业</th><th>终值</th><th>历史峰值</th><th>首次10x</th><th>起点PE</th><th>起点PB</th><th>市值(亿)</th><th>起点ROE%</th><th>净利倍数</th></tr>
      {rows_all}
    </table>
    </div>
  </details>
  <p class="sub">起点财务 = 2005 年报（2006-09 时点最近可得年报）；净利倍数 = 2025 年报归母净利 / 2005 年报归母净利。</p>
</section>

<section>
  <h2><span class="no">叁</span>坐过山车的 {n_rc} 只：十倍易得，守住太难</h2>
  <div class="grid2">
    <div>
      <p>曾达 10x 但终点不足 10x 的有 <b>{n_rc} 只</b>——占所有曾达者的 <b>{n_rc/n_touch*100:.0f}%</b>。包括：</p>
      <ul style="font-size:13px;padding-left:18px;">
        <li><b>万科A</b>：曾 16.1x → 终值 2.2x（地产周期终结）</li>
        <li><b>平安银行</b>：曾 13.3x → 终值 8.0x</li>
        <li><b>多只退市股</b>：曾达 10x 后退市归零（国华退 12.2x → 0.2x）</li>
      </ul>
      <div class="concl" style="margin-top:10px;">
        「买入持有不动」在二十年维度有 <b>2/3 概率把 10x 还回去</b>。卖出纪律（基本面证伪 / 产业逻辑终结）不是可选项，是十倍股策略的另一半。
      </div>
    </div>
    <div>
      <table>
        <tr><th>名称</th><th>曾达峰值</th><th>终值</th><th>落差</th></tr>
        {rc_rows}
      </table>
    </div>
  </div>
</section>

<section>
  <h2><span class="no">肆</span>共同点一：起点画像——「平庸的大多数」</h2>
  <p>二十倍股起点（2005 年报 + 2006-09 价格）与全市场对比：<b>没有一项指标显著偏离市场</b>。</p>
  <div class="charts">
    <div id="c_profile" class="chart"></div>
    <div id="c_pct" class="chart"></div>
  </div>
  <table>
    <tr><th>指标</th><th>20x股中位</th><th>全市场中位</th><th>差异</th><th>解读</th></tr>
    <tr><td class="l">起点市值</td><td>{cs['20x股 mv 中位(亿)']:.1f} 亿</td><td>{base['mv_median']:.1f} 亿</td><td>持平（分位 43）</td><td class="l">不需要「小市值弹性」</td></tr>
    <tr><td class="l">起点 PE</td><td>{cs['20x股 pe 中位']:.1f} 倍</td><td>{base['pe_median']:.1f} 倍</td><td>略低（分位 44）</td><td class="l">不需要「贵」，也不靠「便宜」</td></tr>
    <tr><td class="l">起点 PB</td><td>{cs['20x股 pb 中位']:.2f}</td><td>{base['pb_median']:.2f}</td><td>持平</td><td class="l">—</td></tr>
    <tr><td class="l">起点 ROE</td><td>{cs['20x股 roe05 中位']:.1f}%</td><td>{base['roe_median']:.1f}%</td><td>略高（分位 59）</td><td class="l">「及格但不卓越」</td></tr>
    <tr><td class="l">营收增速</td><td>{cs['20x股 ory05 中位']:.1f}%</td><td>{base['ory_median']:.1f}%</td><td>略高（分位 58）</td><td class="l">同上</td></tr>
    <tr><td class="l">毛利率</td><td>{cs['20x股 毛利率中位']:.1f}%</td><td>—</td><td>平庸</td><td class="l">护城河不是起点特征</td></tr>
  </table>
  <div class="concl">
    对比十年组（PE 81 倍、显著贵于市场），二十倍股的起点「去特征化」印证：<b>窗口越长，起点信息越不重要，过程信息（产业周期 + 公司进化）越重要</b>。十年十倍股靠单轮产业趋势（起点必须"贵得有理由"），二十年十倍股靠多轮周期复利（起点只需"活着且不差"）。
  </div>
</section>

<section>
  <h2><span class="no">伍</span>共同点二：行业百花齐放 + 盈利驱动 + 牛市兑现</h2>
  <div class="charts">
    <div id="c_ind" class="chart"></div>
    <div id="c_fy" class="chart"></div>
  </div>
  <div class="grid2">
    <div class="card">
      <h4>① 行业极度分散（vs 十年组的电子垄断）</h4>
      <ul>
        <li>前 9 大行业各只占 6-9 只：电气设备、通信设备、化学制药、家电、中成药、白酒、农药化肥、半导体、航空</li>
        <li>覆盖 <b>56 个行业</b>——消费、周期（黄金/煤炭/稀土）、制造升级、公用事业全部在列</li>
        <li>对照十年组：电子硬件链 12/20 一家独大</li>
      </ul>
    </div>
    <div class="card">
      <h4>② 盈利是唯一硬共同点</h4>
      <ul>
        <li>净利中位涨 <b>{cs['20x股 净利倍数中位']:.0f} 倍</b>、营收中位涨 {cs['20x股 rev 倍数中位']:.0f} 倍（2005→2025 年报）</li>
        <li>格力净利 +56.9x、茅台 +73.6x、浪潮信息 +288x——涨幅的绝大部分由盈利解释</li>
        <li>与十年组（净利 +21.5x）同构：<b>十倍股的本质是盈利十倍</b></li>
      </ul>
    </div>
  </div>
  <div class="card" style="margin-top:14px;">
    <h4>③ 首次 10x 的时间规律：牛市批量兑现</h4>
    <ul>
      <li><b>2015 年牛市一次性兑现 51 只</b>（占全部 168 只的 30%）；2010 年 20 只、2017 年 16 只、2014 年 16 只</li>
      <li>起点到首次 10x 的中位等待约 <b>6-8 年</b>——即便买中，也大概率要熬过 2008（-70%）与 2011-2014 长熊</li>
      <li>含义：<b>「等得起」是十倍股策略的隐性成本</b>；2015/2021 泡沫期不减仓 = 把纸面 10x 交还市场（呼应第叁节）</li>
    </ul>
  </div>
</section>

<section>
  <h2><span class="no">陆</span>2006 时点识别规则测试：能不能提前找到？</h2>
  <p>用 2006-09-12 时点全部可见信息（2005 年报 + 当时价格），测试 8 类经典选股规则：</p>
  <table>
    <tr><th>规则</th><th>组合规模</th><th>终值10x命中</th><th>曾达10x命中</th><th>精度</th><th>vs 随机(12.4%)</th></tr>
    {rule_rows}
    <tr style="background:var(--chip);"><td class="l"><b>全市场（无规则）</b></td><td class="num">1,272</td><td class="num">158</td><td class="num">475</td><td class="num"><b>12.42%</b></td><td class="num">基线</td></tr>
  </table>
  <div id="c_rules" class="chart-full"></div>
  <div class="grid2">
    <div class="card">
      <h4>✅ 唯一稳定有效的还是「质量+成长」</h4>
      <ul>
        <li><b>ROE>10% & 营收增速>20%</b>：精度 18.1%，随机基线的 1.46 倍，但依然漏掉 80% 的十倍股</li>
        <li>该规则 2006 年命中的 13 只全是后来的核心资产：<span class="tags">{ ''.join(f'<span class="tag">{n}</span>' for n in qg_hits) }</span></li>
        <li>质量派（ROE>15%）单独用精度 15.2%——质量是正向但弱的信号，与十年组结论一致</li>
      </ul>
    </div>
    <div class="card">
      <h4>❌ 三个经典流派再次失败</h4>
      <ul>
        <li><b>便宜派（PE<20&PB<2）：精度 8.0%，低于随机</b>——负选股！起点便宜与二十年十倍负相关</li>
        <li>PEG<1（林奇派）：2006 时点仅 1 只达标（EPS 增速数据缺失+高增长股不便宜）</li>
        <li>小市值：精度与随机持平（2006 年市值普遍小，无区分度）</li>
      </ul>
    </div>
  </div>
</section>

<section>
  <h2><span class="no">柒</span>十年 vs 二十年：同一市场，两种十倍股逻辑</h2>
  <table>
    <tr><th style="width:24%">维度</th><th>十年组（2016→2026）</th><th>二十年组（2006→2026）</th></tr>
    {cmp_html}
  </table>
  <div class="concl">
    <b>统一框架下的解读</b>：十倍股概率随窗口拉长 16 倍，本质是「年化 13%」与「年化 26%」的复利差。十年十倍必须踩中单轮超级产业趋势（起点贵、行业集中、静态规则精度 2%）；二十年十倍只需「中上的质量 × 活下来 × 两三轮周期」（起点平庸、行业分散、规则精度 18% 但信号弱）。<b>共同的不变量只有一个：盈利增长（+21.5x / +22.3x）扛下全部涨幅，以及——估值便宜从来不是十倍股的起点特征。</b>
  </div>
</section>

<section>
  <h2><span class="no">捌</span>方法论更新：五层体系 → 六层体系（新增卖出纪律）</h2>
  <div class="grid2">
    <div class="card">
      <h4>识别端（怎么找到）</h4>
      <ul>
        <li><b>1. 质量门限做底仓</b>：ROE>10% & 增速>20% 宽筛——二十年口径精度 18%（1.46 倍随机），是唯一两个周期都验证有效的静态规则</li>
        <li><b>2. 放弃预测单只</b>：即便最强规则也漏 80%；用组合（30-50 只）承接 12-18% 的命中概率，赚赔率不赚胜率</li>
        <li><b>3. 产业趋势做放大器</b>：十年组证明单轮趋势股（起点贵）弹性最大；二十年组证明多轮叠加（家电→白酒→黄金→AI 硬件）才是常态——每年检视持仓是否踩在当轮趋势上</li>
      </ul>
    </div>
    <div class="card">
      <h4>持有与卖出端（怎么拿住）</h4>
      <ul>
        <li><b>4. 拿得住的前置条件是「等得起」</b>：中位等待 6-8 年、中途必有 -50% 级回撤；仓位设计必须让人扛得过 2008</li>
        <li><b>5. 卖出纪律 = 证伪而非回撤</b>：产业逻辑终结（万科式）或公司竞争力恶化才卖；单纯下跌/涨多了不是理由</li>
        <li><b>6. 泡沫兑现纪律</b>：2015 年批量制造 51 只十倍股、其中 2/3 后来还回去——牛市后段对「曾达 5x+」的持仓做部分止盈，是对幂律分布最现实的妥协</li>
      </ul>
    </div>
  </div>
  <div class="concl" style="margin-top:14px;">
    与前两份报告的衔接：价值因子体系（双门限 6.3%）负责防守与底仓；十倍股体系负责进攻端。<b>两者互补 = GARP + 趋势定位 + 卖出纪律</b>。
  </div>
</section>

<footer>
  数据来源：本地 PostgreSQL（tushare 行情/财务，2006-2026；已补齐 2010-2015 断档）｜ 脚本：scripts/tenbagger_20y.py ｜ 结果：outputs/tenbagger_20y.json<br>
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

mk('c_profile', {{
  title: {{text: '起点画像：20x股 vs 全市场（中位数）', left:'center', textStyle:{{fontSize:13}}}},
  tooltip: {{}},
  xAxis: Object.assign({{type:'category', data:{profile_labels}}}, AX),
  yAxis: Object.assign({{type:'value'}}, AX),
  series: [
    {{name:'二十年10x股', type:'bar', data:{profile_10x}, itemStyle:{{color:'#c0392b'}}, barGap:0}},
    {{name:'全市场', type:'bar', data:{profile_mkt}, itemStyle:{{color:'#8ea3bd'}}}}
  ],
  legend: {{bottom:0}}
}});

mk('c_pct', {{
  title: {{text: '起点市场分位（50=市场中位）', left:'center', textStyle:{{fontSize:13}}}},
  tooltip: {{}},
  radar: {{
    indicator: {pct_labels}.map(function(k){{return {{name:k, max:100}};}}),
    center:['50%','52%'], radius:'62%'
  }},
  series: [{{type:'radar', data:[{{value:{pct_values}, name:'20x股分位中位', areaStyle:{{opacity:0.35, color:'#2c5f8a'}}, lineStyle:{{color:'#2c5f8a'}}}}]}}]
}});

mk('c_ind', {{
  title: {{text: '行业分布 Top15（共56个行业）', left:'center', textStyle:{{fontSize:13}}}},
  tooltip: {{}},
  grid: {{left:90, right:20, top:30, bottom:24}},
  xAxis: Object.assign({{type:'value'}}, AX),
  yAxis: Object.assign({{type:'category', data:{ind_labels}.reverse()}}, AX),
  series: [{{type:'bar', data:{ind_values}.slice().reverse(), itemStyle:{{color:'#2c5f8a'}}, label:{{show:true, position:'right'}}}}]
}});

mk('c_fy', {{
  title: {{text: '首次达到10x的年份分布（牛市批量兑现）', left:'center', textStyle:{{fontSize:13}}}},
  tooltip: {{}},
  grid: {{left:36, right:16, top:30, bottom:24}},
  xAxis: Object.assign({{type:'category', data:{fy_labels}}}, AX),
  yAxis: Object.assign({{type:'value'}}, AX),
  series: [{{type:'bar', data:{fy_values}, itemStyle:{{color:function(p){{return p.dataIndex>=8&&p.dataIndex<=9?'#c0392b':'#b0894d';}}}},
    markLine:{{data:[{{yAxis:51, name:'2015牛市'}}], lineStyle:{{color:'#c0392b'}}, label:{{formatter:'2015牛市: 51只'}}}}}}]
}});

mk('c_rules', {{
  title: {{text: '识别规则精度 vs 随机基线（12.42%）', left:'center', textStyle:{{fontSize:13}}}},
  tooltip: {{}},
  grid: {{left:170, right:40, top:30, bottom:24}},
  xAxis: Object.assign({{type:'value', max:20}}, AX),
  yAxis: Object.assign({{type:'category', data:{rule_names}.reverse()}}, AX),
  series: [
    {{type:'bar', data:{rule_prec}.slice().reverse(),
      itemStyle:{{color:function(p){{return p.value>=12.42?'#c0392b':'#9aa4b8';}}}},
      label:{{show:true, position:'right', formatter:'{{c}}%'}}}},
    {{type:'bar', data:{rule_names}.map(function(){{return 12.42;}}), barWidth:2, itemStyle:{{color:'#1a7a4a'}}, silent:true, tooltip:{{show:false}}}}
  ]
}});
</script>
</body>
</html>
"""

out = "outputs/a股二十年十倍股全景解剖.html"
open(out, "w").write(HTML)
print("written", out, len(HTML), "chars")

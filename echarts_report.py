"""ECharts 回测报告渲染器 — 复刻 Argus React CandleChart 组件风格

红涨绿跌（中式）、三角买卖标记、MA/EMA指标、止盈止损线
"""
from __future__ import annotations

import json
from datetime import date

_CDN_ECHARTS = "https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js"
_MA_COLORS = ["#f5a623", "#2196f3", "#9c27b0", "#00bcd4", "#ff5722", "#4caf50"]
_EMA_COLORS = ["#e91e63", "#ff9800", "#009688", "#673ab7"]


def build_report_data(result, adapter, symbols: list[str], start: date, end: date):
    """把 BacktestResult 摊成 ECharts 可用的 JSON 数据"""
    from argus.strategy.report import build_report as argus_build

    def bars_provider(sym):
        return adapter.get_bar([sym], start=start, end=end)

    rep = argus_build(result, bars_provider=bars_provider, indicators={"ma": [5, 20]})

    candles = []
    for c in rep.get("candles", []):
        ind_data = {}
        ind = c.get("indicators", {})
        for k, v in ind.items():
            if k == "boll" and isinstance(v, dict):
                ind_data["boll"] = {sub: vals for sub, vals in v.items()}
            elif isinstance(v, list):
                ind_data[k] = v

        # 计算单标的收益曲线：逐日持仓 × 收盘价
        sym = c["symbol"]
        bars = c["bars"]
        # 从 trades 重建持仓变化
        sym_trades = [t for t in rep.get("trades", []) if t.get("symbol") == sym]
        # 建立日期索引
        date_index = {b["date"]: i for i, b in enumerate(bars)}
        shares = 0.0
        per_stock_eq = []
        buy_idx = min((date_index.get(t["date"], 99999) for t in sym_trades if t.get("side") == "buy"), default=0)
        last_eq = None
        for i, b in enumerate(bars):
            d = b["date"]
            for t in sym_trades:
                if t.get("date") == d:
                    if t["side"] == "buy":
                        shares += t.get("shares", 0)
                    else:
                        shares -= t.get("shares", 0)
            if shares > 0 and i >= buy_idx:
                last_eq = shares * b["close"]
            # 持仓为0时保持上一刻的净值（平线），而不是跌到0
            per_stock_eq.append(last_eq)

        # 合并交易详情到买卖标记
        sym_buys = []
        sym_sells = []
        for t in sym_trades:
            d = t.get("date", "")
            p = t.get("price", 0)
            sh = t.get("shares", 0)
            nt = t.get("notional", 0)
            detail = f'{p} × {sh}股 = {nt:,.0f}'
            (sym_buys if t.get("side") == "buy" else sym_sells).append(
                {"date": d, "price": p, "detail": detail})

        candles.append({
            "symbol": sym,
            "bars": bars,
            "buys": sym_buys,
            "sells": sym_sells,
            "stops": c.get("stops", []),
            "take_profits": c.get("take_profits", []),
            "indicators": ind_data,
            "per_equity": per_stock_eq,
        })

    eq = rep.get("equity", {})
    eq_dates = eq.get("dates", [])
    eq_vals = eq.get("equity", [])

    trades = rep.get("trades", [])
    buy_pts, sell_pts = [], []
    for t in trades:
        d = t.get("date", "")
        p = t.get("price", 0)
        if d in eq_dates:
            idx = eq_dates.index(d)
            ev = eq_vals[idx] if idx < len(eq_vals) else 0
            (buy_pts if t.get("side") == "buy" else sell_pts).append({"x": d, "y": ev})

    bench = rep.get("benchmark")
    bench_data = None
    if bench:
        bench_data = {"dates": bench.get("dates", []), "values": bench.get("equity", [])}

    m = rep.get("metrics", {})
    labels = [
        ("total_return", "总收益"), ("annual_return", "年化"), ("sharpe", "夏普"),
        ("max_drawdown", "最大回撤"), ("win_rate", "胜率"), ("days", "交易日"),
    ]
    kpis = []
    for k, lbl in labels:
        v = m.get(k)
        if v is not None:
            if k in ("days",): txt = str(int(v))
            elif k in ("sharpe",): txt = f"{v:.2f}"
            else: txt = f"{v * 100:.2f}%"
            kpis.append({"label": lbl, "value": txt})

    return {
        "strategy": rep.get("strategy", ""), "params": rep.get("params", {}),
        "period": rep.get("period", []), "kpis": kpis,
        "equity": {"dates": eq_dates, "values": eq_vals},
        "benchmark": bench_data, "buy_points": buy_pts, "sell_points": sell_pts,
        "candles": candles,
        "trades": [{"date": t.get("date",""), "symbol": t.get("symbol",""),
                     "side": t.get("side",""), "price": t.get("price",0),
                     "shares": t.get("shares",0), "notional": t.get("notional",0)}
                    for t in trades],
    }


def render_echarts_html(result, adapter, symbols: list[str], start: date, end: date,
                         benchmark: dict | None = None,
                         title: str = "回测报告") -> str:
    """生成 ECharts 自包含 HTML，复刻 Argus CandleChart 暗色主题"""
    data = build_report_data(result, adapter, symbols, start, end)

    # 添加基准数据（归一化到 1.0，与策略净值对齐）
    if benchmark and benchmark.get("dates") and benchmark.get("equity"):
        bv = benchmark["equity"]
        b0 = bv[0] if bv[0] and bv[0] != 0 else 1.0
        data["benchmark"] = {"dates": benchmark["dates"], "values": [v / b0 for v in bv]}

    kpi_html = "".join(
        f'<div class="kpi"><div class="kl">{k["label"]}</div><div class="kv">{k["value"]}</div></div>'
        for k in data["kpis"]
    )

    # 权益+回撤
    eq_divs = ""
    if data["equity"]["dates"]:
        eq_divs = (
            '<div class="card"><h3>📊 净值 vs 沪深300</h3><div id="equity" style="height:360px"></div></div>'
            '<div class="card"><h3>📉 回撤</h3><div id="dd" style="height:200px"></div></div>'
        )

    # 蜡烛图 — 每个标的：K线 + 买卖记录放一起
    trades_by_symbol: dict[str, list] = {}
    # 预建日期→净值的索引，用于算持仓比例
    eq_map = dict(zip(data["equity"]["dates"], data["equity"]["values"])) if data["equity"]["dates"] else {}
    for t in data["trades"]:
        trades_by_symbol.setdefault(t["symbol"], []).append(t)

    def _name(sym):
        """查股票名称"""
        try:
            import psycopg2
            c = psycopg2.connect(host="/tmp", dbname="investassist", user="james", connect_timeout=3)
            cur = c.cursor()
            cur.execute("SELECT name FROM stocks WHERE ts_code=%s", (sym,))
            r = cur.fetchone()
            cur.close(); c.close()
            return r[0] if r else sym
        except Exception:
            return sym

    kline_blocks = ""
    kline_js = ""
    for i, c in enumerate(data["candles"]):
        sym = c["symbol"]
        name = _name(sym)
        sym_trades = trades_by_symbol.get(sym, [])
        did = f"kline-{i}"
        eid = f"seq-{i}"
        tid = f"trades-{i}"

        # 买卖记录表
        rows = ""
        for t in sym_trades:
            d = t["date"]
            eq_val = eq_map.get(d, 1)
            pos_pct = f'{t["notional"] / eq_val * 100:.1f}%' if eq_val and eq_val > 0 else '-'
            rows += (
                f'<tr><td>{d}</td>'
                f'<td class="{"buy" if t["side"] == "buy" else "sell"}">{"买入" if t["side"] == "buy" else "卖出"}</td>'
                f'<td>{t["price"]}</td><td>{t["shares"]}</td><td>{pos_pct}</td><td>{t["notional"]}</td></tr>'
            )
        buy_t = sum(t["notional"] for t in sym_trades if t["side"] == "buy")
        sell_t = sum(t["notional"] for t in sym_trades if t["side"] == "sell")
        pnl = sell_t - buy_t
        pnl_class = "buy" if pnl > 0 else "sell" if pnl < 0 else ""

        kline_blocks += (
            f'<div class="card stock-block">'
            f'<h3 class="stock-header">📈 {sym}'
            f'<span class="stock-name">（{name}）</span>'
            f'<span class="trade-summary {pnl_class}"> · 盈亏 {pnl:+,.0f} · {len(sym_trades)} 笔</span>'
            f'<span class="toggle-btn" onclick="toggleTrade(\'{tid}\',this)">▼</span></h3>'
            f'<div id="{did}" style="height:380px"></div>'
            f'<div id="{eid}" style="height:160px;margin-top:8px"></div>'
            f'<div id="{tid}" class="trade-detail">'
            f'<table><thead><tr><th>日期</th><th>方向</th><th>价格</th><th>股数</th><th>仓位</th><th>金额</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div></div>'
        )
        kline_js += f'_drawKline("{did}", {json.dumps(c, ensure_ascii=False)});\n'
        kline_js += f'_drawSeq("{eid}", {json.dumps({"dates": [b["date"] for b in c["bars"]], "values": c["per_equity"]})});\n'

    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>{title} · {data['strategy']}</title>
<script src="{_CDN_ECHARTS}"></script>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,system-ui,sans-serif;background:#1a1a2e;color:#e0e0e0}}
.wrap{{max-width:1100px;margin:0 auto;padding:24px}}
h1{{font-size:20px;margin-bottom:4px}} .sub{{color:#888;font-size:13px;margin-bottom:20px}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:10px;margin-bottom:20px}}
.kpi{{background:#16213e;border:1px solid #2a2a4a;border-radius:8px;padding:10px;text-align:center}}
.kl{{font-size:11px;color:#666}} .kv{{font-size:17px;font-weight:700;margin-top:4px}}
.card{{background:#16213e;border:1px solid #2a2a4a;border-radius:8px;padding:16px;margin-bottom:16px}}
.card h3{{font-size:14px;margin-bottom:8px;color:#ccc}}
.section-title{{font-size:16px;color:#888;border-bottom:1px solid #2a2a4a;padding-bottom:8px;margin:24px 0 16px;font-weight:600}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th,td{{padding:6px 8px;border-bottom:1px solid #2a2a4a;text-align:right}}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){{text-align:left}}
.buy{{color:#e83535;font-weight:700}} .sell{{color:#1aaa55;font-weight:700}}
.stock-header{{display:flex;align-items:center;gap:8px;cursor:pointer;user-select:none}}
.stock-name{{color:#888;font-size:12px;font-weight:400}}
.trade-summary{{font-size:12px;margin-left:auto}}
.toggle-btn{{font-size:14px;transition:transform 0.2s;color:#666}}
.toggle-btn.open{{transform:rotate(180deg)}}
.trade-detail{{display:block;margin-top:12px}}
.trade-detail.hidden{{display:none}}
</style></head><body><div class="wrap">
<h1>回测报告 · {data['strategy']} {json.dumps(data['params'], ensure_ascii=False)}</h1>
<div class="sub">{data['period'][0]} ~ {data['period'][1]} · {len(data['trades'])} 笔交易</div>

<h2 class="section-title">一、总收益概况</h2>
<div class="kpis">{kpi_html}</div>
{eq_divs}

<h2 class="section-title">二、分标的明细</h2>
{kline_blocks}
<script>
var _MA = {json.dumps(_MA_COLORS)};
var _EMA = {json.dumps(_EMA_COLORS)};
function _drawKline(id, c) {{
    var d = c.bars.map(b=>b.date);
    var o = c.bars.map(b=>[b.open,b.close,b.low,b.high]);
    var ch = echarts.init(document.getElementById(id),null,{{renderer:'canvas'}});
    var s = [{{type:'candlestick',data:o,itemStyle:{{color:'#e8413c',color0:'#3aa856',borderColor:'#e8413c',borderColor0:'#3aa856'}}}}];
    var ind = c.indicators||{{}}, mi=0, ei=0;
    for(var k in ind) {{
        if(k==='boll'||!Array.isArray(ind[k]))continue;
        var cl = k.startsWith('ma')?_MA[mi++%_MA.length]:_EMA[ei++%_EMA.length];
        s.push({{name:k.toUpperCase(),type:'line',data:ind[k].map((v,i)=>[d[i],v]),showSymbol:false,lineStyle:{{color:cl,width:1.4}},z:6}});
    }}
    var boll = ind.boll;
    if(boll&&boll.mid)s.push({{name:'BOLL-MID',type:'line',data:boll.mid.map((v,i)=>[d[i],v]),showSymbol:false,lineStyle:{{color:'#ff9800',width:1.5}},z:6}});
    if(boll&&boll.upper)s.push({{name:'BOLL-UPPER',type:'line',data:boll.upper.map((v,i)=>[d[i],v]),showSymbol:false,lineStyle:{{color:'#9e9e9e',width:1,type:'dotted'}},z:5}});
    if(boll&&boll.lower)s.push({{name:'BOLL-LOWER',type:'line',data:boll.lower.map((v,i)=>[d[i],v]),showSymbol:false,lineStyle:{{color:'#9e9e9e',width:1,type:'dotted'}},z:5}});
    if(c.buys&&c.buys.length)s.push({{name:'买入',type:'scatter',data:c.buys.map(p=>[p.date,p.price,p.detail]),symbol:'triangle',symbolSize:20,itemStyle:{{color:'#e83535',borderColor:'#fff',borderWidth:2}},label:{{show:true,position:'top',formatter:'买',color:'#fff',fontSize:9,fontWeight:700,textBorderColor:'#e83535',textBorderWidth:1.5}},z:10}});
    if(c.sells&&c.sells.length)s.push({{name:'卖出',type:'scatter',data:c.sells.map(p=>[p.date,p.price,p.detail]),symbol:'triangle',symbolSize:20,symbolRotate:180,itemStyle:{{color:'#1aaa55',borderColor:'#fff',borderWidth:2}},label:{{show:true,position:'bottom',formatter:'卖',color:'#fff',fontSize:9,fontWeight:700,textBorderColor:'#1aaa55',textBorderWidth:1.5}},z:10}});
    ch.setOption({{animation:false,backgroundColor:'transparent',tooltip:{{trigger:'axis',axisPointer:{{type:'cross'}},formatter:function(params){{var t='';for(var i=0;i<params.length;i++){{var p=params[i];if(p.seriesName==='买入'||p.seriesName==='卖出'){{t+=p.marker+p.seriesName+': '+p.value[2]+'<br/>'}}else if(p.seriesType==='candlestick'){{t+=p.marker+p.seriesName+'<br/>开 '+p.value[1]+' 收 '+p.value[2]+' 低 '+p.value[3]+' 高 '+p.value[4]+'<br/>'}}else{{t+=p.marker+p.seriesName+': '+p.value[1]+'<br/>'}}}}return t||''}}}},grid:{{left:56,right:90,top:16,bottom:28}},xAxis:{{type:'category',data:d,boundaryGap:true,axisLabel:{{fontSize:10,color:'#999'}},axisLine:{{show:false}},axisTick:{{show:false}},splitLine:{{show:false}}}},yAxis:{{scale:true,axisLabel:{{fontSize:10,color:'#999'}},splitLine:{{lineStyle:{{color:'#2a2a4a'}}}}}},dataZoom:[{{type:'inside'}}],series:s}},true);
}}
function _drawEquity(id, eq, bench, buys, sells) {{
    var ch = echarts.init(document.getElementById(id),null,{{renderer:'canvas'}});
    var s = [{{name:'策略净值',type:'line',data:eq.values,showSymbol:false,lineStyle:{{color:'#5470c6',width:2}},areaStyle:{{color:'rgba(84,112,198,0.1)'}},z:5}}];
    if(bench&&bench.values)s.push({{name:'沪深300',type:'line',data:bench.values,showSymbol:false,lineStyle:{{color:'#999',width:1,type:'dashed'}},z:4}});
    if(buys&&buys.length)s.push({{type:'scatter',data:buys.map(p=>[p.x,p.y]),symbol:'triangle',symbolSize:10,itemStyle:{{color:'#e83535'}},z:10}});
    if(sells&&sells.length)s.push({{type:'scatter',data:sells.map(p=>[p.x,p.y]),symbol:'triangle',symbolSize:10,symbolRotate:180,itemStyle:{{color:'#1aaa55'}},z:10}});
    ch.setOption({{tooltip:{{trigger:'axis'}},grid:{{left:60,right:30,top:12,bottom:28}},xAxis:{{type:'category',data:eq.dates,axisLabel:{{fontSize:10,color:'#999'}}}},yAxis:{{scale:true,axisLabel:{{fontSize:10,color:'#999'}},splitLine:{{lineStyle:{{color:'#2a2a4a'}}}}}},dataZoom:[{{type:'inside'}}],series:s}});
}}
function _drawDD(id, dates, vals) {{
    var ch=echarts.init(document.getElementById(id),null,{{renderer:'canvas'}}),peak=0,dd=[];
    for(var i=0;i<vals.length;i++){{peak=Math.max(peak,vals[i]);dd.push(peak>0?(vals[i]/peak-1)*100:0)}}
    ch.setOption({{tooltip:{{trigger:'axis',valueFormatter:function(v){{return v.toFixed(2)+'%'}}}},grid:{{left:60,right:30,top:12,bottom:28}},xAxis:{{type:'category',data:dates,axisLabel:{{fontSize:10,color:'#999'}}}},yAxis:{{axisLabel:{{formatter:'{{value}}%',color:'#999'}},splitLine:{{lineStyle:{{color:'#2a2a4a'}}}}}},dataZoom:[{{type:'inside'}}],series:[{{type:'line',data:dd,showSymbol:false,areaStyle:{{color:'rgba(232,53,53,0.15)'}},lineStyle:{{color:'#e83535',width:1.5}}}}]}});
}}
function init() {{
    {kline_js}
    _drawEquity("equity", {json.dumps(data["equity"], ensure_ascii=False)}, {json.dumps(data.get("benchmark") or "null", ensure_ascii=False)}, {json.dumps(data["buy_points"], ensure_ascii=False)}, {json.dumps(data["sell_points"], ensure_ascii=False)});
    _drawDD("dd", {json.dumps(data["equity"]["dates"], ensure_ascii=False)}, {json.dumps(data["equity"]["values"], ensure_ascii=False)});
}}
document.addEventListener('DOMContentLoaded', init);
function toggleTrade(id, btn) {{
    var el = document.getElementById(id);
    el.classList.toggle('hidden');
    btn.classList.toggle('open');
}}
function _drawSeq(id, data) {{
    var ch=echarts.init(document.getElementById(id),null,{{renderer:'canvas'}});
    var vals = data.values.filter(function(v){{return v!==null}});
    var ret = vals.length>1 ? (vals[vals.length-1]/vals[0]-1)*100 : 0;
    ch.setOption({{
        title:{{text:'持仓收益 '+ret.toFixed(1)+'%',textStyle:{{fontSize:11,color:'#888'}},left:8,top:4}},
        grid:{{left:12,right:12,top:28,bottom:12}},
        xAxis:{{type:'category',data:data.dates,show:false}},
        yAxis:{{show:false,scale:true}},
        series:[{{type:'line',data:data.values,showSymbol:false,connectNulls:true,
            lineStyle:{{color:ret>=0?'#1aaa55':'#e83535',width:1.5}},
            areaStyle:{{color:ret>=0?'rgba(26,170,85,0.1)':'rgba(232,53,53,0.1)'}}}}]
    }},true);
}}
window.addEventListener('resize', function(){{document.querySelectorAll('[id^="kline-"],[id^="seq-"],[id="equity"],[id="dd"]').forEach(function(el){{var i=echarts.getInstanceByDom(el);if(i)i.resize();}})}});
</script></div></body></html>"""
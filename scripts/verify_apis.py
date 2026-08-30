#!/usr/bin/env python3
"""接口权限验证 — 修复方案附录清单（写码前一次性跑）"""
import sys, traceback

results = []

def check(name, fn):
    try:
        info = fn()
        results.append((name, "OK", info))
        print(f"  ✅ {name}: {info}")
    except Exception as e:
        results.append((name, "FAIL", str(e)[:200]))
        print(f"  ❌ {name}: {str(e)[:200]}")

print("=== Tushare 接口 ===")
import tushare as ts
TOKEN = "72826744b6a3733e61cd602f4fd42fe56a6de0d5781ba77e0bfb929b"
pro = ts.pro_api(TOKEN)

def t_hk_daily():
    df = pro.hk_daily(trade_date="20260828")
    return f"{len(df)} 行, 列: {list(df.columns)[:8]}"
def t_fund_daily():
    df = pro.fund_daily(trade_date="20260828")
    return f"{len(df)} 行, 列: {list(df.columns)[:8]}"
def t_index_daily():
    df = pro.index_daily(trade_date="20260828")
    return f"{len(df)} 行, 列: {list(df.columns)[:8]}"
def t_fina_indicator():
    df = pro.fina_indicator_vip(period="20260630") if hasattr(pro, "fina_indicator_vip") else pro.fina_indicator(period="20260630")
    return f"{len(df)} 行, 列: {list(df.columns)[:8]}"

check("pro.hk_daily", t_hk_daily)
check("pro.fund_daily", t_fund_daily)
check("pro.index_daily", t_index_daily)
check("pro.fina_indicator", t_fina_indicator)

print("\n=== AKShare 宏观接口 ===")
import akshare as ak

def a_shibor():
    df = ak.macro_china_shibor_all()
    return f"{len(df)} 行, 列: {list(df.columns)[:6]}"
def a_bond():
    df = ak.bond_zh_us_rate(start_date="20260501")
    return f"{len(df)} 行, 列: {list(df.columns)[:6]}, 末行日期 {df.iloc[-1, 0] if len(df) else 'N/A'}"
def a_lpr():
    df = ak.macro_china_lpr()
    return f"{len(df)} 行, 列: {list(df.columns)[:6]}, 末行 TRADE_DATE {str(df.iloc[-1].get('TRADE_DATE', ''))[:10]}"
def a_cpi():
    df = ak.macro_china_cpi_monthly()
    return f"{len(df)} 行, 列: {list(df.columns)[:6]}"
def a_pmi():
    df = ak.macro_china_pmi()
    return f"{len(df)} 行, 列: {list(df.columns)[:6]}"
def a_money():
    df = ak.macro_china_money_supply()
    return f"{len(df)} 行, 列: {list(df.columns)[:6]}"

check("macro_china_shibor_all", a_shibor)
check("bond_zh_us_rate", a_bond)
check("macro_china_lpr", a_lpr)
check("macro_china_cpi_monthly", a_cpi)
check("macro_china_pmi", a_pmi)
check("macro_china_money_supply", a_money)

print("\n=== AKShare 港股/全球指数接口 ===")
def a_hk_daily():
    df = ak.stock_hk_daily(symbol="00700", start_date="20260525", end_date="20260605", adjust="")
    return f"{len(df)} 行, 列: {list(df.columns)}"
def a_index_global():
    df = ak.index_global(symbol="道琼斯")
    return f"{len(df)} 行, 列: {list(df.columns)[:6]}"

check("stock_hk_daily(00700, 不复权)", a_hk_daily)
check("index_global(道琼斯)", a_index_global)

print("\n=== 港股复权口径对比 (D4) ===")
def hk_price_compare():
    import csv
    # CSV 里 00700.HK 6/3 附近的 close
    rows = {}
    with open("/Users/james/workspace/stock_data/daily/00700.HK.csv") as f:
        for r in csv.DictReader(f):
            if "2026-05-25" <= r["datetime"] <= "2026-06-03":
                rows[r["datetime"]] = float(r["close"])
    df = ak.stock_hk_daily(symbol="00700", start_date="20260525", end_date="20260603", adjust="")
    ts_rows = {str(d.date()): float(c) for d, c in zip(df["date"], df["close"])}
    diffs = []
    for d in sorted(set(rows) & set(ts_rows)):
        diff = abs(rows[d] - ts_rows[d]) / rows[d]
        diffs.append((d, rows[d], ts_rows[d], diff))
    max_diff = max(x[3] for x in diffs) if diffs else None
    return f"重叠 {len(diffs)} 天, 最大偏差 {max_diff:.4%}, 样例 {diffs[:3]}"

check("00700.HK CSV vs akshare(不复权)", hk_price_compare)

fails = [r for r in results if r[1] == "FAIL"]
print(f"\n=== 总结: {len(results)-len(fails)}/{len(results)} 通过 ===")
sys.exit(1 if fails else 0)

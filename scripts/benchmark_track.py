#!/usr/bin/env python3
"""
月度基准对照监控 (P0#4)
========================
组合 vs 中证1000 (000852.SH) 的月度/累计超额 + 连续跑输报警。

- 数据源: portfolio_nav (组合净值, 由 build_nav.py 每日重建) + index_daily (中证1000)
- 口径: 组合与基准的日收益均对齐到组合交易日序列, 首日(首次调仓日)收益双方都纳入,
        确保月度超额与累计超额严格一致
- 判定: 连续 >= 3 个**完整月**组合月收益跑输中证1000 → 飞书红色告警 (伪 α 报警器)
- 当前月(进行中)只展示不计入连续判定; 数据不足时不误报

用法:
    python3 scripts/benchmark_track.py                 # 全部已建净值策略, stdout + 报告
    python3 scripts/benchmark_track.py --push          # + 飞书推送 (跑输≥3月走红色告警)
    python3 scripts/benchmark_track.py --strategy prod_6f_eq prod_lgbm_neu
"""
import argparse
import os
import sys

import numpy as np
import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
sys.path.insert(0, REPO)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(REPO, ".env"))
except ImportError:
    pass

import factor_eval as fe  # noqa: E402  (get_conn)

BENCH_SYMBOL = "000852.SH"   # 中证1000
BENCH_NAME = "中证1000"
REF_SYMBOL = "000300.SH"     # 沪深300 (参考)
ALERT_MONTHS = 3             # 连续跑输月数阈值


def pct(v):
    return "-" if v is None or (isinstance(v, float) and not np.isfinite(v)) else f"{v*100:+.2f}%"


def load_nav(conn, strategy_id) -> pd.DataFrame | None:
    cur = conn.cursor()
    cur.execute("SELECT trade_date, nav, daily_ret FROM portfolio_nav "
                "WHERE strategy_id=%s ORDER BY trade_date", (strategy_id,))
    rows = cur.fetchall()
    cur.close()
    if len(rows) < 2:
        return None
    df = pd.DataFrame(rows, columns=["trade_date", "nav", "daily_ret"])
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df.set_index("trade_date")


def load_index_ret(conn, symbol, start) -> pd.Series:
    """index_daily pct_chg → 日收益序列 (首日即当日 pct_chg, 与组合日收益同口径)."""
    cur = conn.cursor()
    cur.execute("SELECT trade_date, pct_chg FROM index_daily WHERE symbol=%s AND trade_date>=%s",
                (symbol, start.date()))
    df = pd.DataFrame(cur.fetchall(), columns=["trade_date", "pct_chg"])
    cur.close()
    if df.empty:
        return pd.Series(dtype=float)
    df["pct_chg"] = pd.to_numeric(df["pct_chg"], errors="coerce") / 100.0
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df.set_index("trade_date")["pct_chg"].sort_index().dropna()


def monthly_series(daily_ret: pd.Series) -> pd.Series:
    """月收益序列 (index=月末, value=该月复利收益)."""
    out = daily_ret.groupby(daily_ret.index.to_period("M")).apply(lambda x: (1 + x).prod() - 1)
    out.index = out.index.to_timestamp("M")
    return out


def detect_underperform(port_monthly: pd.Series, bench_monthly: pd.Series) -> tuple[int, list]:
    """从最近完整月往回数, 连续跑输月数。当前月(最后一个月)若尚未完结则跳过。"""
    common = port_monthly.index.intersection(bench_monthly.index).sort_values()
    if len(common) < 2:
        return 0, []
    today = pd.Timestamp.today()
    # 最后一个月若是当前自然月(进行中), 不参与判定
    judge_idx = common[:-1] if common[-1] >= today.to_period("M").to_timestamp("M") else common
    streak, worst = 0, []
    for m in judge_idx[::-1]:
        ex = port_monthly[m] - bench_monthly[m]
        if ex < 0:
            streak += 1
            worst.append((m, ex))
        else:
            break
    return streak, worst


def analyze(conn, strategy_id, name) -> dict:
    nav = load_nav(conn, strategy_id)
    if nav is None:
        return {"strategy_id": strategy_id, "name": name, "empty": True}
    port_ret = nav["daily_ret"]
    port_nav = nav["nav"]

    bench_ret = load_index_ret(conn, BENCH_SYMBOL, nav.index[0]).reindex(nav.index).fillna(0)
    ref_ret = load_index_ret(conn, REF_SYMBOL, nav.index[0]).reindex(nav.index).fillna(0)
    bench_nav = (1 + bench_ret).cumprod()
    ref_nav = (1 + ref_ret).cumprod()

    port_m = monthly_series(port_ret)
    bench_m = monthly_series(bench_ret)
    ref_m = monthly_series(ref_ret)

    cum_excess_1000 = port_nav.iloc[-1] - bench_nav.iloc[-1]
    cum_excess_300 = port_nav.iloc[-1] - ref_nav.iloc[-1]
    streak, worst = detect_underperform(port_m, bench_m)

    years = port_ret.groupby(port_ret.index.year).apply(lambda x: (1 + x).prod() - 1)
    bench_y = bench_m.groupby(bench_m.index.year).apply(lambda x: (1 + x).prod() - 1)

    return {
        "strategy_id": strategy_id, "name": name, "empty": False,
        "since": nav.index[0].date(), "days": len(nav),
        "port_nav": port_nav, "bench_nav": bench_nav, "ref_nav": ref_nav,
        "port_m": port_m, "bench_m": bench_m, "ref_m": ref_m,
        "cum_excess_1000": cum_excess_1000, "cum_excess_300": cum_excess_300,
        "streak": streak, "worst": worst, "years": years, "bench_y": bench_y,
    }


def render(r: dict) -> str:
    if r.get("empty"):
        return f"### 📊 基准对照 · {r['name']}\n\n⚠️ 无净值 (portfolio_nav 空, 请先跑 build_nav)"
    L = [f"### 📊 基准对照 · {r['name']}  (自 {r['since']}, {r['days']} 交易日)\n"]
    L.append(f"- 累计超额 vs **{BENCH_NAME}**: {pct(r['cum_excess_1000'])}"
             + f" | vs 沪深300: {pct(r['cum_excess_300'])}")

    idx = r["port_m"].index.union(r["bench_m"].index).sort_values()
    L.append(f"\n**月度收益 vs {BENCH_NAME}**\n")
    L.append("| 月份 | 组合 | 中证1000 | 月超额 |")
    L.append("|---|---|---|---|")
    for m in idx[-12:]:
        pv = r["port_m"].get(m, np.nan)
        bv = r["bench_m"].get(m, np.nan)
        L.append(f"| {m.strftime('%Y-%m')} | {pct(pv)} | {pct(bv)} | {pct(pv-bv)} |")

    if r["streak"] >= ALERT_MONTHS:
        L.append(f"\n🔴 **连续 {r['streak']} 月跑输 {BENCH_NAME}, 触发复盘!** "
                 + " | ".join(f"{m.strftime('%Y-%m')}({e*100:+.1f}pp)" for m, e in r["worst"]))
    elif r["streak"] > 0:
        L.append(f"\n🟡 连续 {r['streak']} 月跑输 {BENCH_NAME} (未达 {ALERT_MONTHS} 月阈值) "
                 + " | ".join(f"{m.strftime('%Y-%m')}({e*100:+.1f}pp)" for m, e in r["worst"]))
    else:
        L.append(f"\n✅ 最近无连续跑输 {BENCH_NAME}")

    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", action="append", default=None, help="可重复; 默认全部已建净值策略")
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    conn = fe.get_conn()
    cur = conn.cursor()
    if args.strategy:
        sids = args.strategy
    else:
        cur.execute("SELECT DISTINCT strategy_id FROM portfolio_nav ORDER BY 1")
        sids = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT strategy_id, name FROM strategy_config")
    names = dict(cur.fetchall())
    cur.close()

    results, alerts = [], []
    for sid in sids:
        r = analyze(conn, sid, names.get(sid, sid))
        results.append(r)
        if not r.get("empty") and r["streak"] >= ALERT_MONTHS:
            alerts.append(r)

    text = "\n\n".join(render(r) for r in results)
    print(text)

    out_path = args.out or os.path.join(REPO, "reports", "benchmark_track.md")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w") as f:
        f.write(text + "\n")

    if args.push:
        try:
            from report_builder import _push_feishu_card
            if alerts:
                al = "\n\n".join(render(r) for r in alerts)
                ok = _push_feishu_card(al, f"🔴 基准对照告警 · 连续{ALERT_MONTHS}月跑输", "red")
                print(f"\n[Feishu] 告警推送: {'✅ 成功' if ok else '❌ 失败'}")
            else:
                print("\n[Feishu] 无连续跑输报警, 静默不推送")
        except Exception as e:
            print(f"\n[Feishu] 推送失败: {e}")

    conn.close()


if __name__ == "__main__":
    main()

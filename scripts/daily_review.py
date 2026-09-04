#!/usr/bin/env python3
"""
每日复盘 (六子系统 P1)
======================
每个交易日收盘后跑: 持仓归因 + 因子健康度 + 信号摘要 → 报告 + 飞书。

内容:
  1. 持仓当日表现: position 加权日收益 vs 沪深300 (超额)
  2. 近 20 日回看: 当前持仓静态持有的累计收益 vs 基准 + 个股贡献 top/bottom
  3. 因子健康度: 6 因子回顾 IC (最新截面值 vs 近20日实现收益的 Spearman)
  4. 信号摘要: pending/executed 计数 + 最近调仓信息
  5. 拥挤度: 引用 crowding_monitor.md (pipeline 每日已跑)

用法:
    python3 scripts/daily_review.py             # 输出到 stdout + reports/
    python3 scripts/daily_review.py --push      # + 飞书推送
"""
import argparse
import os
import sys
from datetime import date

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

import factor_eval as fe


# ---------------------------------------------------------------- 数据
def load_positions(conn, strategy_id):
    cur = conn.cursor()
    cur.execute("SELECT ts_code, weight, entry_date FROM position "
                "WHERE strategy_id=%s ORDER BY weight DESC", (strategy_id,))
    rows = cur.fetchall()
    cur.close()
    if not rows:
        return None, None
    pos = pd.DataFrame(rows, columns=["ts_code", "weight", "entry_date"])
    return pos, date.today()


def load_recent_returns(conn, ts_codes, days=20):
    """近 N 交易日日收益宽表 (含当日)."""
    cur = conn.cursor()
    ph = ",".join(["%s"] * len(ts_codes))
    cur.execute(f"SELECT trade_date, ts_code, pct_chg FROM daily_quote "
                f"WHERE ts_code IN ({ph}) AND trade_date >= (CURRENT_DATE - %s)",
                (*ts_codes, days + 15))
    df = pd.DataFrame(cur.fetchall(), columns=["trade_date", "ts_code", "pct_chg"])
    cur.close()
    if df.empty:
        return pd.DataFrame()
    df["pct_chg"] = pd.to_numeric(df["pct_chg"], errors="coerce") / 100.0
    wide = df.pivot(index="trade_date", columns="ts_code", values="pct_chg").sort_index()
    return wide.tail(days + 1)


def load_benchmark(conn, days=20):
    cur = conn.cursor()
    cur.execute("SELECT trade_date, pct_chg FROM index_daily WHERE symbol='000300.SH' "
                "AND trade_date >= (CURRENT_DATE - %s) ORDER BY trade_date", (days + 15,))
    rows = cur.fetchall()
    cur.close()
    s = pd.Series({r[0]: float(r[1]) / 100.0 if r[1] is not None else np.nan for r in rows}).sort_index()
    return s.tail(days + 1)


def lookback_ic(conn, factor_names, days=20):
    """回顾 IC: ~days 交易日前的因子截面值 vs 其后 days 日实现收益的 Spearman (因子当下是否在工作)."""
    cur = conn.cursor()
    ph = ",".join(["%s"] * len(factor_names))
    # 截面取 ~days 交易日前 (自然日 *1.5 余量), 保证其后有完整 days 日收益
    cur.execute(f"SELECT MAX(trade_date) FROM factor_value "
                f"WHERE factor_name IN ({ph}) AND trade_date <= CURRENT_DATE - %s",
                (*factor_names, int(days * 1.5)))
    d = cur.fetchone()[0]
    if d is None:
        return {}, None
    cur.execute(f"SELECT factor_name, ts_code, value FROM factor_value "
                f"WHERE trade_date=%s AND factor_name IN ({ph})", (d,) + tuple(factor_names))
    cross = pd.DataFrame(cur.fetchall(), columns=["factor_name", "ts_code", "value"])
    cur.close()
    # 近 N 日实现收益
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT trade_date FROM daily_quote WHERE trade_date > %s "
                "ORDER BY trade_date DESC LIMIT %s", (d, days))
    dates = [r[0] for r in cur.fetchall()][::-1]
    cur.close()
    if not dates:
        return {}, d
    cur = conn.cursor()
    cur.execute("SELECT ts_code, trade_date, pct_chg FROM daily_quote WHERE trade_date = ANY(%s)",
                (dates,))
    df = pd.DataFrame(cur.fetchall(), columns=["ts_code", "trade_date", "pct_chg"])
    cur.close()
    df["pct_chg"] = pd.to_numeric(df["pct_chg"], errors="coerce") / 100.0
    fwd = df.groupby("ts_code")["pct_chg"].apply(lambda x: (1 + x).prod() - 1)
    ics = {}
    for f in factor_names:
        c = cross[cross["factor_name"] == f].set_index("ts_code")["value"]
        common = c.dropna().index.intersection(fwd.dropna().index)
        if len(common) < 50:
            ics[f] = np.nan
            continue
        ics[f] = c[common].rank().corr(fwd[common].rank())  # Spearman
    return ics, d


def signal_summary(conn, strategy_id):
    cur = conn.cursor()
    cur.execute("SELECT status, COUNT(*) FROM signal_log WHERE strategy_id=%s GROUP BY 1", (strategy_id,))
    by_status = dict(cur.fetchall())
    cur.execute("SELECT MAX(trade_date) FROM signal_log WHERE strategy_id=%s", (strategy_id,))
    last = cur.fetchone()[0]
    cur.close()
    return by_status, last


# ---------------------------------------------------------------- 报告
def render(pos, rets, bench, ics, factor_date, by_status, last_signal, strategy_name) -> str:
    L = [f"### 📈 每日复盘 · {strategy_name}\n"]

    # 1. 当日
    if rets is not None and len(rets) and pos is not None:
        last_d = rets.index[-1]
        day_ret = (rets.iloc[-1] * pos.set_index("ts_code")["weight"]).sum()
        bench_d = bench.iloc[-1] if len(bench) else np.nan
        excess = day_ret - bench_d if not np.isnan(bench_d) else np.nan
        L.append(f"**持仓日收益** ({pd.Timestamp(last_d).date()}): "
                 f"{'🔴' if day_ret >= 0 else '🟢'}{day_ret:+.2%} | "
                 f"沪深300 {bench_d:+.2%} | 超额 **{excess:+.2%}**\n")

        # 2. 近 20 日
        cum = (1 + rets).prod() - 1
        port_cum = float((pos.set_index("ts_code")["weight"] * cum.reindex(pos.ts_code).fillna(0)).sum())
        bench_cum = float((1 + bench).prod() - 1)
        L.append(f"**近 {len(rets)} 交易日**: 组合 {port_cum:+.1%} | 基准 {bench_cum:+.1%} | "
                 f"超额 {port_cum - bench_cum:+.1%}")

        # 3. 个股贡献
        contrib = (pos.set_index("ts_code")["weight"] * cum.reindex(pos.ts_code).fillna(0)).sort_values()
        if len(contrib):
            top = contrib.tail(5).sort_values(ascending=False)
            bot = contrib.head(5)
            L.append("\n**贡献 Top5**: " + ", ".join(f"{k} {v:+.1%}" for k, v in top.items()))
            L.append("**拖累 Top5**: " + ", ".join(f"{k} {v:+.1%}" for k, v in bot.items()))

    # 4. 因子健康度
    if ics:
        L.append(f"\n**因子回顾 IC** (截面 {factor_date} vs 近20日收益):")
        flags = []
        for f, v in ics.items():
            if np.isnan(v):
                continue
            emoji = "🟢" if v > 0.03 else ("🟡" if v > 0 else "🔴")
            flags.append(f"{f} {emoji}{v:+.2f}")
        L.append("  ".join(flags))

    # 5. 信号摘要
    st = " / ".join(f"{k} {v}" for k, v in sorted(by_status.items())) if by_status else "无"
    L.append(f"\n**信号**: {st} | 最近调仓: {last_signal}")
    L.append("\n*拥挤度详见 crowding_monitor.md (pipeline 每日更新)*")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="prod_6f_eq")
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    conn = fe.get_conn()
    cur = conn.cursor()
    cur.execute("SELECT name, config FROM strategy_config WHERE strategy_id=%s", (args.strategy,))
    row = cur.fetchone()
    if not row:
        raise SystemExit(f"策略不存在: {args.strategy}")
    name, cfg = row
    cur.close()

    pos, _ = load_positions(conn, args.strategy)
    if pos is None:
        text = f"### 📈 每日复盘 · {name}\n\n⚠️ 无持仓 (position 为空) — 首次调仓信号执行后开始复盘"
        print(text)
    else:
        rets = load_recent_returns(conn, pos["ts_code"].tolist())
        bench = load_benchmark(conn)
        ics, fdate = lookback_ic(conn, list(cfg["factors"].keys()))
        by_status, last_sig = signal_summary(conn, args.strategy)
        text = render(pos, rets, bench, ics, fdate, by_status, last_sig, name)
        print(text)

        if args.out:
            os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
            with open(args.out, "w") as f:
                f.write(text + "\n")

    if args.push:
        try:
            from report_builder import _push_feishu_card
            ok = _push_feishu_card(text, f"📈 每日复盘 · {name}", "blue")
            print(f"\n[Feishu] 推送: {'✅ 成功' if ok else '❌ 失败'}")
        except Exception as e:
            print(f"\n[Feishu] 推送失败: {e}")

    conn.close()


if __name__ == "__main__":
    main()

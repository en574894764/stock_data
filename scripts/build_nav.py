#!/usr/bin/env python3
"""
组合净值曲线持久化 (P2)
========================
从 trade_log + daily_quote 全量重建组合每日净值 → portfolio_nav 表。
- 买入日收益按 close/open (模拟开盘成交), 卖出日贡献 0 (开盘现金化), 持有日 pct_chg
- 调仓日扣单边换手成本 (strategy_config.cost_one_side)
- 每次全量重算 (数据量小, 无增量状态)

daily_review 自动引用 since inception 绩效。

用法:
    python3 scripts/build_nav.py                    # 重建所有策略
    python3 scripts/build_nav.py --strategy prod_6f_eq
"""
import argparse
import os
import sys

import numpy as np
import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
sys.path.insert(0, REPO)

import factor_eval as fe

COST_DEFAULT = 0.0015


def load_trades(conn, strategy_id):
    cur = conn.cursor()
    cur.execute("""
        SELECT s.exec_date, s.ts_code, s.action, s.target_weight, t.price, s.strategy_id
        FROM trade_log t JOIN signal_log s ON t.signal_id = s.signal_id
        WHERE s.strategy_id = %s ORDER BY s.exec_date, s.signal_id
    """, (strategy_id,))
    rows = cur.fetchall()
    cur.close()
    return rows


def load_config_cost(conn, strategy_id):
    cur = conn.cursor()
    cur.execute("SELECT config->>'cost_one_side' FROM strategy_config WHERE strategy_id=%s", (strategy_id,))
    r = cur.fetchone()
    cur.close()
    return float(r[0]) if r and r[0] else COST_DEFAULT


def load_quotes(conn, ts_codes, start):
    """日线 (open/close/pre_close/pct_chg) 宽表."""
    cur = conn.cursor()
    ph = ",".join(["%s"] * len(ts_codes))
    cur.execute(f"SELECT trade_date, ts_code, open, close, pre_close, pct_chg FROM daily_quote "
                f"WHERE ts_code IN ({ph}) AND trade_date >= %s", (*ts_codes, start))
    df = pd.DataFrame(cur.fetchall(), columns=["trade_date", "ts_code", "open", "close", "pre_close", "pct_chg"])
    cur.close()
    if df.empty:
        return {}
    for col in ("open", "close", "pre_close", "pct_chg"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    out = {}
    for col in ("open", "close", "pre_close", "pct_chg"):
        w = df.pivot(index="trade_date", columns="ts_code", values=col).sort_index()
        w.index = pd.to_datetime(w.index)
        out[col] = w
    return out


def build_nav(conn, strategy_id) -> pd.DataFrame:
    trades = load_trades(conn, strategy_id)
    if not trades:
        return pd.DataFrame()
    cost = load_config_cost(conn, strategy_id)

    # 成交日历: exec_date -> {ts_code: (action, weight, price)}
    by_date = {}
    for exec_d, ts_code, action, tw, price, _ in trades:
        by_date.setdefault(exec_d, {})[ts_code] = (action, tw, price)
    all_codes = sorted({t[1] for t in trades})
    quotes = load_quotes(conn, all_codes, min(by_date))

    # 全交易日序列 (从首个执行日到最新)
    cal = quotes["pct_chg"].index
    cal = cal[cal >= pd.Timestamp(min(by_date))]
    if len(cal) == 0:
        return pd.DataFrame()

    rows, nav = [], 1.0
    holdings = {}  # ts_code -> weight (成交后)
    for d in cal:
        d_date = d.date()
        day_cost = 0.0
        if d_date in by_date:
            # 当日成交: 先算成本 (换手金额 × 单边费率)
            for ts_code, (action, tw, price) in by_date[d_date].items():
                if action == "BUY":
                    day_cost += tw * cost          # 买入仓位 × 单边
                elif ts_code in holdings:
                    day_cost += holdings[ts_code] * cost  # 卖出仓位 × 单边
        # 持仓收益
        ret = 0.0
        for ts_code, w in holdings.items():
            if ts_code in by_date.get(d_date, {}) and by_date[d_date][ts_code][0] == "SELL":
                continue  # 开盘卖出: 当日不贡献
            if ts_code in quotes["pct_chg"].columns and d in quotes["pct_chg"].index:
                pc = quotes["pct_chg"].at[d, ts_code] if pd.notna(quotes["pct_chg"].at[d, ts_code]) else None
                if ts_code in by_date.get(d_date, {}) and by_date[d_date][ts_code][0] == "BUY":
                    # 买入日: 开盘价成交 → close/open - 1
                    o, c = quotes["open"].at[d, ts_code], quotes["close"].at[d, ts_code]
                    if pd.notna(o) and o > 0 and pd.notna(c):
                        ret += w * (c / o - 1)
                elif pc is not None:
                    ret += w * (pc / 100.0)  # daily_quote.pct_chg 为百分数
        # 成交后更新持仓
        if d_date in by_date:
            for ts_code, (action, tw, price) in by_date[d_date].items():
                if action == "BUY":
                    holdings[ts_code] = tw
                else:
                    holdings.pop(ts_code, None)
        daily = ret - day_cost
        nav *= (1 + daily)
        rows.append((strategy_id, d_date, nav, daily))

    df = pd.DataFrame(rows, columns=["strategy_id", "trade_date", "nav", "daily_ret"])
    # 基准
    cur = conn.cursor()
    cur.execute("SELECT trade_date, pct_chg FROM index_daily WHERE symbol='000300.SH' AND trade_date >= %s",
                (min(by_date),))
    bench = {r[0]: float(r[1]) / 100.0 for r in cur.fetchall() if r[1] is not None}
    cur.close()
    df["benchmark_ret"] = df["trade_date"].map(bench)
    return df


def save_nav(conn, df: pd.DataFrame):
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS portfolio_nav (
        strategy_id VARCHAR(64) NOT NULL,
        trade_date DATE NOT NULL,
        nav DOUBLE PRECISION NOT NULL,
        daily_ret DOUBLE PRECISION NOT NULL,
        benchmark_ret DOUBLE PRECISION,
        PRIMARY KEY (strategy_id, trade_date))""")
    for sid in df["strategy_id"].unique():
        cur.execute("DELETE FROM portfolio_nav WHERE strategy_id=%s", (sid,))
    from psycopg2.extras import execute_values
    execute_values(cur, """INSERT INTO portfolio_nav (strategy_id, trade_date, nav, daily_ret, benchmark_ret)
        VALUES %s ON CONFLICT (strategy_id, trade_date) DO NOTHING""",
        [(r.strategy_id, r.trade_date, r.nav, r.daily_ret, r.benchmark_ret) for r in df.itertuples()])
    conn.commit()
    cur.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default=None, help="不指定则重建全部")
    args = ap.parse_args()

    conn = fe.get_conn()
    cur = conn.cursor()
    if args.strategy:
        sids = [args.strategy]
    else:
        cur.execute("SELECT DISTINCT strategy_id FROM trade_log")
        sids = [r[0] for r in cur.fetchall()]
    cur.close()

    if not sids:
        print("无成交记录, portfolio_nav 为空")
        conn.close()
        return

    dfs = []
    for sid in sids:
        df = build_nav(conn, sid)
        if df.empty:
            print(f"{sid}: 无成交/无行情, 跳过")
            continue
        nav = df["nav"].iloc[-1]
        total = nav - 1
        ret = df["daily_ret"]
        print(f"{sid}: {len(df)} 交易日, 累计 {total:+.2%}, "
              f"年化 {((nav)**(244/max(len(df),1))-1):+.2%}")
        dfs.append(df)
    if dfs:
        all_df = pd.concat(dfs)
        save_nav(conn, all_df)
        print(f"✅ portfolio_nav 已写入 ({len(all_df)} 行)")
    conn.close()


if __name__ == "__main__":
    main()

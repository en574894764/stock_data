#!/usr/bin/env python3
"""ETF 动量轮动生产信号生成器 (趋势腿 P0)
==========================================
把 etf_rotation.py 的回测口径产品化: N 日动量 TopK + 绝对动量过滤(双动量)。

目标持仓 → 与 position 差分 → BUY/SELL 落 signal_log → 执行建议(飞书)。
与个股信号生成器 (generate_signals.py) 同构, 共用 signal_log / position / trade_log
三表与 execute_signals / build_nav / daily_review 执行链。

策略参数从 strategy_config 读 (config 即策略):
    mom=252 (动量窗口) | top_n=3 | abs_mom=true (Top1 动量<=0 → 全切现金 ETF)
    cash=511990 | min_history=120 | rebalance=monthly

用法:
    python3 scripts/generate_etf_signals.py              # 到调仓期才生成
    python3 scripts/generate_etf_signals.py --force      # 强制生成
    python3 scripts/generate_etf_signals.py --dry-run    # 预览, 不落库
    python3 scripts/generate_etf_signals.py --push       # 飞书推送
"""
import argparse
import os
import sys
import warnings

import numpy as np
import pandas as pd

# etf_rotation 用 DBAPI2 连接调 pd.read_sql, 新版 pandas 会打 SQLAlchemy 警告, 静默
warnings.filterwarnings("ignore", message=".*SQLAlchemy.*")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
sys.path.insert(0, REPO)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(REPO, ".env"))
except ImportError:
    pass

import factor_eval as fe  # noqa: E402  (get_conn)
from etf_rotation import POOL, CASH, load_etf_returns, load_list_dates  # noqa: E402
from generate_signals import (  # noqa: E402  (复用个股信号链的公共逻辑)
    load_strategy, is_rebalance_day, next_trading_day, push_feishu,
)

DEFAULT_STRATEGY = "prod_etf_mom"


# ---------------------------------------------------------------- 动量截面
def compute_etf_momentum(conn, codes, mom: int, end, min_history: int) -> dict:
    """最新截面 ETF 动量。返回 {code: 动量值}, 只含上市满 min_history 交易日的 ETF。
    动量 = cum[t] / cum[t-mom] - 1 (累计收益比), 数据不足 → -9 (排最末)。"""
    # 读足够长的日收益 (mom 窗口 × 1.8 余量 + 缓冲)
    start = (pd.Timestamp(end) - pd.Timedelta(days=int(mom * 1.8) + 90)).strftime("%Y-%m-%d")
    wide = load_etf_returns(conn, codes, start, end)
    if wide.empty:
        return {}
    list_dates = load_list_dates(conn)
    cum = (1 + wide.fillna(0)).cumprod()
    t = wide.index[-1]
    momv = {}
    for c in wide.columns:
        if list_dates.get(c) is not None and t > list_dates[c] + pd.Timedelta(days=min_history):
            shifted = cum[c].shift(mom)
            if t in shifted.index and pd.notna(shifted.loc[t]) and shifted.loc[t] > 0:
                momv[c] = float(cum[c].loc[t] / shifted.loc[t] - 1)
            else:
                momv[c] = -9.0
    return momv


# ---------------------------------------------------------------- 核心
def generate(cur, strategy: dict, force=False, dry_run=False) -> dict:
    cfg = strategy["config"]
    sid = strategy["strategy_id"]

    ok, why = is_rebalance_day(cur, sid, cfg["rebalance"])
    if not ok and not force:
        return {"skip": True, "reason": why}

    mom = cfg.get("mom", 252)
    top_n = cfg.get("top_n", 3)
    abs_mom = cfg.get("abs_mom", True)
    cash = cfg.get("cash", CASH)
    min_history = cfg.get("min_history", 120)
    pool = sorted(set(POOL) | {cash})

    # 最新 ETF 交易日
    cur.execute("SELECT MAX(trade_date) FROM etf_quote")
    factor_date = cur.fetchone()[0]
    if factor_date is None:
        raise SystemExit("etf_quote 无数据")

    momv = compute_etf_momentum(cur.connection, pool, mom, factor_date, min_history)
    if len(momv) < top_n + 2:
        raise SystemExit(f"ETF 动量截面不足: {len(momv)} < {top_n + 2}")

    ranked = sorted(momv.items(), key=lambda kv: -kv[1])
    sel = [c for c, _ in ranked[:top_n]]
    cash_on = False
    if abs_mom and ranked and ranked[0][1] <= 0:
        sel = [cash]
        cash_on = True

    # 目标持仓: TopK 等权 / 现金全仓
    weights = pd.Series(1.0 / len(sel), index=sel)
    target_w = float(weights.mean())

    # 当前持仓
    cur.execute("SELECT ts_code, weight FROM position WHERE strategy_id = %s", (sid,))
    current = dict(cur.fetchall())

    tgt, cur_ = set(weights.index), set(current.keys())
    buys = sorted(tgt - cur_)
    sells = sorted(cur_ - tgt)
    holds = sorted(tgt & cur_)

    exec_date = next_trading_day(cur, factor_date)

    rank_map = {c: i + 1 for i, (c, _) in enumerate(ranked)}
    signals = []
    for c in buys:
        signals.append((sid, factor_date, exec_date, c, "BUY", float(weights[c]),
                        float(momv.get(c, 0.0)), rank_map.get(c), "动量新进"))
    for c in sells:
        signals.append((sid, factor_date, exec_date, c, "SELL", 0.0, None, None, "动量剔除"))

    if not dry_run and signals:
        cur.executemany(
            """INSERT INTO signal_log (strategy_id, trade_date, exec_date, ts_code, action,
               target_weight, score, rank_in_pool, reason)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (strategy_id, trade_date, ts_code, action) DO NOTHING""",
            signals)
        cur.connection.commit()

    names = {**POOL, cash: "货币现金"}
    return {
        "skip": False, "reason": why, "factor_date": factor_date, "exec_date": exec_date,
        "pool_size": len(momv), "target": sel, "current": list(current.keys()),
        "buys": buys, "sells": sells, "holds": holds,
        "weights": weights, "target_w": target_w, "cash_on": cash_on,
        "ranked": [(names.get(c, c), v) for c, v in ranked[:top_n]],
        "signals_written": 0 if dry_run else len(signals),
    }


# ---------------------------------------------------------------- 报告
def render(res: dict, strategy: dict) -> str:
    if res["skip"]:
        return f"**{strategy['name']}** — 未到调仓期\n\n{res['reason']}"
    L = [f"### 🛰️ {strategy['name']} — ETF 轮动信号\n"]
    L.append(f"- 信号日: {res['factor_date']} | 建议执行: **{res['exec_date']} 开盘** | "
             f"池: {res['pool_size']} 只 | 目标持仓: {len(res['target'])} 只 "
             f"(等权 {res['target_w']*100:.0f}%)")
    if res["cash_on"]:
        L.append(f"- ⚠️ **绝对动量过滤触发**: 最强标的动量 ≤ 0, 全仓切换现金 (511990)\n")
    L.append(f"- 动量榜: " + ", ".join(f"{n} {v*100:+.1f}%" for n, v in res["ranked"]))
    L.append(f"- 变动: 买入 {len(res['buys'])} | 卖出 {len(res['sells'])} | 保留 {len(res['holds'])}")
    if res["sells"]:
        L.append(f"**卖出**: " + ", ".join(res["sells"]))
    if res["buys"]:
        L.append(f"**买入**: " + ", ".join(res["buys"]))
    if not res["buys"] and not res["sells"]:
        L.append("\n✅ 目标持仓与当前一致, 无交易")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default=DEFAULT_STRATEGY)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--push", action="store_true")
    args = ap.parse_args()

    conn = fe.get_conn()
    cur = conn.cursor()
    strategy = load_strategy(cur, args.strategy)
    res = generate(cur, strategy, force=args.force, dry_run=args.dry_run)

    report = render(res, strategy)
    print(report)
    if res.get("signals_written"):
        print(f"\n✅ signal_log 落库 {res['signals_written']} 条 (pending)")

    if args.push:
        push_feishu(report, strategy["name"])

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
信号生成器 (六子系统生产链 P0)
================================
读 strategy_config + factor_value 最新截面 → 股票池过滤 → z-score 合成打分
→ top N 目标持仓 → 与 position 差分 → BUY/SELL 信号落 signal_log → 执行建议报告。

用法:
    python3 scripts/generate_signals.py                   # 按调仓周期判断, 到期才生成
    python3 scripts/generate_signals.py --force           # 强制生成 (忽略周期)
    python3 scripts/generate_signals.py --dry-run         # 预览, 不落库
    python3 scripts/generate_signals.py --push            # 飞书推送执行建议
    python3 scripts/generate_signals.py --strategy prod_6f_eq
"""
import argparse
import json
import os
import sys
from datetime import date

import numpy as np
import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
sys.path.insert(0, REPO)

# .env 凭证 (飞书推送; launchd 环境下由 pipeline load_env 注入, 手动跑时由这里兜底)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(REPO, ".env"))
except ImportError:
    pass

import factor_eval as fe  # 复用 get_conn / load_universe_filter


# ---------------------------------------------------------------- 数据
def load_strategy(cur, strategy_id: str) -> dict:
    cur.execute("SELECT strategy_id, name, config FROM strategy_config "
                "WHERE strategy_id = %s AND is_active", (strategy_id,))
    row = cur.fetchone()
    if not row:
        raise SystemExit(f"策略不存在或未激活: {strategy_id}")
    return {"strategy_id": row[0], "name": row[1], "config": row[2]}


def load_factor_cross(cur, factors: dict) -> tuple:
    """最新截面因子宽表 (index=ts_code, columns=factor_name). 返回 (df, trade_date)."""
    names = tuple(factors.keys())
    ph = ",".join(["%s"] * len(names))
    cur.execute(f"SELECT MAX(trade_date) FROM factor_value WHERE factor_name IN ({ph})", names)
    d = cur.fetchone()[0]
    if d is None:
        raise SystemExit(f"factor_value 无这些因子: {names}")
    cur.execute(f"SELECT factor_name, ts_code, value FROM factor_value "
                f"WHERE trade_date = %s AND factor_name IN ({ph})", (d,) + names)
    df = pd.DataFrame(cur.fetchall(), columns=["factor_name", "ts_code", "value"])
    wide = df.pivot(index="ts_code", columns="factor_name", values="value")
    return wide, d


def next_trading_day(cur, d) -> date:
    """下一交易日: 优先 trade_cal (含未来日历), 回退 index_daily (仅历史)."""
    cur.execute("SELECT MIN(cal_date) FROM trade_cal WHERE is_open = '1' AND cal_date > %s", (d,))
    r = cur.fetchone()[0]
    if r:
        return r
    cur.execute("SELECT MIN(trade_date) FROM index_daily WHERE symbol='000001.SH' AND trade_date > %s", (d,))
    r = cur.fetchone()[0]
    return r if r else d


def last_signal_date(cur, strategy_id: str):
    cur.execute("SELECT MAX(trade_date) FROM signal_log WHERE strategy_id = %s", (strategy_id,))
    return cur.fetchone()[0]


def trading_days_between(cur, d1, d2) -> int:
    cur.execute("SELECT COUNT(*) FROM index_daily WHERE symbol='000001.SH' "
                "AND trade_date > %s AND trade_date <= %s", (d1, d2))
    return cur.fetchone()[0]


def is_rebalance_day(cur, strategy_id: str, rebalance: str) -> tuple:
    """返回 (是否调仓, 说明). 无历史信号 → 首次必调."""
    last = last_signal_date(cur, strategy_id)
    if last is None:
        return True, "首次生成"
    if rebalance == "monthly":
        need, span = 20, "月度(≈20交易日)"
    elif rebalance == "biweekly":
        need, span = 10, "双周(≈10交易日)"
    elif rebalance.startswith("n_days:"):
        need, span = int(rebalance.split(":")[1]), rebalance
    else:
        return True, f"未知周期 {rebalance}, 默认生成"
    today = date.today()
    passed = trading_days_between(cur, last, today)
    return passed >= need, f"距上次信号 {passed} 交易日 (周期 {span})"


# ---------------------------------------------------------------- 核心
def compose_score(cross: pd.DataFrame, factors: dict) -> pd.Series:
    """z-score 加权合成 (value 越大预期收益越高; 至少一半因子有值才出分)."""
    zs = []
    for name, w in factors.items():
        if name not in cross.columns:
            raise SystemExit(f"截面缺因子列: {name}")
        col = cross[name].dropna()
        sd = col.std()
        z = (col - col.mean()) / (sd if sd and sd > 0 else 1.0)
        zs.append(z * w)
    return pd.concat(zs, axis=1).sum(axis=1, min_count=len(zs) // 2)


def generate(cur, strategy: dict, force=False, dry_run=False) -> dict:
    cfg = strategy["config"]
    sid = strategy["strategy_id"]

    ok, why = is_rebalance_day(cur, sid, cfg["rebalance"])
    if not ok and not force:
        return {"skip": True, "reason": why}

    # 因子截面
    cross, factor_date = load_factor_cross(cur, cfg["factors"])
    cross_t = pd.Timestamp(factor_date)

    # 股票池: 沪深非 ST + 上市满 N 自然日 (与 factor_eval 同口径)
    uni, list_dates = fe.load_universe_filter(cur.connection)
    t = cross_t
    keep = [c for c in cross.index if c in uni and list_dates.get(c) is not None
            and t > pd.Timestamp(list_dates[c]) + pd.Timedelta(days=cfg["min_history_days"])]
    pool = cross.loc[keep]
    if len(pool) < cfg["min_cross_section"]:
        raise SystemExit(f"截面不足: {len(pool)} < {cfg['min_cross_section']}")

    # 打分 + top N
    score = compose_score(pool, cfg["factors"])
    score = score.dropna().sort_values(ascending=False)
    top_n = cfg["top_n"]
    target = score.head(top_n)
    target_w = 1.0 / len(target)

    # 当前持仓
    cur.execute("SELECT ts_code, weight FROM position WHERE strategy_id = %s", (sid,))
    current = dict(cur.fetchall())

    tgt, cur_ = set(target.index), set(current.keys())
    buys = sorted(tgt - cur_)
    sells = sorted(cur_ - tgt)
    holds = sorted(tgt & cur_)

    exec_date = next_trading_day(cur, factor_date)

    signals = []
    for c in buys:
        signals.append((sid, factor_date, exec_date, c, "BUY", target_w,
                        float(target[c]), int(target.index.get_loc(c)) + 1, "新进"))
    for c in sells:
        signals.append((sid, factor_date, exec_date, c, "SELL", 0.0, None, None, "调仓剔除"))

    if not dry_run and signals:
        cur.executemany(
            """INSERT INTO signal_log (strategy_id, trade_date, exec_date, ts_code, action,
               target_weight, score, rank_in_pool, reason)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (strategy_id, trade_date, ts_code, action) DO NOTHING""",
            signals)
        cur.connection.commit()

    return {
        "skip": False, "reason": why, "factor_date": factor_date, "exec_date": exec_date,
        "pool_size": len(pool), "target": target, "current": current,
        "buys": buys, "sells": sells, "holds": holds, "target_w": target_w,
        "signals_written": 0 if dry_run else len(signals),
    }


# ---------------------------------------------------------------- 报告
def render(res: dict, strategy: dict) -> str:
    if res["skip"]:
        return f"**{strategy['name']}** — 未到调仓期\n\n{res['reason']}"
    L = [f"### 📋 {strategy['name']} — 调仓信号\n"]
    L.append(f"- 信号日: {res['factor_date']} | 建议执行: **{res['exec_date']} 开盘** | "
             f"股票池: {res['pool_size']} 只 | 目标持仓: {len(res['target'])} 只 "
             f"(等权 {res['target_w']*100:.1f}%)")
    L.append(f"- 变动: 买入 {len(res['buys'])} | 卖出 {len(res['sells'])} | 保留 {len(res['holds'])} | "
             f"换手 {len(res['buys'])+len(res['sells'])} / {len(res['target'])}\n")

    if res["sells"]:
        L.append(f"**卖出 ({len(res['sells'])})**: " + ", ".join(res["sells"]))
    if res["buys"]:
        tgt = res["target"]
        L.append(f"\n**买入 ({len(res['buys'])})**:")
        L.append("| 代码 | 合成分 | 池内排名 |")
        L.append("|---|---|---|")
        for c in res["buys"]:
            L.append(f"| {c} | {tgt[c]:.2f} | {tgt.index.get_loc(c)+1} |")
    if not res["buys"] and not res["sells"]:
        L.append("\n✅ 目标持仓与当前一致, 无交易")
    return "\n".join(L)


def push_feishu(text: str, strategy_name: str) -> bool:
    try:
        from report_builder import _push_feishu_card
        return _push_feishu_card(text, f"🎯 策略信号 · {strategy_name}", "blue")
    except Exception as e:
        print(f"[Feishu] 推送失败: {e}")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="prod_6f_eq")
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

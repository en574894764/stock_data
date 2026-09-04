#!/usr/bin/env python3
"""
信号执行状态机 (signal → position → trade_log)
=============================================
把 pending 信号推进为 executed, 更新 position 快照, 记录 trade_log。

模式:
    --simulate   模拟成交: 按 exec_date 开盘价全量成交 (灰度期用, 虚拟资金 100 万)
    --confirm    实盘确认: 指定 signal-id 与实际成交价
    --list       仅列出 pending 信号 (默认)

用法:
    python3 scripts/execute_signals.py --list
    python3 scripts/execute_signals.py --simulate                    # 执行全部到期 pending
    python3 scripts/execute_signals.py --simulate --date 2026-09-07  # 只执行指定执行日
    python3 scripts/execute_signals.py --confirm --signal-id 123 --price 4.56 --volume 2000
"""
import argparse
import os
import sys
from datetime import date

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
sys.path.insert(0, REPO)

import factor_eval as fe

VIRTUAL_CASH = 1_000_000
EXPIRE_DAYS = 5  # exec_date 后 N 个交易日仍无行情 → expired


def list_pending(conn, strategy_id=None):
    cur = conn.cursor()
    sql = ("SELECT signal_id, strategy_id, trade_date, exec_date, ts_code, action, "
           "target_weight, reason FROM signal_log WHERE status = 'pending'")
    args = []
    if strategy_id:
        sql += " AND strategy_id = %s"
        args.append(strategy_id)
    sql += " ORDER BY exec_date, signal_id"
    cur.execute(sql, args)
    rows = cur.fetchall()
    cur.close()
    return rows


def get_open_prices(conn, ts_codes: list, d) -> dict:
    """d 日开盘价 (daily_quote; 停牌/未出行情的不在返回里)."""
    if not ts_codes:
        return {}
    cur = conn.cursor()
    ph = ",".join(["%s"] * len(ts_codes))
    cur.execute(f"SELECT ts_code, open FROM daily_quote WHERE trade_date = %s AND ts_code IN ({ph})",
                (d, *ts_codes))
    out = {r[0]: float(r[1]) for r in cur.fetchall() if r[1] and float(r[1]) > 0}
    cur.close()
    return out


def trading_days_since(conn, d) -> int:
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM index_daily WHERE symbol='000001.SH' AND trade_date > %s", (d,))
    n = cur.fetchone()[0]
    cur.close()
    return n


def simulate_execute(conn, strategy_id=None, only_date=None) -> dict:
    """按 exec_date 开盘价模拟成交: pending → executed; position upsert/delete; trade_log 落库."""
    rows = [r for r in list_pending(conn, strategy_id)
            if r[3] <= ((date.fromisoformat(only_date) if only_date else date.today()))]
    if not rows:
        return {"executed": 0, "skipped_no_price": [], "expired": 0}

    cur = conn.cursor()
    prices = get_open_prices(conn, sorted({r[4] for r in rows}), max(r[3] for r in rows))
    executed, skipped, expired = 0, [], 0

    for sig_id, sid, trade_d, exec_d, ts_code, action, tw, reason in rows:
        px = prices.get(ts_code)
        if px is None:
            if trading_days_since(conn, exec_d) >= EXPIRE_DAYS:
                cur.execute("UPDATE signal_log SET status='expired' WHERE signal_id=%s", (sig_id,))
                expired += 1
            else:
                skipped.append((ts_code, str(exec_d)))
            continue

        if action == "BUY":
            value = VIRTUAL_CASH * tw
            volume = int(round(value / px / 100) * 100) or 100
            cur.execute(
                """INSERT INTO position (strategy_id, ts_code, weight, entry_date, last_signal, updated_at)
                   VALUES (%s,%s,%s,%s,%s,now())
                   ON CONFLICT (strategy_id, ts_code) DO UPDATE
                   SET weight=EXCLUDED.weight, last_signal=EXCLUDED.last_signal, updated_at=now()""",
                (sid, ts_code, tw, exec_d, trade_d))
        else:  # SELL
            cur.execute("SELECT weight FROM position WHERE strategy_id=%s AND ts_code=%s", (sid, ts_code))
            prow = cur.fetchone()
            value = VIRTUAL_CASH * (prow[0] if prow else 0)
            volume = int(round(value / px / 100) * 100)
            cur.execute("DELETE FROM position WHERE strategy_id=%s AND ts_code=%s", (sid, ts_code))

        cur.execute(
            "INSERT INTO trade_log (signal_id, executed_at, price, volume, value, note) "
            "VALUES (%s, now(), %s, %s, %s, 'simulate')",
            (sig_id, px, volume, volume * px))
        cur.execute("UPDATE signal_log SET status='executed' WHERE signal_id=%s", (sig_id,))
        executed += 1

    conn.commit()
    cur.close()
    return {"executed": executed, "skipped_no_price": skipped, "expired": expired}


def confirm_execute(conn, signal_id: int, price: float, volume: float):
    """实盘确认: 用实际成交价回填 (同 simulate 的状态机, 价格外部给定)."""
    cur = conn.cursor()
    cur.execute("SELECT strategy_id, trade_date, exec_date, ts_code, action, target_weight "
                "FROM signal_log WHERE signal_id=%s AND status='pending'", (signal_id,))
    row = cur.fetchone()
    if not row:
        raise SystemExit(f"signal {signal_id} 不存在或非 pending")
    sid, trade_d, exec_d, ts_code, action, tw = row
    if action == "BUY":
        cur.execute(
            """INSERT INTO position (strategy_id, ts_code, weight, entry_date, last_signal, updated_at)
               VALUES (%s,%s,%s,%s,%s,now())
               ON CONFLICT (strategy_id, ts_code) DO UPDATE
               SET weight=EXCLUDED.weight, last_signal=EXCLUDED.last_signal, updated_at=now()""",
            (sid, ts_code, tw, exec_d, trade_d))
    else:
        cur.execute("DELETE FROM position WHERE strategy_id=%s AND ts_code=%s", (sid, ts_code))
    cur.execute("INSERT INTO trade_log (signal_id, executed_at, price, volume, value, note) "
                "VALUES (%s, now(), %s, %s, %s, 'confirm')",
                (signal_id, price, volume, price * volume))
    cur.execute("UPDATE signal_log SET status='executed' WHERE signal_id=%s", (signal_id,))
    conn.commit()
    cur.close()
    print(f"✅ signal #{signal_id} {action} {ts_code} @{price} × {volume} 已确认, position 已更新")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default=None)
    ap.add_argument("--simulate", action="store_true")
    ap.add_argument("--confirm", action="store_true")
    ap.add_argument("--list", action="store_true", help="仅列出 pending (默认)")
    ap.add_argument("--date", default=None, help="只处理该执行日 (YYYY-MM-DD)")
    ap.add_argument("--signal-id", type=int, default=None)
    ap.add_argument("--price", type=float, default=None)
    ap.add_argument("--volume", type=float, default=None)
    args = ap.parse_args()

    conn = fe.get_conn()

    if args.simulate:
        res = simulate_execute(conn, args.strategy, args.date)
        print(f"✅ 模拟成交 {res['executed']} 条")
        if res["skipped_no_price"]:
            print(f"⏭️ 无开盘价未成交 {len(res['skipped_no_price'])} 条 (留待重试): "
                  f"{res['skipped_no_price'][:10]}")
        if res["expired"]:
            print(f"🗑️ 过期作废 {res['expired']} 条")
        conn.close()
        return

    if args.confirm:
        if not (args.signal_id and args.price):
            ap.error("--confirm 需要 --signal-id 与 --price")
        confirm_execute(conn, args.signal_id, args.price, args.volume)
        conn.close()
        return

    # 默认: 列出 pending
    rows = list_pending(conn, args.strategy)
    print(f"pending 信号: {len(rows)} 条")
    for r in rows[:80]:
        print(f"  #{r[0]} {r[1]} 信号{r[2]} 执行{r[3]} {r[4]} {r[5]} w={r[6]} ({r[7]})")
    if len(rows) > 80:
        print(f"  ... 共 {len(rows)} 条")
    conn.close()


if __name__ == "__main__":
    main()

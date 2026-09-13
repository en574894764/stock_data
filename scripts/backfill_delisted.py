#!/usr/bin/env python3
"""退市股历史行情回补 — 补齐 daily_quote 中已退市 A 股的缺口区间

背景（2026-09-14，数据基建诊断 P1）：
  已退市 A股在 tushare 的 daily 接口中仍有历史数据，但日常 pipeline 的区间自愈
  只从「全表 MAX(trade_date)+1」往后拉，退市股停在某个历史点位后永远不会被回补。
  实测：339 只退市股中 316 只有缺口（18 只完全无日线），合计约 42 万个交易日行。

策略：
  逐只算缺口区间 [PG MAX+1, delist_date]（无数据则 [list_date, delist_date]）
  → pro.daily(ts_code, start, end) 拉取 → 与 pipeline 同一 upsert SQL 写入
  → 幂等（ON CONFLICT DO NOTHING）+ 可断点续跑（每次重算缺口）

用法：
  python scripts/backfill_delisted.py --dry-run        # 只列缺口，不拉数
  python scripts/backfill_delisted.py --limit 5        # 先试 5 只
  python scripts/backfill_delisted.py                  # 全量
  python scripts/backfill_delisted.py --only 000005.SZ,000018.SZ
  python scripts/backfill_delisted.py --export         # 跑完自动重导 CSV

注意：
  - 需 .env 中的 TUSHARE_TOKEN（load_env 注入）
  - 北交所退市股（.BJ）tushare 覆盖不一，拉不到会记 SKIP，不算失败
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

import psycopg2
import psycopg2.extras

REPO = Path(__file__).resolve().parent.parent
DB = dict(host=os.environ.get("PGHOST", "/tmp"), dbname=os.environ.get("PGDATABASE", "investassist"),
          user=os.environ.get("PGUSER", "james"), connect_timeout=5)
LOG_FILE = REPO / "logs" / "backfill_delisted.log"

DAILY_QUOTE_INSERT_SQL = """
    INSERT INTO daily_quote (ts_code, trade_year, trade_date, pre_close, open, high, low, close, change, pct_chg, vol, amount)
    VALUES %s
    ON CONFLICT (ts_code, trade_year, trade_date) DO NOTHING
"""


def load_env():
    p = REPO / ".env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"\''))


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def _clean_float(v):
    try:
        if v is None:
            return None
        f = float(v)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def ensure_partition(conn, year: int):
    """确保 daily_quote_YYYY 分区存在（退市股回补可能触及任何年份）。"""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name=%s", (f"daily_quote_{year}",))
        if cur.fetchone():
            return
        log(f"  创建分区 daily_quote_{year}")
        cur.execute(f"""CREATE TABLE IF NOT EXISTS daily_quote_{year}
                        PARTITION OF daily_quote FOR VALUES FROM ('{year}-01-01') TO ('{year + 1}-01-01')""")
    conn.commit()


def load_targets(conn, only: list[str] | None, gap_days: int = 10) -> list[dict]:
    """算退市股缺口区间。gap_days: 距退市日不足该天数视为已完整。"""
    sql = """
    WITH d AS (
        SELECT ts_code, name, list_date, delist_date
        FROM stocks
        WHERE delist_date IS NOT NULL
          AND ts_code NOT LIKE '%%.HK'
          AND (exchange IN ('SSE','SZSE','BSE') OR ts_code LIKE '%%.SH' OR ts_code LIKE '%%.SZ' OR ts_code LIKE '%%.BJ')
    ), q AS (
        SELECT ts_code, MAX(trade_date) AS mx, COUNT(*) AS n
        FROM daily_quote WHERE ts_code NOT LIKE '%%.HK' GROUP BY ts_code
    )
    SELECT d.ts_code, d.name, d.list_date, d.delist_date,
           q.mx AS pg_max, COALESCE(q.n, 0) AS pg_rows
    FROM d LEFT JOIN q ON d.ts_code = q.ts_code
    WHERE q.mx IS NULL OR q.mx < d.delist_date - %s
    ORDER BY d.delist_date, d.ts_code
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (gap_days,))
        rows = [dict(r) for r in cur.fetchall()]
    if only:
        want = set(only)
        rows = [r for r in rows if r["ts_code"] in want]
    return rows


def trading_days_between(conn, start: date, end: date) -> int:
    with conn.cursor() as cur:
        cur.execute("""SELECT COUNT(*) FROM trade_cal
                       WHERE is_open::int = 1 AND cal_date >= %s AND cal_date <= %s""", (start, end))
        return cur.fetchone()[0]


def fetch_one(conn, pro, tgt: dict, sleep: float = 0.25) -> tuple[str, int, str]:
    """返回 (状态, 写入行数, 说明)"""
    ts_code = tgt["ts_code"]
    delist = tgt["delist_date"]
    pg_max = tgt["pg_max"]
    start = (pg_max + __import__("datetime").timedelta(days=1)) if pg_max else (tgt["list_date"] or date(2000, 1, 1))
    end = delist
    if start > end:
        return ("OK", 0, "无需补")
    nd = trading_days_between(conn, start, end)
    if nd <= 0:
        return ("OK", 0, "区间内无交易日")
    try:
        df = pro.daily(ts_code=ts_code, start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"))
    except Exception as e:
        return ("FAIL", 0, f"API 异常: {str(e)[:60]}")
    time.sleep(sleep)
    if df is None or df.empty:
        return ("SKIP", 0, f"源端无数据 (应补 {nd} 日)")

    rows = []
    for r in df.to_dict("records"):
        o, h, l, c = (_clean_float(r.get(x)) for x in ("open", "high", "low", "close"))
        if not (o and o > 0 and h and h > 0 and l and l > 0 and c and c > 0):
            continue
        td = str(r["trade_date"])
        rows.append((r["ts_code"], int(td[:4]), f"{td[:4]}-{td[4:6]}-{td[6:8]}",
                     _clean_float(r.get("pre_close")), o, h, l, c,
                     _clean_float(r.get("change")), _clean_float(r.get("pct_chg")),
                     _clean_float(r.get("vol")), _clean_float(r.get("amount"))))
    if not rows:
        return ("SKIP", 0, "返回全为零值/脏行")

    years = sorted({r[1] for r in rows})
    for y in years:
        ensure_partition(conn, y)
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, DAILY_QUOTE_INSERT_SQL, rows, page_size=1000)
    conn.commit()
    return ("OK", len(rows), f"{start} ~ {end} 拉到 {len(rows)} 行 (应补 {nd} 日)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只列缺口")
    ap.add_argument("--limit", type=int, help="只处理前 N 只（试跑）")
    ap.add_argument("--only", help="指定 ts_code，逗号分隔")
    ap.add_argument("--sleep", type=float, default=0.25, help="每只间隔秒数（默认 0.25）")
    ap.add_argument("--export", action="store_true", help="跑完自动重导 CSV")
    args = ap.parse_args()

    load_env()
    import tushare as ts
    token = os.environ.get("TUSHARE_TOKEN", "")
    if not token and not args.dry_run:
        print("❌ 未找到 TUSHARE_TOKEN", file=sys.stderr)
        return 1
    pro = ts.pro_api(token) if token else None

    conn = psycopg2.connect(**DB)
    only = [s.strip() for s in args.only.split(",")] if args.only else None
    targets = load_targets(conn, only)
    total_gap_days = 0
    for t in targets:
        s = (t["pg_max"] + __import__("datetime").timedelta(days=1)) if t["pg_max"] else (t["list_date"] or date(2000, 1, 1))
        t["_gap_days"] = trading_days_between(conn, s, t["delist_date"])
        total_gap_days += max(t["_gap_days"], 0)

    log(f"{'='*70}")
    log(f"退市股回补 | 目标 {len(targets)} 只 | 待补交易日合计 {total_gap_days:,}"
        f"{' | DRY-RUN' if args.dry_run else ''}")
    log(f"{'='*70}")

    if args.dry_run:
        for t in targets[:30]:
            print(f"  {t['ts_code']:<12} {t['name']:<14} 退市 {t['delist_date']} | 现有 {t['pg_rows']:>5} 行 "
                  f"| PG MAX {t['pg_max'] or '无'} | 待补 {t['_gap_days']:>4} 交易日")
        if len(targets) > 30:
            print(f"  ... 其余 {len(targets) - 30} 只（合计待补 {total_gap_days:,} 交易日）")
        conn.close()
        return 0

    if args.limit:
        targets = targets[:args.limit]

    stats = {"OK": 0, "SKIP": 0, "FAIL": 0}
    written = 0
    t0 = time.time()
    for i, t in enumerate(targets, 1):
        status, n, note = fetch_one(conn, pro, t, sleep=args.sleep)
        stats[status] = stats.get(status, 0) + 1
        written += n
        if status != "OK" or i % 20 == 0 or i <= 5:
            log(f"  [{i}/{len(targets)}] {t['ts_code']} {t['name']} → {status} {note}")
    log(f"完成 | 用时 {time.time()-t0:.0f}s | 写入 {written:,} 行 | "
        f"OK {stats.get('OK',0)} SKIP {stats.get('SKIP',0)} FAIL {stats.get('FAIL',0)}")

    # 复核剩余缺口
    still = load_targets(conn, only)
    log(f"剩余缺口: {len(still)} 只")
    conn.close()

    if args.export:
        import subprocess
        log("重导 CSV ...")
        r = subprocess.run([sys.executable, str(REPO / "scripts" / "export.py")],
                           capture_output=True, text=True, cwd=REPO, timeout=3600)
        log(f"export exit={r.returncode}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

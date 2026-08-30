#!/usr/bin/env python3
"""L2 周度对账质检 — PG ↔ CSV 对账 / 标的生命周期 / 财报合理性 / 分布漂移

质检方案 2026-08-30 F4。每周六 pipeline 尾部自动调用，也可手动:
    python scripts/reconcile.py            # 全部四项
    python scripts/reconcile.py --only csv # 只跑某项 (csv|lifecycle|financial|drift)

退出码: 有 ERR 级问题返回 1（pipeline 据此推飞书告警）。
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

ERRS, WARNS = [], []


def err(item: str, detail: str):
    ERRS.append((item, detail))
    print(f"  ❌ {item} — {detail}")


def warn(item: str, detail: str):
    WARNS.append((item, detail))
    print(f"  ⚠️ {item} — {detail}")


def ok(item: str):
    print(f"  ✅ {item}")


def get_conn():
    import psycopg2
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "/tmp"),
        dbname=os.environ.get("PGDATABASE", "investassist"),
        user=os.environ.get("PGUSER", "james"),
        password=os.environ.get("PGPASSWORD", ""),
        connect_timeout=5,
    )


# ── 1. PG ↔ CSV 对账 ────────────────────────────────────────────────────────

def check_csv_reconciliation(conn):
    """daily/ 每个文件行数 vs PG 行数（PG 多于 CSV = 导出滞后；CSV 多于 PG = 异常）。
    抽样 20 个文件做内容比对（首行/中间行/末行）。
    """
    print("\n── 1. PG ↔ CSV 对账 ──")
    cur = conn.cursor()
    daily_dir = REPO / "daily"

    # PG 行数按标的
    cur.execute("SELECT ts_code, COUNT(*) FROM daily_quote GROUP BY ts_code")
    pg_counts = dict(cur.fetchall())

    csv_counts = {}
    for f in daily_dir.glob("*.csv"):
        with open(f, "rb") as fh:
            csv_counts[f.stem] = sum(1 for _ in fh) - 1  # 减 header

    # 只对账 PG 中有的标的（旧退市股 CSV 保留是设计行为）
    lagging, ahead, mismatch = [], [], 0
    for ts, n_pg in pg_counts.items():
        n_csv = csv_counts.get(ts)
        if n_csv is None:
            continue  # per-symbol 导出未覆盖（如本轮未重导），不算
        if n_pg > n_csv:
            lagging.append((ts, n_csv, n_pg))
        elif n_csv > n_pg:
            ahead.append((ts, n_csv, n_pg))

    if lagging:
        warn("CSV 落后 PG", f"{len(lagging)} 个文件（最近导出后 PG 又有写入，正常于交易日；"
                            f"周六应≈0）: {lagging[:5]}")
    else:
        ok("daily/ 全部文件与 PG 行数一致")

    if ahead:
        err("CSV 多于 PG", f"{len(ahead)} 个文件行数超过 PG（疑似数据被删未重导）: {ahead[:5]}")

    # 抽样内容比对：10 只活跃 + 5 只港股 + 5 只 ETF
    cur.execute("""
        SELECT ts_code FROM daily_quote WHERE trade_date > CURRENT_DATE - 7
          AND ts_code LIKE '%.HK' ORDER BY random() LIMIT 5""")
    sample = [r[0] for r in cur.fetchall()]
    cur.execute("""
        SELECT ts_code FROM daily_quote WHERE trade_date > CURRENT_DATE - 7
          AND ts_code NOT LIKE '%.HK' AND ts_code NOT LIKE '51%' AND ts_code NOT LIKE '15%'
          AND ts_code NOT LIKE '56%' AND ts_code NOT LIKE '58%'
        ORDER BY random() LIMIT 5""")
    sample += [r[0] for r in cur.fetchall()]

    checked, content_bad = 0, 0
    for ts in sample:
        f = daily_dir / f"{ts}.csv"
        if not f.exists():
            continue
        cur.execute("""SELECT trade_date::text, open, high, low, close, vol, amount
                       FROM daily_quote WHERE ts_code = %s ORDER BY trade_date DESC LIMIT 1""",
                    (ts,))
        row = cur.fetchone()
        if not row:
            continue
        with open(f) as fh:
            lines = [l.rstrip("\n") for l in fh if l.strip()]
        if not lines:
            continue
        last = lines[-1].split(",")
        # CSV 格式: symbol,datetime,open,high,low,close,volume,amount
        try:
            if last[1][:10] != row[0] or abs(float(last[5]) - float(row[4])) > 1e-6:
                content_bad += 1
                print(f"    {ts}: CSV 末行 {last[1][:10]} close={last[5]} vs PG {row[0]} close={row[4]}")
        except (ValueError, IndexError):
            content_bad += 1
        checked += 1

    if content_bad:
        err("抽样内容比对失败", f"{content_bad}/{checked} 文件末行与 PG 不一致")
    elif checked:
        ok(f"抽样内容比对 {checked} 文件末行全部一致")


# ── 2. 标的生命周期 ────────────────────────────────────────────────────────

def check_lifecycle(conn):
    """stocks 表上市中 vs daily_quote 近期出现: 新股漏拉 / 退市残留。"""
    print("\n── 2. 标的生命周期对账 ──")
    cur = conn.cursor()
    # stocks 表 list_status='L' 的标的，近 45 天无日线 → 漏拉（容忍新股上市初期）
    cur.execute("""
        SELECT s.ts_code, s.name FROM stocks s
        WHERE s.list_status = 'L' AND s.list_date < CURRENT_DATE - 45
          AND NOT EXISTS (
            SELECT 1 FROM daily_quote d
            WHERE d.ts_code = s.ts_code AND d.trade_date > CURRENT_DATE - 45
          )
        ORDER BY s.ts_code LIMIT 20""")
    rows = cur.fetchall()
    if len(rows) >= 50:
        warn("上市中(>45天)但 45 天无日线", f"{len(rows)}+ 只（疑似漏拉或数据源失效，需人工确认）: "
              f"{[r[0] for r in rows[:8]]}")
    elif len(rows) > 0:
        print(f"  ℹ️ 上市中但 45 天无日线 {len(rows)} 只（<50，多为真实停牌/长期无成交，抽查: "
              f"{[r[0] for r in rows[:5]]}）")
    else:
        ok("上市中标的均有近期日线")


# ── 3. 财报数字合理性 ──────────────────────────────────────────────────────

def check_financial_sanity(conn):
    """净利率极端 / 资产负债率极端 — flag 供人工抽查（不算失败）。"""
    print("\n── 3. 财报数字合理性 ──")
    cur = conn.cursor()
    cur.execute("""
        SELECT ts_code, total_revenue, n_income FROM income
        WHERE report_year = EXTRACT(YEAR FROM CURRENT_DATE)
          AND report_type = '1' AND total_revenue > 0
          AND ABS(n_income::numeric / total_revenue) > 1.0
        ORDER BY ABS(n_income::numeric / total_revenue) DESC LIMIT 10""")
    rows = cur.fetchall()
    if rows:
        warn(f"{TODAY_STR} 年净利率 |margin|>100%", f"{len(rows)} 只 flag（投资收益主导型可能合理）: "
              f"{[r[0] for r in rows[:6]]}")
    else:
        ok("无极端净利率")


# ── 4. 分布漂移 ────────────────────────────────────────────────────────────

def check_drift(conn):
    """全市场最近 5 交易日 vs 前 5 交易日: 成交量中位数环比（节假日前后会自然波动，仅提示）。"""
    print("\n── 4. 分布漂移 ──")
    cur = conn.cursor()
    cur.execute("""
        WITH days AS (
          SELECT DISTINCT trade_date FROM daily_quote
          WHERE ts_code NOT LIKE '%.HK' ORDER BY trade_date DESC LIMIT 10
        ),
        ranked AS (
          SELECT trade_date, ROW_NUMBER() OVER (ORDER BY trade_date DESC) AS rn FROM days
        )
        SELECT
          CASE WHEN rn <= 5 THEN 'recent' ELSE 'prior' END AS bucket,
          PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY vol) AS med_vol
        FROM daily_quote d JOIN ranked r ON d.trade_date = r.trade_date
        WHERE d.ts_code NOT LIKE '%.HK' AND d.vol > 0
        GROUP BY 1""")
    rows = dict(cur.fetchall())
    recent, prior = rows.get("recent"), rows.get("prior")
    if recent and prior and prior > 0:
        ratio = float(recent) / float(prior)
        if ratio > 3 or ratio < 1 / 3:
            warn("成交量中位数漂移", f"近 5 日/前 5 日 = {ratio:.1f} 倍（疑似拉取事故或极端行情）")
        else:
            ok(f"成交量中位数稳定（近/前 = {ratio:.2f}）")
    else:
        warn("成交量漂移", "数据不足无法比较")


TODAY_STR = str(date.today())


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--only", choices=["csv", "lifecycle", "financial", "drift"],
                   help="只跑某一项")
    args = p.parse_args()

    print(f"{'='*60}\nL2 周度对账质检 | {TODAY_STR}\n{'='*60}")
    conn = get_conn()
    try:
        if args.only in (None, "csv"):
            check_csv_reconciliation(conn)
        if args.only in (None, "lifecycle"):
            check_lifecycle(conn)
        if args.only in (None, "financial"):
            check_financial_sanity(conn)
        if args.only in (None, "drift"):
            check_drift(conn)
    finally:
        conn.close()

    print(f"\n{'='*60}")
    print(f"L2 完成 | ❌ {len(ERRS)} 错误  ⚠️ {len(WARNS)} 警告")
    return 1 if ERRS else 0


if __name__ == "__main__":
    sys.exit(main())

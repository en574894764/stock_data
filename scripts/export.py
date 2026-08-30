#!/usr/bin/env python3
"""Export data from PostgreSQL → stock_data CSV（M4 改造 2026-08-30）

导出内容：
  daily/<symbol>.csv                          按标的个股日线（全量重写，A股+港股）
  index/<symbol>.csv                          指数日线（按 symbol 全量重写）
  data/daily/{a_shares,etf,hk}/YYYY-Qn.csv     季度打包（原有功能）
  data/fundamental/*.csv                       财报表（原有）
  data/meta/*.csv                              基础信息（原有）

  宏观 macro/*.csv 不经 PG（fetch_macro.py 直写，见决策点 D2）。
  index/ 中 PG 无对应 symbol 的旧文件（DJI/SPX 等美股指数）保留不动。

用法：
  python3 scripts/export.py                     # 全部导出
  python3 scripts/export.py --skip-per-symbol   # 跳过 daily/ 按标的导出（耗时项 ~3-5 分钟）
  python3 scripts/export.py --full              # 季度打包也全量重导（默认增量）

DSN：默认 /tmp socket（与其他脚本一致），可用 PG_EXPORT_DSN 覆盖。
失败时非零退出（供 pipeline.py 检查）。
"""

import argparse
import csv
import os
import sys
from pathlib import Path
from datetime import date, timedelta

import psycopg2

REPO = Path(os.environ.get("STOCK_DATA_REPO", Path(__file__).resolve().parent.parent))
DATA = REPO / "data"
DAILY_DIR = REPO / "daily"
INDEX_DIR = REPO / "index"

QUARTERS = [(1, 3), (4, 6), (7, 9), (10, 12)]

PER_SYMBOL_HEADER = ["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"]
INDEX_HEADER = ["trade_date", "open", "high", "low", "close", "pre_close", "pct_chg", "vol"]

failed = False


def get_conn():
    dsn = os.environ.get("PG_EXPORT_DSN", "")
    if dsn:
        return psycopg2.connect(dsn)
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "/tmp"),
        dbname=os.environ.get("PGDATABASE", "investassist"),
        user=os.environ.get("PGUSER", "james"),
        password=os.environ.get("PGPASSWORD", ""),
        connect_timeout=5,
    )


def _num(v):
    """数值格式化：None → ''；Decimal → float（与历史 CSV 的 float repr 一致）。"""
    if v is None:
        return ""
    if isinstance(v, (int, float)):
        return v
    try:
        return float(v)
    except (TypeError, ValueError):
        return v


def _fail(msg: str):
    global failed
    failed = True
    print(f"  ❌ {msg}", file=sys.stderr)


# ═════════════════════════════════════════════════════════════════════════════
# 按标的导出 daily/<symbol>.csv（全量重写，流式）
# ═════════════════════════════════════════════════════════════════════════════

def export_per_symbol(conn):
    """daily_quote → daily/<ts_code>.csv，全量重写单文件。

    server-side cursor 流式读取（~2340 万行），按 ts_code 分组写文件；
    先写 .tmp 再原子 rename，中断不留半写文件。
    """
    print("\n=== DAILY (per-symbol, 全量重写) ===")
    DAILY_DIR.mkdir(parents=True, exist_ok=True)

    cur = conn.cursor(name="per_symbol_export")
    cur.itersize = 50000
    cur.execute("""
        SELECT ts_code, trade_date, open, high, low, close, vol, amount
        FROM daily_quote
        ORDER BY ts_code, trade_date
    """)

    n_files = 0
    n_rows = 0
    cur_sym = None
    f = w = tmp_path = out_path = None

    def _close_file():
        nonlocal f, w
        if f:
            f.close()
            os.replace(tmp_path, out_path)
            f = w = None

    try:
        for sym, d, o, h, l, c, vol, amount in cur:
            if sym != cur_sym:
                _close_file()
                cur_sym = sym
                out_path = DAILY_DIR / f"{sym}.csv"
                tmp_path = DAILY_DIR / f".{sym}.csv.tmp"
                f = open(tmp_path, "w", newline="")
                w = csv.writer(f)
                w.writerow(PER_SYMBOL_HEADER)
                n_files += 1
            d_str = d.isoformat() if hasattr(d, "isoformat") else str(d)
            w.writerow([sym, d_str, _num(o), _num(h), _num(l), _num(c), _num(vol), _num(amount)])
            n_rows += 1
        _close_file()
        print(f"  daily/: {n_files} 个文件, {n_rows:,} 行")
    except Exception as e:
        _fail(f"daily/ 按标的导出失败: {e}")
        if f:
            f.close()
            if tmp_path and tmp_path.exists():
                tmp_path.unlink()
    finally:
        cur.close()


# ═════════════════════════════════════════════════════════════════════════════
# 指数导出 index/<symbol>.csv
# ═════════════════════════════════════════════════════════════════════════════

def export_index(conn):
    """index_daily → index/<symbol>.csv，按 symbol 全量重写。

    保护：若 PG 行数 < 现有 CSV 行数（如 HKTECH/HSHKCI PG 数据反而少），
    保留现有 CSV 并告警，避免覆盖丢数据。
    """
    print("\n=== INDEX ===")
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    cur = conn.cursor()
    cur.execute("SELECT symbol, COUNT(*) FROM index_daily GROUP BY symbol ORDER BY symbol")
    symbols = cur.fetchall()

    for symbol, pg_count in symbols:
        csv_path = INDEX_DIR / f"{symbol}.csv"
        if csv_path.exists():
            with open(csv_path) as f:
                csv_rows = sum(1 for _ in f) - 1
            if pg_count < csv_rows:
                print(f"  {symbol}: SKIP (PG {pg_count} 行 < CSV {csv_rows} 行, 保留现有文件)")
                continue

        cur.execute("""
            SELECT trade_date, open, high, low, close, pre_close, pct_chg, volume
            FROM index_daily WHERE symbol = %s ORDER BY trade_date
        """, (symbol,))
        rows = cur.fetchall()
        if not rows:
            continue
        tmp_path = INDEX_DIR / f".{symbol}.csv.tmp"
        with open(tmp_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(INDEX_HEADER)
            for d, o, h, l, c, pre, pct, vol in rows:
                d_str = d.isoformat() if hasattr(d, "isoformat") else str(d)
                w.writerow([d_str, _num(o), _num(h), _num(l), _num(c), _num(pre), _num(pct), _num(vol)])
        os.replace(tmp_path, csv_path)
        print(f"  {symbol}: {len(rows):,} 行 ({rows[0][0]} ~ {rows[-1][0]})")

    cur.close()
    print("  (PG 无对应数据的旧文件如 DJI/SPX 等保留不动)")


# ═════════════════════════════════════════════════════════════════════════════
# 季度打包（原有功能）
# ═════════════════════════════════════════════════════════════════════════════

def _write_csv(path: Path, rows, header: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def export_daily(conn, table: str, dir_name: str, ts_col: str, cols: list[str],
                 filter_cond: str = "", full: bool = False):
    """季度打包导出（增量：只重导当前/上一季度，其余跳过）。"""
    filter_clause = f"WHERE {filter_cond}" if filter_cond else ""
    filter_and = f"AND {filter_cond}" if filter_cond else ""
    out_dir = DATA / "daily" / dir_name
    out_dir.mkdir(parents=True, exist_ok=True)
    cur = conn.cursor()

    cur.execute(f"SELECT MIN(EXTRACT(YEAR FROM {ts_col})), MAX(EXTRACT(YEAR FROM {ts_col})) FROM {table} {filter_clause}")
    min_y, max_y = cur.fetchone()
    if min_y is None:
        print(f"  {dir_name}: NO DATA — skipping")
        return

    col_csv = ", ".join(f'"{c}"' for c in cols)
    total_rows, skipped, exported = 0, 0, 0
    years = range(int(min_y), int(max_y) + 1)
    today = date.today()

    for year in years:
        for qi, (qm_start, qm_end) in enumerate(QUARTERS, 1):
            child = f"{table}_{year}"
            cur.execute(f"SELECT 1 FROM information_schema.tables WHERE table_name='{child}'")
            src = child if cur.fetchone() else table

            start = f"{year}-{qm_start:02d}-01"
            if qm_end == 12:
                end = f"{year}-12-31"
            else:
                end = f"{year}-{qm_end + 1:02d}-01"

            csv_path = out_dir / f"{year}-Q{qi}.csv"

            if not full and csv_path.exists():
                quarter_end = date(year, qm_end, 1) + timedelta(days=31)
                quarter_end = quarter_end.replace(day=1) - timedelta(days=1)
                grace_end = quarter_end + timedelta(days=30)
                if today > grace_end:
                    skipped += 1
                    continue

            cur.execute(f"""
                SELECT COUNT(*) FROM {src}
                WHERE {ts_col} >= '{start}' AND {ts_col} <= '{end}' {filter_and}
            """)
            count = cur.fetchone()[0]
            if count == 0:
                continue

            cur.execute(f"""
                SELECT {col_csv} FROM {src}
                WHERE {ts_col} >= '{start}' AND {ts_col} <= '{end}' {filter_and}
                ORDER BY {ts_col}
            """)
            _write_csv(csv_path, cur.fetchall(), cols)
            total_rows += count
            exported += 1
            print(f"  {dir_name}/{year}-Q{qi}: {count:>10,} rows")

    print(f"  {dir_name}: {total_rows:,} rows ({exported} exported, {skipped} skipped)")
    cur.close()


def _export_table(conn, pg_table: str, csv_path: Path):
    """整表导出为单 CSV，覆盖写。"""
    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(*) FROM {pg_table}")
    count = cur.fetchone()[0]
    if count == 0:
        print(f"  {csv_path.name}: EMPTY — skipping")
        return
    cur.execute(f"SELECT * FROM {pg_table} ORDER BY 1, 2")
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    _write_csv(csv_path, cur.fetchall(), [d[0] for d in cur.description])
    print(f"  {csv_path.name}: {count:,} rows")
    cur.close()


# ═════════════════════════════════════════════════════════════════════════════
# 主入口
# ═════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="季度打包全量重导（默认增量）")
    parser.add_argument("--skip-per-symbol", action="store_true", help="跳过 daily/ 按标的导出（耗时项）")
    parser.add_argument("--skip-index", action="store_true", help="跳过 index/ 指数导出")
    args = parser.parse_args()

    print(f"Mode: {'FULL' if args.full else 'INCREMENTAL'}")
    try:
        conn = get_conn()
    except Exception as e:
        print(f"❌ PostgreSQL 连接失败: {e}", file=sys.stderr)
        sys.exit(1)

    # ── 按标的 daily/ ──
    if args.skip_per_symbol:
        print("\n=== DAILY (per-symbol): SKIP ===")
    else:
        export_per_symbol(conn)

    # ── 指数 index/ ──
    if args.skip_index:
        print("\n=== INDEX: SKIP ===")
    else:
        export_index(conn)

    # ── 季度打包 data/daily/ ──
    print("\n=== QUARTERLY PACKS ===")
    export_daily(conn, "daily_quote", "a_shares", "trade_date",
                 ["ts_code", "trade_date", "open", "high", "low", "close",
                  "pre_close", "vol", "amount", "pct_chg"], full=args.full)
    export_daily(conn, "etf_quote", "etf", "trade_date",
                 ["code", "trade_date", "open", "high", "low", "close",
                  "pre_close", "vol", "amount"], full=args.full)
    export_daily(conn, "daily_quote", "hk", "trade_date",
                 ["ts_code", "trade_date", "open", "high", "low", "close",
                  "pre_close", "vol", "amount", "pct_chg"],
                 filter_cond="ts_code LIKE '%.HK'", full=args.full)

    # ── Fundamental ──
    print("\n=== FUNDAMENTAL ===")
    _export_table(conn, "income", DATA / "fundamental" / "income_stmt.csv")
    _export_table(conn, "balance_sheet", DATA / "fundamental" / "balance_sheet.csv")
    _export_table(conn, "cashflow", DATA / "fundamental" / "cashflow.csv")
    _export_table(conn, "financial_indicator", DATA / "fundamental" / "financial_indicator.csv")

    # ── Meta ──
    print("\n=== META ===")
    _export_table(conn, "stocks", DATA / "meta" / "stock_basic.csv")
    _export_table(conn, "trade_cal", DATA / "meta" / "trade_cal.csv")
    _export_table(conn, "etf", DATA / "meta" / "etf_basic.csv")

    conn.close()
    if failed:
        print("\n❌ Done with ERRORS", file=sys.stderr)
        sys.exit(1)
    print(f"\n✅ Done. Data in {DATA}/ & {DAILY_DIR}/ & {INDEX_DIR}/")


if __name__ == "__main__":
    main()

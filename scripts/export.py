#!/usr/bin/env python3
"""Export data from PostgreSQL → stock_data CSV format (quarterly).

Usage:
  cd ~/workspace/stock_data
  python3 scripts/export.py
"""

import csv, os, sys
from pathlib import Path
from datetime import date

import psycopg2
import psycopg2.extras

DSN = os.environ.get("PG_EXPORT_DSN", "postgresql://james@localhost:5432/investassist")
REPO = Path(os.environ.get("STOCK_DATA_REPO", os.path.expanduser("~/workspace/stock_data")))
DATA = REPO / "data"

QUARTERS = [(1, 3), (4, 6), (7, 9), (10, 12)]


def _write_csv(path: Path, rows, header: list[str]):
    """Write rows to CSV with header."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def export_daily(conn, table: str, dir_name: str, ts_col: str, cols: list[str], 
                 where: str = ""):
    """Export daily data as quarterly CSVs. Reads from PG partitioned table.

    Args:
        table: PG table name (e.g. 'daily_quote' — parent of year partitions).
        dir_name: Output subdirectory (e.g. 'a_shares').
        ts_col: Timestamp/date column for ordering.
        cols: Columns to export.
        where: Optional WHERE clause (e.g. "market='A'").
    """
    out_dir = DATA / "daily" / dir_name
    out_dir.mkdir(parents=True, exist_ok=True)

    # Get min/max year range from the parent table
    cur = conn.cursor()
    cur.execute(f"SELECT MIN(EXTRACT(YEAR FROM {ts_col})), MAX(EXTRACT(YEAR FROM {ts_col})) FROM {table} {where}")
    min_y, max_y = cur.fetchone()
    if min_y is None:
        print(f"  {dir_name}: NO DATA — skipping")
        return

    years = range(int(min_y), int(max_y) + 1)
    col_csv = ", ".join(f'"{c}"' for c in cols)
    total_rows = 0

    for year in years:
        for qi, (qm_start, qm_end) in enumerate(QUARTERS, 1):
            # Use the year-partitioned child table for performance
            child = f"{table}_{year}"
            cur.execute(f"SELECT 1 FROM information_schema.tables WHERE table_name='{child}'")
            src = child if cur.fetchone() else table

            start = f"{year}-{qm_start:02d}-01"
            if qm_end == 12:
                end = f"{year}-12-31"
            else:
                end = f"{year}-{qm_end+1:02d}-01"

            cur.execute(f"""
                SELECT COUNT(*) FROM {src}
                WHERE {ts_col} >= '{start}' AND {ts_col} < '{end}' {where}
            """)
            count = cur.fetchone()[0]
            if count == 0:
                continue

            csv_path = out_dir / f"{year}-Q{qi}.csv"
            cur.execute(f"""
                SELECT {col_csv} FROM {src}
                WHERE {ts_col} >= '{start}' AND {ts_col} < '{end}' {where}
                ORDER BY {ts_col}
            """)
            rows = cur.fetchall()
            _write_csv(csv_path, rows, cols)
            total_rows += count
            sz = csv_path.stat().st_size
            print(f"  {year}-Q{qi}: {count:>10,} rows  {sz:>12,} bytes")

    print(f"  {dir_name}: {total_rows:,} total rows ({years[0]}–{years[-1]})")


def _export_table(conn, pg_table: str, csv_path: Path):
    """Export a table as a single CSV file."""
    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(*) FROM {pg_table}")
    count = cur.fetchone()[0]
    if count == 0:
        print(f"  {csv_path.name}: EMPTY — skipping")
        return

    cur.execute(f"SELECT * FROM {pg_table} ORDER BY 1, 2")
    rows = cur.fetchall()
    header = [d[0] for d in cur.description]
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    _write_csv(csv_path, rows, header)
    print(f"  {csv_path.name}: {count:,} rows")


def main():
    print(f"DSN: {DSN}")
    print(f"Repo: {REPO}\n")
    conn = psycopg2.connect(DSN)

    # ── Daily data ──
    print("=== DAILY ===")
    export_daily(conn, "daily_quote", "a_shares", "trade_date",
                 ["ts_code", "trade_date", "open", "high", "low", "close",
                  "pre_close", "vol", "amount", "pct_chg"])

    export_daily(conn, "etf_quote", "etf", "trade_date",
                 ["code", "trade_date", "open", "high", "low", "close",
                  "pre_close", "vol", "amount"])

    print("\n  hk: SKIP (hk_quote has 0 rows in PG)")

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
    print("  hk_basic: SKIP (no hk_basic table in PG)")

    # ── Macro ──
    print("\n=== MACRO ===")
    print("  SKIP: macro data is Parquet-only (not in PG per design)")

    conn.close()
    print(f"\n✅ Done. Data in {DATA}/")


if __name__ == "__main__":
    main()

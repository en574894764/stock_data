#!/usr/bin/env python3
"""Export data from PostgreSQL → stock_data CSV format (quarterly).

Default: incremental — only exports new/current quarters.
With --full: re-export everything from scratch.

Usage:
  python3 scripts/export.py           # incremental (weekly cron)
  python3 scripts/export.py --full    # full rebuild
"""

import argparse, csv, os, sys
from pathlib import Path
from datetime import date, timedelta

import psycopg2

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
                 where: str = "", full: bool = False):
    """Export daily data as quarterly CSVs from PG partitioned table.

    Incremental mode (default): only exports the current and previous quarter.
    Past quarters are skipped if their CSV already exists.
    --full: re-exports every quarter.
    """
    out_dir = DATA / "daily" / dir_name
    out_dir.mkdir(parents=True, exist_ok=True)
    cur = conn.cursor()

    cur.execute(f"SELECT MIN(EXTRACT(YEAR FROM {ts_col})), MAX(EXTRACT(YEAR FROM {ts_col})) FROM {table} {where}")
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
            end = f"{year}-{qm_end:02d}-28"
            if qm_end == 12:
                end = f"{year}-12-31"
            else:
                q_next = qm_end + 1
                end = f"{year}-{q_next:02d}-01"

            csv_path = out_dir / f"{year}-Q{qi}.csv"

            # ── Incremental skip logic ──
            if not full and csv_path.exists():
                # Current quarter: always re-export (new days added)
                # Previous quarter: re-export for 30 days after quarter end (corrections)
                quarter_end = date(year, qm_end, 1) + timedelta(days=31)
                quarter_end = quarter_end.replace(day=1) - timedelta(days=1)
                grace_end = quarter_end + timedelta(days=30)

                if today <= grace_end:
                    pass  # re-export below
                else:
                    skipped += 1
                    continue

            cur.execute(f"""
                SELECT COUNT(*) FROM {src}
                WHERE {ts_col} >= '{start}' AND {ts_col} < '{end}' {where}
            """)
            count = cur.fetchone()[0]
            if count == 0:
                continue

            cur.execute(f"""
                SELECT {col_csv} FROM {src}
                WHERE {ts_col} >= '{start}' AND {ts_col} < '{end}' {where}
                ORDER BY {ts_col}
            """)
            _write_csv(csv_path, cur.fetchall(), cols)
            total_rows += count
            exported += 1
            sz = csv_path.stat().st_size
            print(f"  {year}-Q{qi}: {count:>10,} rows  {sz:>12,} bytes")

    print(f"  {dir_name}: {total_rows:,} rows ({exported} exported, {skipped} skipped)")


def _export_table(conn, pg_table: str, csv_path: Path):
    """Export a table as a single CSV file. Always overwrites."""
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="Full rebuild")
    args = parser.parse_args()

    print(f"Mode: {'FULL' if args.full else 'INCREMENTAL'}")
    print(f"DSN: {DSN}\n")
    conn = psycopg2.connect(DSN)

    # ── Daily ──
    print("=== DAILY ===")
    export_daily(conn, "daily_quote", "a_shares", "trade_date",
                 ["ts_code", "trade_date", "open", "high", "low", "close",
                  "pre_close", "vol", "amount", "pct_chg"], full=args.full)
    export_daily(conn, "etf_quote", "etf", "trade_date",
                 ["code", "trade_date", "open", "high", "low", "close",
                  "pre_close", "vol", "amount"], full=args.full)
    print("  hk: SKIP (hk_quote has 0 rows in PG)")

    # ── Fundamental (always overwrite — reference data) ──
    print("\n=== FUNDAMENTAL ===")
    _export_table(conn, "income", DATA / "fundamental" / "income_stmt.csv")
    _export_table(conn, "balance_sheet", DATA / "fundamental" / "balance_sheet.csv")
    _export_table(conn, "cashflow", DATA / "fundamental" / "cashflow.csv")
    _export_table(conn, "financial_indicator", DATA / "fundamental" / "financial_indicator.csv")

    # ── Meta (small, always overwrite) ──
    print("\n=== META ===")
    _export_table(conn, "stocks", DATA / "meta" / "stock_basic.csv")
    _export_table(conn, "trade_cal", DATA / "meta" / "trade_cal.csv")
    _export_table(conn, "etf", DATA / "meta" / "etf_basic.csv")
    print("  hk_basic: SKIP (no hk_basic table in PG)")

    # ── Macro ──
    print("\n=== MACRO ===  SKIP (Parquet-only, not in PG)")

    conn.close()
    print(f"\n✅ Done. Data in {DATA}/")


if __name__ == "__main__":
    main()

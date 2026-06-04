#!/usr/bin/env python3
"""Restore data from stock_data CSV → DuckDB/Parquet for querying.

Usage:
  python restore.py                    # interactive DuckDB query
  python restore.py --export-parquet   # export all to Parquet
"""

import duckdb, os, sys
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"

def restore_all(con):
    """Register all CSV files as DuckDB views/tables."""
    
    # Daily data (quarterly CSV)
    for market in ["a_shares", "hk", "etf"]:
        daily_dir = DATA_DIR / "daily" / market
        if daily_dir.exists():
            files = sorted(daily_dir.glob("*-Q*.csv"))
            if files:
                paths = [str(f) for f in files]
                con.execute(f"""
                    CREATE OR REPLACE VIEW {market}_daily AS 
                    SELECT * FROM read_csv({paths}, union_by_name=true, auto_detect=true)
                """)
                n = con.execute(f"SELECT COUNT(*) FROM {market}_daily").fetchone()[0]
                print(f"  {market}_daily: {n:,} rows ({len(files)} year files)")

    # Fundamental tables (flat CSV)
    for table in ["income_stmt", "balance_sheet", "cashflow", "financial_indicator"]:
        fp = DATA_DIR / "fundamental" / f"{table}.csv"
        if fp.exists():
            con.execute(f"""
                CREATE OR REPLACE TABLE {table} AS 
                SELECT * FROM read_csv('{fp}', auto_detect=true)
            """)
            n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  {table}: {n:,} rows")

    # Meta tables
    for meta_name, table_name in [
        ("stock_basic", "stock_basic"), ("trade_cal", "trade_cal"),
        ("etf_basic", "etf_basic"), ("hk_basic", "hk_basic")
    ]:
        fp = DATA_DIR / "meta" / f"{meta_name}.csv"
        if fp.exists():
            con.execute(f"""
                CREATE OR REPLACE TABLE {table_name} AS 
                SELECT * FROM read_csv('{fp}', auto_detect=true)
            """)
            n = con.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
            print(f"  {table_name}: {n:,} rows")

    # Macro tables
    for macro_name in ["shibor", "lpr", "cpi", "pmi", "money_supply", "bond_yield_10y"]:
        fp = DATA_DIR / "macro" / f"{macro_name}.csv"
        if fp.exists():
            con.execute(f"""
                CREATE OR REPLACE TABLE {macro_name} AS 
                SELECT * FROM read_csv('{fp}', auto_detect=true)
            """)
            n = con.execute(f"SELECT COUNT(*) FROM {macro_name}").fetchone()[0]
            print(f"  {macro_name}: {n:,} rows")


if __name__ == "__main__":
    con = duckdb.connect()
    print("Restoring data from CSV...")
    restore_all(con)
    print("\n✅ Restore complete. All tables available in DuckDB.")
    print("\nExample queries:")
    print("  SELECT * FROM a_shares_daily LIMIT 5")
    print("  SELECT ts_code, AVG(close) FROM a_shares_daily WHERE trade_date >= '2026-01-01' GROUP BY ts_code")
    
    if "--export-parquet" in sys.argv:
        out = Path("parquet")
        out.mkdir(exist_ok=True)
        for table in con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='main'").fetchall():
            name = table[0]
            con.execute(f"COPY {name} TO '{out/name}.parquet' (FORMAT PARQUET)")
            print(f"  → parquet/{name}.parquet")
    
    if "--interactive" in sys.argv:
        print("\nDuckDB shell. Type 'exit' to quit.")
        import code
        code.interact(local=locals())

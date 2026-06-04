#!/usr/bin/env python3
"""Export data from quant_sys Parquet → stock_data CSV format.

Usage:
  cd ~/workspace/quant_sys
  .venv/bin/python /path/to/stock_data/scripts/export.py
"""

import duckdb, os, sys
from pathlib import Path

QUANT_SYS = os.path.expanduser("~/workspace/quant_sys")
REPO_PATH = os.environ.get("STOCK_DATA_REPO", os.path.expanduser("~/stock_data"))
DATA_DIR = f"{REPO_PATH}/data"

def export():
    con = duckdb.connect()
    base = f"{QUANT_SYS}/data/raw"

    # ── Daily data: one CSV per year ──
    for market, glob_path, dir_name in [
        ("a_shares", f"{base}/a_shares/daily/date=*/data.parquet", "a_shares"),
        ("hk", f"{base}/hk_connect/daily/date=*/data.parquet", "hk"),
        ("etf", f"{base}/etf/daily/date=*/data.parquet", "etf"),
    ]:
        os.makedirs(f"{DATA_DIR}/daily/{dir_name}", exist_ok=True)
        years = con.execute(f"""
            SELECT DISTINCT REGEXP_EXTRACT(filename, 'date=(\\d{{4}})')
            FROM read_parquet('{glob_path}', union_by_name=true, filename=true)
            ORDER BY 1
        """).fetchall()
        
        print(f"\n### {dir_name} ({len(years)} years)")
        for (year,) in years:
            csv_path = f"{DATA_DIR}/daily/{dir_name}/{year}.csv"
            con.execute(f"""
                COPY (
                    SELECT * EXCLUDE(filename)
                    FROM read_parquet('{glob_path}', union_by_name=true, filename=true)
                    WHERE REGEXP_EXTRACT(filename, 'date=(\\d{{4}})') = '{year}'
                    ORDER BY 1, 2
                ) TO '{csv_path}' (HEADER, DELIMITER ',')
            """)
            n = con.execute(f"SELECT COUNT(*) FROM '{csv_path}'").fetchone()[0]
            sz = os.path.getsize(csv_path)
            print(f"  {year}: {n:>10,} rows  {sz:>12,} bytes")

    # ── Fundamental tables ──
    print("\n### Fundamental")
    for f in ["income_stmt", "balance_sheet", "cashflow", "financial_indicator"]:
        src = f"{base}/a_shares/fundamental/{f}.parquet"
        dst = f"{DATA_DIR}/fundamental/{f}.csv"
        os.makedirs(f"{DATA_DIR}/fundamental", exist_ok=True)
        con.execute(f"COPY (SELECT * FROM '{src}' ORDER BY 1,2) TO '{dst}' (HEADER, DELIMITER ',')")
        n = con.execute(f"SELECT COUNT(*) FROM '{src}'").fetchone()[0]
        print(f"  {f}: {n:,} rows")

    # ── Meta ──
    print("\n### Meta")
    for src_name, dst_name in [
        ("a_shares/meta/stock_basic", "stock_basic"),
        ("a_shares/meta/trade_cal", "trade_cal"),
        ("etf/meta/fund_basic", "etf_basic"),
        ("hk_connect/meta/hk_basic", "hk_basic"),
    ]:
        src = f"{base}/{src_name}.parquet"
        dst = f"{DATA_DIR}/meta/{dst_name}.csv"
        os.makedirs(f"{DATA_DIR}/meta", exist_ok=True)
        con.execute(f"COPY (SELECT * FROM '{src}' ORDER BY 1) TO '{dst}' (HEADER, DELIMITER ',')")
        n = con.execute(f"SELECT COUNT(*) FROM '{src}'").fetchone()[0]
        print(f"  {dst_name}: {n:,} rows")

    # ── Macro ──
    print("\n### Macro")
    os.makedirs(f"{DATA_DIR}/macro", exist_ok=True)
    for m in ["shibor", "lpr", "cpi", "pmi", "money_supply", "bond_yield_10y"]:
        src = f"{base}/macro/{m}.parquet"
        dst = f"{DATA_DIR}/macro/{m}.csv"
        try:
            con.execute(f"COPY (SELECT * FROM '{src}' ORDER BY 1) TO '{dst}' (HEADER, DELIMITER ',')")
            n = con.execute(f"SELECT COUNT(*) FROM '{src}'").fetchone()[0]
            print(f"  {m}: {n:,} rows")
        except Exception as e:
            print(f"  {m}: SKIP ({e})")

    con.close()
    print(f"\n✅ Done. Data in {DATA_DIR}/")

if __name__ == "__main__":
    export()

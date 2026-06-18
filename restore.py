#!/usr/bin/env python3
"""从 stock_data CSV 还原数据（供换机/重建环境使用）。

使用方式：
  python3 restore.py                    # 验证所有 CSV 完整性并打印摘要
  python3 restore.py --duckdb           # 启动 DuckDB 交互查询
  python3 restore.py --export-parquet   # 导出本地 Parquet 缓存（供 Argus 1d/ 目录）
  python3 restore.py --symbol 600519.SH # 验证单只标的

数据布局（新格式）：
  daily/SYMBOL.csv       — 日线（每标的一文件，append-only）
  macro/INDICATOR.csv    — 宏观数据
  meta/stock_basic.csv   — 股票元信息
  meta/trade_cal.csv     — 交易日历
  fundamental/           — 财报（按标的 CSV，如已迁移）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).parent
DAILY_DIR = REPO / "daily"
MACRO_DIR = REPO / "macro"
META_DIR  = REPO / "meta"
FUND_DIR  = REPO / "fundamental"


# ── 验证 ──────────────────────────────────────────────────────────────────

def check_daily(symbol: str | None = None) -> bool:
    files = sorted(DAILY_DIR.glob("*.csv")) if not symbol else [DAILY_DIR / f"{symbol}.csv"]
    ok = True
    print(f"[日线] {DAILY_DIR}")
    for f in files:
        if not f.exists():
            print(f"  ✗ {f.name} 不存在", file=sys.stderr)
            ok = False
            continue
        try:
            df = pd.read_csv(f, parse_dates=["datetime"])
            required = {"symbol", "datetime", "open", "high", "low", "close", "volume", "amount"}
            missing = required - set(df.columns)
            if missing:
                print(f"  ✗ {f.name}: 缺列 {missing}", file=sys.stderr)
                ok = False
                continue
            if df["datetime"].isna().any():
                print(f"  ✗ {f.name}: 存在 NaT datetime", file=sys.stderr)
                ok = False
                continue
            dmin = str(df["datetime"].min())[:10]
            dmax = str(df["datetime"].max())[:10]
            print(f"  ✓ {f.name}: {len(df):>6} 行  {dmin} ~ {dmax}")
        except Exception as e:
            print(f"  ✗ {f.name}: {e}", file=sys.stderr)
            ok = False
    return ok


def check_macro() -> bool:
    print(f"\n[宏观] {MACRO_DIR}")
    ok = True
    for f in sorted(MACRO_DIR.glob("*.csv")):
        try:
            df = pd.read_csv(f)
            print(f"  ✓ {f.name}: {len(df)} 行")
        except Exception as e:
            print(f"  ✗ {f.name}: {e}", file=sys.stderr)
            ok = False
    return ok


def check_meta() -> bool:
    print(f"\n[元数据] {META_DIR}")
    ok = True
    for f in sorted(META_DIR.glob("*.csv")):
        try:
            df = pd.read_csv(f)
            print(f"  ✓ {f.name}: {len(df)} 行")
        except Exception as e:
            print(f"  ✗ {f.name}: {e}", file=sys.stderr)
            ok = False
    return ok


def check_fundamental() -> bool:
    print(f"\n[财报] {FUND_DIR}")
    ok = True
    if not FUND_DIR.exists():
        print("  (目录不存在，跳过)")
        return True
    for f in sorted(FUND_DIR.glob("*.csv")):
        try:
            df = pd.read_csv(f)
            print(f"  ✓ {f.name}: {len(df)} 行")
        except Exception as e:
            print(f"  ✗ {f.name}: {e}", file=sys.stderr)
            ok = False
    return ok


# ── 导出 Parquet 本地缓存 ──────────────────────────────────────────────────

def export_parquet(out_dir: Path | None = None) -> None:
    """从 daily/*.csv 重建 1d/*.parquet 本地缓存（供 Argus 快速读取）。"""
    out = out_dir or (REPO / "1d")
    out.mkdir(parents=True, exist_ok=True)
    files = sorted(DAILY_DIR.glob("*.csv"))
    print(f"\n[导出 Parquet] {len(files)} 个标的 → {out}")
    for f in files:
        try:
            df = pd.read_csv(f, parse_dates=["datetime"])
            target = out / f.with_suffix(".parquet").name
            df.to_parquet(target, index=False)
            print(f"  → {target.name}: {len(df)} 行")
        except Exception as e:
            print(f"  ✗ {f.name}: {e}", file=sys.stderr)
    print("完成。")


# ── DuckDB 交互 ────────────────────────────────────────────────────────────

def start_duckdb() -> None:
    try:
        import duckdb
    except ImportError:
        print("请先安装 duckdb: pip install duckdb", file=sys.stderr)
        sys.exit(1)

    con = duckdb.connect()

    # 注册 daily 视图
    csv_files = list(DAILY_DIR.glob("*.csv"))
    if csv_files:
        paths = [str(f) for f in csv_files]
        con.execute(f"CREATE VIEW daily AS SELECT * FROM read_csv({paths!r}, union_by_name=true, auto_detect=true)")
        n = con.execute("SELECT COUNT(*) FROM daily").fetchone()[0]
        print(f"  daily: {n:,} 行（{len(csv_files)} 个标的）")

    # 注册宏观
    for f in MACRO_DIR.glob("*.csv"):
        name = f.stem.replace("-", "_")
        con.execute(f"CREATE VIEW {name} AS SELECT * FROM read_csv('{f}', auto_detect=true)")
        print(f"  {name}: 已注册")

    # 注册元数据
    for f in META_DIR.glob("*.csv"):
        name = f.stem.replace("-", "_")
        con.execute(f"CREATE VIEW {name} AS SELECT * FROM read_csv('{f}', auto_detect=true)")
        print(f"  {name}: 已注册")

    print("\n可用视图: daily, lpr, bond_yield_10y, pmi, money_supply, shibor, stock_basic, trade_cal")
    print("示例: SELECT * FROM daily WHERE symbol='600519.SH' ORDER BY datetime DESC LIMIT 5\n")

    import code
    code.interact(local={"con": con, "pd": pd}, banner="DuckDB 交互模式（输入 exit() 退出）")


# ── 主入口 ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="stock_data 验证与还原工具")
    parser.add_argument("--symbol", help="只检查指定标的（如 600519.SH）")
    parser.add_argument("--duckdb", action="store_true", help="启动 DuckDB 交互查询")
    parser.add_argument("--export-parquet", action="store_true", help="导出 1d/ Parquet 缓存")
    parser.add_argument("--out", help="导出 Parquet 的目标目录（默认 ./1d）")
    args = parser.parse_args()

    if args.duckdb:
        start_duckdb()
        sys.exit(0)

    if args.export_parquet:
        export_parquet(Path(args.out) if args.out else None)
        sys.exit(0)

    print("=== stock_data 完整性验证 ===\n")
    results = [
        check_daily(args.symbol),
        check_macro(),
        check_meta(),
        check_fundamental(),
    ]

    print("\n" + ("✅ 所有检查通过" if all(results) else "❌ 存在问题，见上方错误"))
    sys.exit(0 if all(results) else 1)

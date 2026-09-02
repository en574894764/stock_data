#!/usr/bin/env python3
"""因子计算引擎 → PG factor_value 表

约定: 所有因子 value 越大 = 预期收益越高（方向已调好）。

因子清单 (6 个原生因子):
  ret_20d_rev   20日反转: -(过去20日累计收益)。高=近期跌得多=预期反弹强
  turnover_20   -mean(20日换手率)。低换手溢价, 取负号
  ln_mv         -ln(总市值)。小市值溢价, 取负号 (total_mv 单位: 万元)
  ivol_60       -60日特质波动率(市场模型残差std, 基准=沪深300)。低波溢价, 取负号
  ep_ttm        1/pe_ttm。EP 价值因子, 亏损股为负(有意义, 保留)
  roe_lf        PIT 对齐的最近已披露 ROE (ann_date <= T, financial_indicator)

数据源: daily_quote(pct_chg) / daily_basic / index_daily(000300.SH) / financial_indicator
注意: 收益计算全部基于 pct_chg 连乘 (复权口径), 不用 close 直接相除 (除权会错)

用法:
  python3 scripts/compute_factors.py --start 2015-01-01   # 全量
  python3 scripts/compute_factors.py                      # 增量: 补最近 10 个交易日
"""
import argparse
import os
import sys
from datetime import date

import numpy as np
import pandas as pd
import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FACTORS = ["ret_20d_rev", "turnover_20", "ln_mv", "ivol_60", "ep_ttm", "roe_lf"]

FACTOR_DDL = """
CREATE TABLE IF NOT EXISTS factor_value (
    trade_date  date         NOT NULL,
    ts_code     varchar(12)  NOT NULL,
    factor_name varchar(32)  NOT NULL,
    value       float8,
    PRIMARY KEY (factor_name, trade_date, ts_code)
);
CREATE INDEX IF NOT EXISTS idx_factor_date ON factor_value (trade_date);
"""


def get_conn():
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "/tmp"),
        port=os.environ.get("PGPORT", "5432"),
        dbname=os.environ.get("PGDATABASE", "investassist"),
        user=os.environ.get("PGUSER", "james"),
        password=os.environ.get("PGPASSWORD", ""),
    )


def a_shares_only(df_wide_index_cols: pd.Index) -> pd.Index:
    """只保留沪深 A 股 (排除 .BJ / .HK / 指数 / 退市变体)。"""
    return pd.Index([c for c in df_wide_index_cols
                     if (c.endswith(".SZ") or c.endswith(".SH")) and not c.startswith("T")])


def load_daily_wide(conn, start: str) -> pd.DataFrame:
    """daily_quote A股 pct_chg → 宽表 (index=date, columns=ts_code), 值=日收益(小数)"""
    sql = ("SELECT ts_code, trade_date, pct_chg FROM daily_quote "
           f"WHERE trade_date >= '{start}' AND (ts_code LIKE '%%.SZ' OR ts_code LIKE '%%.SH')")
    df = pd.read_sql(sql, conn)
    df["pct_chg"] = pd.to_numeric(df["pct_chg"], errors="coerce")
    df["ret"] = df["pct_chg"] / 100.0
    wide = df.pivot_table(index="trade_date", columns="ts_code", values="ret", aggfunc="last")
    wide = wide.sort_index()
    return wide


def load_market_ret(conn, start: str) -> pd.Series:
    # 注意: index_daily.pct_chg 历史大量为 NULL, 用 close 自算 (指数无除权, 直接相除即可)
    sql = ("SELECT symbol, trade_date, close FROM index_daily "
           f"WHERE symbol='000300.SH' AND trade_date >= '{start}'")
    df = pd.read_sql(sql, conn)
    s = df.set_index("trade_date")["close"].sort_index().astype(float)
    return s / s.shift(1) - 1.0


def load_basic_wide(conn, start: str, col: str) -> pd.DataFrame:
    sql = (f"SELECT ts_code, trade_date, {col} FROM daily_basic "
           f"WHERE trade_date >= '{start}' AND (ts_code LIKE '%%.SZ' OR ts_code LIKE '%%.SH')")
    df = pd.read_sql(sql, conn)
    df[col] = pd.to_numeric(df[col], errors="coerce")
    wide = df.pivot_table(index="trade_date", columns="ts_code", values=col, aggfunc="last")
    return wide.sort_index()


def ret_20d_rev(ret_wide: pd.DataFrame) -> pd.DataFrame:
    logret = np.log1p(ret_wide.clip(-0.95, 10))
    cum = logret.cumsum()
    r20 = np.exp(cum - cum.shift(20)) - 1.0
    return -r20  # 反转: 取负, 跌得多 → 值大


def turnover_20(basic_wide: pd.DataFrame) -> pd.DataFrame:
    return -basic_wide.rolling(20, min_periods=15).mean()


def ln_mv(basic_wide: pd.DataFrame) -> pd.DataFrame:
    return -np.log(basic_wide.where(basic_wide > 0))


def ep_ttm(basic_wide: pd.DataFrame) -> pd.DataFrame:
    return 1.0 / basic_wide  # pe_ttm 可能负(亏损), 1/pe 保留符号


def ivol_60(ret_wide: pd.DataFrame, mkt: pd.Series) -> pd.DataFrame:
    """特质波动: beta=rolling_cov(r,rm)/rolling_var(rm), 残差 rolling std"""
    rm = mkt.reindex(ret_wide.index)
    rm_var = rm.rolling(60, min_periods=40).var()

    # E[r*rm], E[rm^2] 路线算 rolling beta, 避免 DataFrame.rolling.cov(Series) 的兼容问题
    r_x_rm = ret_wide.mul(rm, axis=0)
    E_rxrm = r_x_rm.rolling(60, min_periods=40).mean()
    E_r = ret_wide.rolling(60, min_periods=40).mean()
    E_rm = rm.rolling(60, min_periods=40).mean()
    E_rm2 = (rm * rm).rolling(60, min_periods=40).mean()

    cov = E_rxrm - E_r.mul(E_rm, axis=0)
    var_m = E_rm2 - E_rm ** 2
    beta = cov.div(var_m, axis=0)

    resid = ret_wide.sub(beta.mul(rm, axis=0), axis=0)
    ivol = resid.rolling(60, min_periods=40).std()
    return -ivol  # 低波溢价: 取负


def roe_lf(conn, start: str, dates: pd.Index) -> pd.DataFrame:
    """PIT 对齐 ROE: 每个交易日取 ann_date <= T 的最近一期披露 roe (report_type='1' 合并报表优先)"""
    sql = ("SELECT ts_code, ann_date, report_year, report_type, roe FROM financial_indicator "
           "WHERE ann_date IS NOT NULL AND roe IS NOT NULL AND report_type='1' "
           "AND (ts_code LIKE '%.SZ' OR ts_code LIKE '%.SH')")
    ev = pd.read_sql(sql, conn)
    ev["ann_date"] = pd.to_datetime(ev["ann_date"])
    ev = ev.sort_values(["ts_code", "ann_date"])
    ev = ev.drop_duplicates(["ts_code", "ann_date"], keep="last")

    ts_idx = pd.to_datetime(pd.Index(dates))
    out = pd.DataFrame(index=dates, columns=ev["ts_code"].unique(), dtype=float)
    for code, g in ev.groupby("ts_code"):
        pos = np.searchsorted(g["ann_date"].values, ts_idx.values, side="right") - 1
        valid = pos >= 0
        vals = np.where(valid, g["roe"].values[np.clip(pos, 0, len(g) - 1)], np.nan)
        out[code] = vals
    return out.astype(float)


def upsert_factor(conn, name: str, wide: pd.DataFrame, only_dates=None):
    df = wide.stack().rename("value").reset_index()
    df.columns = ["trade_date", "ts_code", "value"]
    if only_dates is not None:
        df = df[df["trade_date"].isin(only_dates)]
    df = df.dropna(subset=["value"])
    df = df[np.isfinite(df["value"].astype(float))]
    if df.empty:
        return 0

    # COPY 快路径: 落盘 CSV → COPY 进 staging → INSERT ... ON CONFLICT
    # (executemany 千万行需要小时级, COPY 只需分钟级)
    import csv as _csv
    import tempfile
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS _staging_factor (LIKE factor_value INCLUDING DEFAULTS)")
    cur.execute("TRUNCATE _staging_factor")
    n = 0
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as tf:
        path = tf.name
        w = _csv.writer(tf, lineterminator="\n")
        for r in df.itertuples(index=False):
            w.writerow((r.ts_code, r.trade_date, name, repr(float(r.value))))
            n += 1
    try:
        with open(path) as f:
            cur.copy_expert("COPY _staging_factor (ts_code, trade_date, factor_name, value) FROM STDIN WITH CSV", f)
        cur.execute("INSERT INTO factor_value SELECT * FROM _staging_factor "
                    "ON CONFLICT (factor_name, trade_date, ts_code) DO UPDATE SET value=EXCLUDED.value")
        conn.commit()
    finally:
        os.unlink(path)
    cur.close()
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None, help="全量起始日 (默认增量模式: 最近10个交易日)")
    args = ap.parse_args()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(FACTOR_DDL)
    conn.commit()
    cur.close()

    if args.start:
        start = args.start
        only_dates = None
    else:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT trade_date FROM daily_quote "
                    "WHERE ts_code LIKE '%.SZ' OR ts_code LIKE '%.SH' ORDER BY trade_date DESC LIMIT 10")
        recent = [r[0] for r in cur.fetchall()]
        cur.close()
        if not recent:
            print("daily_quote 无数据"); return
        # 增量模式: 滚动窗口需要前置历史 (60日窗口 + 余量), 数据多载 400 天, 只回写最近 10 日
        load_start = (min(recent) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
        only_dates = set(recent)
        print(f"增量模式: 回写 {min(recent)} ~ {max(recent)} (数据从 {load_start} 载入供滚动窗口)")
        start = load_start

    print(f"加载数据 (start={start})...")
    ret_wide = load_daily_wide(conn, start)
    mkt = load_market_ret(conn, start)
    print(f"  daily_quote: {ret_wide.shape[1]} 只 × {len(ret_wide)} 日")

    # warmup: 滚动窗口需要前置数据, 多载 1 年
    print("计算因子...")
    n = 0
    n += upsert_factor(conn, "ret_20d_rev", ret_20d_rev(ret_wide), only_dates)
    print(f"  ret_20d_rev: {n}")

    db_turn = load_basic_wide(conn, start, "turnover_rate")
    db_mv = load_basic_wide(conn, start, "total_mv")
    db_pe = load_basic_wide(conn, start, "pe_ttm")

    n = upsert_factor(conn, "turnover_20", turnover_20(db_turn), only_dates)
    print(f"  turnover_20: {n}")
    n = upsert_factor(conn, "ln_mv", ln_mv(db_mv), only_dates)
    print(f"  ln_mv: {n}")
    n = upsert_factor(conn, "ep_ttm", ep_ttm(db_pe), only_dates)
    print(f"  ep_ttm: {n}")

    n = upsert_factor(conn, "ivol_60", ivol_60(ret_wide, mkt), only_dates)
    print(f"  ivol_60: {n}")

    n = upsert_factor(conn, "roe_lf", roe_lf(conn, start, ret_wide.index), only_dates)
    print(f"  roe_lf: {n}")

    cur = conn.cursor()
    cur.execute("SELECT factor_name, COUNT(*), MIN(trade_date), MAX(trade_date) "
                "FROM factor_value GROUP BY factor_name ORDER BY factor_name")
    print("\n=== factor_value 现状 ===")
    for r in cur.fetchall():
        print(f"  {r[0]:<12} {r[1]:>12,} 行  {r[2]} ~ {r[3]}")
    cur.close()
    conn.close()
    print("完成")


if __name__ == "__main__":
    main()

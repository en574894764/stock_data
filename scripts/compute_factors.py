#!/usr/bin/env python3
"""因子计算引擎 → PG factor_value 表

约定: 所有因子 value 越大 = 预期收益越高（方向已调好）。

因子清单 (8 个原生因子):
  ret_20d_rev   20日反转: -(过去20日累计收益)。高=近期跌得多=预期反弹强
  turnover_20   -mean(20日换手率)。低换手溢价, 取负号
  ln_mv         -ln(总市值)。小市值溢价, 取负号 (total_mv 单位: 万元)
  ivol_60       -60日特质波动率(市场模型残差std, 基准=沪深300)。低波溢价, 取负号
  ep_ttm        1/pe_ttm。EP 价值因子, 亏损股为负(有意义, 保留)
  roe_lf        PIT 对齐的最近已披露 ROE (ann_date <= T, financial_indicator)
  sue_gr        PIT 对齐的最新 netprofit_yoy (净利润同比, 成长/盈余惊喜代理)
  sue_delta     同类型财报 netprofit_yoy 的环比变化 (同比加速度, SUE 代理)
  技术指标横截面化 13 个 (见 technical_factors, 均已方向化):
    macd_dif_n / macd_hist_n / rsi_14 / bias_20 / kdj_j / cci_14 / wr_14
    boll_bw_20 / boll_pos_20 / atr_14_n / obv_slope_20 / mfi_14 / ma_ratio_5_20

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

FACTORS = ["ret_20d_rev", "turnover_20", "ln_mv", "ivol_60", "ep_ttm", "roe_lf", "sue_gr", "sue_delta"]

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


def sue_factors(conn, dates: pd.Index) -> tuple:
    """盈余惊喜双因子 (均 PIT 对齐 ann_date <= T):
    sue_gr    最新披露的 netprofit_yoy (成长/惊喜水平)
    sue_delta 最新披露与前一同类型(report_type)披露的 netprofit_yoy 之差 (加速度, SUE 代理)
    """
    sql = ("SELECT ts_code, ann_date, report_type, netprofit_yoy FROM financial_indicator "
           "WHERE ann_date IS NOT NULL AND netprofit_yoy IS NOT NULL "
           "AND (ts_code LIKE '%.SZ' OR ts_code LIKE '%.SH')")
    ev = pd.read_sql(sql, conn)
    ev["ann_date"] = pd.to_datetime(ev["ann_date"])
    # 同类型前值 → 同比加速度
    ev = ev.sort_values(["ts_code", "report_type", "ann_date"])
    ev["prev"] = ev.groupby(["ts_code", "report_type"])["netprofit_yoy"].shift(1)
    ev["delta"] = ev["netprofit_yoy"] - ev["prev"]
    # PIT 对齐: 同日多类型披露取最后一个事件
    ev = ev.sort_values(["ts_code", "ann_date"]).drop_duplicates(["ts_code", "ann_date"], keep="last")
    ts_idx = pd.to_datetime(pd.Index(dates))
    gr = pd.DataFrame(index=dates, columns=ev["ts_code"].unique(), dtype=float)
    dl = pd.DataFrame(index=dates, columns=ev["ts_code"].unique(), dtype=float)
    for code, g in ev.groupby("ts_code"):
        pos = np.searchsorted(g["ann_date"].values, ts_idx.values, side="right") - 1
        valid = pos >= 0
        idx = np.clip(pos, 0, len(g) - 1)
        gr[code] = np.where(valid, g["netprofit_yoy"].values[idx], np.nan)
        dl[code] = np.where(valid, g["delta"].values[idx], np.nan)
    return gr.astype(float), dl.astype(float)


def load_daily_col(conn, start: str, col: str) -> pd.DataFrame:
    """daily_quote 任意列 → 宽表 (index=date, columns=ts_code)"""
    sql = (f"SELECT ts_code, trade_date, {col} FROM daily_quote "
           f"WHERE trade_date >= '{start}' AND (ts_code LIKE '%%.SZ' OR ts_code LIKE '%%.SH')")
    df = pd.read_sql(sql, conn)
    df[col] = pd.to_numeric(df[col], errors="coerce")
    wide = df.pivot_table(index="trade_date", columns="ts_code", values=col, aggfunc="last")
    return wide.sort_index()


def technical_factors(conn, start: str) -> dict:
    """13 个主流技术指标的横截面化版本 (与图表看盘指标同源, 方向已统一调为'越大越好')。

    A股横截面呈反转效应 + 低波溢价, 故动量/摆动/波动类指标统一取负:
      macd_dif_n / macd_hist_n  MACD DIF/柱 除以价格归一
      rsi_14                    RSI(14) 平均涨跌法
      bias_20                   乖离率 (C/MA20-1)
      kdj_j                     KDJ(9,3,3) 的 J
      cci_14 / wr_14            CCI(14) / Williams %R(14)
      boll_bw_20 / boll_pos_20  布林带(20,2) 带宽/%B 位置
      atr_14_n                  ATR(14)/close
      obv_slope_20              OBV 20日增量 / 20日总量
      mfi_14                    MFI(14) 资金流比率
      ma_ratio_5_20             MA5/MA20 均线多排
    """
    C = load_daily_col(conn, start, "close")
    H = load_daily_col(conn, start, "high")
    L = load_daily_col(conn, start, "low")
    V = load_daily_col(conn, start, "vol")
    ret = C.pct_change()

    out = {}
    # MACD(12,26,9)
    ema12 = C.ewm(span=12, adjust=False).mean()
    ema26 = C.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    out["macd_dif_n"] = -(dif / C)
    out["macd_hist_n"] = -((dif - dea) / C)

    # RSI(14)
    up = ret.clip(lower=0).rolling(14, min_periods=10).mean()
    dn = (-ret.clip(upper=0)).rolling(14, min_periods=10).mean()
    out["rsi_14"] = -(100 * up / (up + dn))

    # BIAS(20) + 布林带(20,2)
    ma5 = C.rolling(5, min_periods=4).mean()
    ma20 = C.rolling(20, min_periods=15).mean()
    std20 = C.rolling(20, min_periods=15).std()
    out["bias_20"] = -(C / ma20 - 1.0)
    out["boll_bw_20"] = -(4 * std20 / ma20)
    out["boll_pos_20"] = -((C - (ma20 - 2 * std20)) / (4 * std20))
    out["ma_ratio_5_20"] = -(ma5 / ma20)

    # KDJ(9,3,3)
    llv9, hhv9 = L.rolling(9, min_periods=7).min(), H.rolling(9, min_periods=7).max()
    rsv = 100 * (C - llv9) / (hhv9 - llv9)
    K = rsv.ewm(alpha=1 / 3, adjust=False).mean()
    D = K.ewm(alpha=1 / 3, adjust=False).mean()
    out["kdj_j"] = -(3 * K - 2 * D)

    # CCI(14)
    tp = (H + L + C) / 3.0
    tp_ma = tp.rolling(14, min_periods=10).mean()
    md = (tp - tp_ma).abs().rolling(14, min_periods=10).mean()
    out["cci_14"] = -((tp - tp_ma) / (0.015 * md))

    # Williams %R(14): 原 WR ∈ [-100,0], -100 深度超卖; 取负后 值大=超卖=预期反弹
    hhv14, llv14 = H.rolling(14, min_periods=10).max(), L.rolling(14, min_periods=10).min()
    out["wr_14"] = 100 * (hhv14 - C) / (hhv14 - llv14)

    # ATR(14)/close
    prev_c = C.shift(1)
    tr = np.maximum(H - L, np.maximum((H - prev_c).abs(), (L - prev_c).abs()))
    out["atr_14_n"] = -(tr.rolling(14, min_periods=10).mean() / C)

    # OBV 20日斜率 (按 20 日总量归一)
    obv = (np.sign(ret) * V).fillna(0).cumsum()
    norm = V.rolling(20, min_periods=15).mean() * 20
    out["obv_slope_20"] = -((obv - obv.shift(20)) / norm)

    # MFI(14)
    mf = tp * V
    pos = mf.where(ret > 0, 0.0).rolling(14, min_periods=10).sum()
    neg = mf.where(ret < 0, 0.0).rolling(14, min_periods=10).sum()
    out["mfi_14"] = -(100 * pos / (pos + neg))

    return out


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

    sue_gr_w, sue_dl_w = sue_factors(conn, ret_wide.index)
    n = upsert_factor(conn, "sue_gr", sue_gr_w, only_dates)
    print(f"  sue_gr: {n}")
    n = upsert_factor(conn, "sue_delta", sue_dl_w, only_dates)
    print(f"  sue_delta: {n}")

    print("技术指标因子...")
    for name, w in technical_factors(conn, start).items():
        n = upsert_factor(conn, name, w, only_dates)
        print(f"  {name}: {n}")

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

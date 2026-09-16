#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
价值投资模块 · 公共库
─────────────────────────────────────────────────────────
职责：
  1. 读 config.json（所有阈值可配，脚本里不写死数字）
  2. 连 PostgreSQL（stock_data 主库 investassist，host=/tmp，不走网络）
  3. 提供三个任务共用的取数 / 指标计算函数

口径约定（重要）：
  - 财报一律用年报：report_type = '4'（'1'Q1 / '2'中报 / '3'Q3 / '4'年报）
  - income 表金额字段在年内是累计值，年报即全年
  - 市值：daily_basic.total_mv 单位是【万元】，输出统一折算为【亿元】
  - 金额：财务表单位是【元】，输出统一折算为【亿元】
  - 涨跌颜色遵循 A 股习惯：红涨绿跌
"""

from __future__ import annotations

import json
import os
import warnings
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import psycopg2

warnings.filterwarnings("ignore", message="pandas only supports SQLAlchemy")

# ── 路径 ────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
OUTPUT_DIR = BASE_DIR / "outputs"
REPORT_DIR = BASE_DIR / "reports"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
REPORT_DIR.mkdir(parents=True, exist_ok=True)

# ── 数据库 ──────────────────────────────────────────────────────────────
DSN = dict(
    host=os.environ.get("PGHOST", "/tmp"),
    dbname=os.environ.get("PGDATABASE", "investassist"),
    user=os.environ.get("PGUSER", "james"),
)

ANNUAL = "4"  # 年报 report_type


# ── 配置 ────────────────────────────────────────────────────────────────
def _strip_comments(obj: Any) -> Any:
    """递归删掉以 _ 开头的注释键"""
    if isinstance(obj, dict):
        return {k: _strip_comments(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_strip_comments(v) for v in obj]
    return obj


def load_config(path: str | Path | None = None) -> dict:
    p = Path(path) if path else CONFIG_PATH
    with open(p, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return _strip_comments(raw)


def resolve_base_year(cfg: dict) -> int:
    """基准年报年份：配置优先，否则 当前年份 - 1"""
    y = cfg["screen"].get("base_year")
    return int(y) if y else date.today().year - 1


# ── 连接 / 查询 ─────────────────────────────────────────────────────────
def connect():
    return psycopg2.connect(**DSN)


def query(sql: str, params: tuple | None = None, conn=None) -> pd.DataFrame:
    """执行 SQL 返回 DataFrame（列名原样保留）"""
    own = conn is None
    conn = conn or connect()
    try:
        return pd.read_sql(sql, conn, params=params)
    finally:
        if own:
            conn.close()


def latest_trade_date(conn=None) -> date:
    """最近一个有行情/估值的交易日"""
    df = query("SELECT max(trade_date) AS d FROM daily_basic", conn=conn)
    d = df["d"].iloc[0]
    if d is None:
        raise RuntimeError("daily_basic 为空，无法确定最新交易日")
    return d if isinstance(d, date) else pd.to_datetime(d).date()


# ── 任务① 选股数据 ──────────────────────────────────────────────────────
def fetch_screen_universe(cfg: dict, base_year: int, conn=None) -> pd.DataFrame:
    """
    取财报选股所需的原始数据。

    对每只股票取「不晚于 base_year 的最近一期年报」，这样即使个别公司年报
    尚未披露或已退市，也能落到可用数据上；超出 max_year_lag 的再剔除。
    """
    max_lag = int(cfg["screen"].get("max_year_lag", 2))
    lo = base_year - max_lag

    sql = """
        SELECT DISTINCT ON (ts_code)
               ts_code, report_year, ann_date,
               roe, roe_dt, roa, debt_to_assets,
               netprofit_yoy, profit_dedt, bps, current_ratio
        FROM financial_indicator
        WHERE report_type = %s
          AND report_year BETWEEN %s AND %s
        ORDER BY ts_code, report_year DESC
    """
    fi = query(sql, (ANNUAL, lo, base_year), conn=conn)

    stocks = query(
        "SELECT ts_code, symbol, name, industry, market, list_status, list_date FROM stocks",
        conn=conn,
    )
    df = fi.merge(stocks, on="ts_code", how="left")
    return df


def apply_screen(df: pd.DataFrame, cfg: dict, base_year: int) -> pd.DataFrame:
    """按配置的维度过滤，返回合格 list（并带上打分用的原始列）"""
    c = cfg["screen"]
    roe_col = c.get("roe_field", "roe")
    out = df.copy()

    out = out[out["ts_code"].str.endswith(tuple(c["markets"]), na=False)]
    out = out.dropna(subset=[roe_col, "debt_to_assets"])

    if c.get("exclude_delisted", True):
        out = out[out["list_status"] == "L"]
    if c.get("exclude_st", True):
        out = out[~out["name"].fillna("").str.contains("ST|退", regex=True)]
    for ind in c.get("exclude_industries", []) or []:
        out = out[out["industry"] != ind]

    out = out[
        (out[roe_col] >= float(c["roe_min"]))
        & (out["debt_to_assets"] <= float(c["debt_to_assets_max"]))
    ]
    return out.sort_values([roe_col, "debt_to_assets"], ascending=[False, True]).reset_index(drop=True)


# ── 任务② 利润历史 ──────────────────────────────────────────────────────
def fetch_profit_history(ts_codes: list[str], base_year: int, cfg: dict, conn=None) -> pd.DataFrame:
    """取 base_year 往前 prefer_points 个年度的归母净利润（年报口径）"""
    span = int(cfg["valuation"]["growth"].get("prefer_points", 5)) - 1
    lo = base_year - span
    sql = """
        SELECT ts_code, report_year, n_income_attr_p, n_income
        FROM income
        WHERE report_type = %s AND report_year BETWEEN %s AND %s
          AND ts_code = ANY(%s)
    """
    return query(sql, (ANNUAL, lo, base_year, list(ts_codes)), conn=conn)


def fit_growth(profits: dict[int, float | None], cfg: dict) -> tuple[float, str, bool]:
    """
    用历史年报利润拟合年化增速。

    规则（与配置一一对应）：
      1. 优先取最近 prefer_points 个利润点（默认 5），全为正 → 几何年化 CAGR
      2. 不足或含非正值 → 退化到 fallback_points 个点（默认 3）
      3. 仍不满足 → 用 default_rate
    返回 (增速, 口径说明, 是否被上下限截断)
    """
    g = cfg["valuation"]["growth"]
    prefer = int(g.get("prefer_points", 5))
    fallback = int(g.get("fallback_points", 3))
    default = float(g.get("default_rate", 0.05))
    lo, hi = float(g.get("min_rate", -1.0)), float(g.get("max_rate", 1.0))
    mode = g.get("mode", "cagr")

    pts = [(y, profits[y]) for y in sorted(profits) if profits[y] is not None]
    rate, note = None, ""

    for n in (prefer, fallback):
        if len(pts) >= n:
            tail = pts[-n:]
            vals = [float(v) for _, v in tail]
            if all(v > 0 for v in vals):
                if mode == "mean_yoy":
                    yoys = [vals[i] / vals[i - 1] - 1 for i in range(1, len(vals))]
                    rate = float(np.mean(yoys))
                    note = f"均增速{n}点({tail[0][0]}~{tail[-1][0]})"
                else:
                    rate = (vals[-1] / vals[0]) ** (1.0 / (n - 1)) - 1
                    note = f"CAGR{n}点({tail[0][0]}~{tail[-1][0]})"
                break

    if rate is None:
        rate, note = default, f"默认值{default:.1%}"

    clipped = False
    if rate < lo:
        rate, clipped = lo, True
    elif rate > hi:
        rate, clipped = hi, True
    return rate, note, clipped


# ── 任务③ 行情 / 市值 ───────────────────────────────────────────────────
def fetch_market_snapshot(codes: list[str], trade_date: date, conn=None) -> pd.DataFrame:
    """最新交易日的市值与估值快照。total_mv 单位【万元】"""
    sql = """
        SELECT ts_code, trade_date, close, total_mv, circ_mv,
               pe, pe_ttm, pb, dv_ttm, turnover_rate
        FROM daily_basic
        WHERE trade_date = %s AND ts_code = ANY(%s)
    """
    return query(sql, (trade_date, list(codes)), conn=conn)


def fetch_price_history(codes: list[str], lookback_days: int, conn=None) -> pd.DataFrame:
    """取最近 lookback_days 个交易日的收盘价（用于趋势判断与走势图）"""
    sql = """
        SELECT ts_code, trade_date, close
        FROM daily_quote
        WHERE trade_date >= (CURRENT_DATE - (%s || ' days')::interval)
          AND ts_code = ANY(%s)
        ORDER BY ts_code, trade_date
    """
    df = query(sql, (int(lookback_days * 1.6), list(codes)), conn=conn)
    if df.empty:
        return df
    df["trade_date"] = pd.to_datetime(df["trade_date"])  # 统一类型，避免 date/Timestamp 比较失效
    keep = sorted(df["trade_date"].unique())[-lookback_days:]
    return df[df["trade_date"].isin(keep)].reset_index(drop=True)


def compute_trend(prices: pd.Series, cfg: dict) -> dict:
    """
    单只股票的趋势刻画。

    返回：各周期涨跌幅、均线位置、趋势标签与强度分。
    强度分 = close>MA20 + MA20>MA60 + ret_20>0 + MA20 斜率>0，取值 0~4
      4 / 3 → 上升趋势；2 → 震荡；1 / 0 → 下降趋势
    """
    t = cfg["monitor"]["trend"]
    s = prices.dropna()
    out = dict(ret_5=np.nan, ret_20=np.nan, ret_60=np.nan, ma20=np.nan, ma60=np.nan,
               ma20_slope=np.nan, pos_250=np.nan, trend="数据不足", trend_score=np.nan,
               high_250=np.nan, low_250=np.nan)
    if len(s) < 2:
        return out

    last = float(s.iloc[-1])

    def _ret(n: int) -> float:
        return float(s.iloc[-1] / s.iloc[-1 - n] - 1) if len(s) > n else np.nan

    out["ret_5"], out["ret_20"], out["ret_60"] = _ret(t["short"]), _ret(t["mid"]), _ret(t["long"])

    ma20 = s.rolling(t["mid"]).mean()
    ma60 = s.rolling(t["long"]).mean()
    out["ma20"] = float(ma20.iloc[-1]) if pd.notna(ma20.iloc[-1]) else np.nan
    out["ma60"] = float(ma60.iloc[-1]) if pd.notna(ma60.iloc[-1]) else np.nan
    if len(ma20.dropna()) > 5:
        prev = ma20.dropna().iloc[-6]
        out["ma20_slope"] = float(ma20.dropna().iloc[-1] / prev - 1) if prev else np.nan

    hi, lo_ = float(s.max()), float(s.min())
    out["high_250"], out["low_250"] = hi, lo_
    out["pos_250"] = (last - lo_) / (hi - lo_) if hi > lo_ else np.nan

    score = 0
    if pd.notna(out["ma20"]) and last > out["ma20"]:
        score += 1
    if pd.notna(out["ma20"]) and pd.notna(out["ma60"]) and out["ma20"] > out["ma60"]:
        score += 1
    if pd.notna(out["ret_20"]) and out["ret_20"] > 0:
        score += 1
    if pd.notna(out["ma20_slope"]) and out["ma20_slope"] > 0:
        score += 1
    out["trend_score"] = score
    out["trend"] = {4: "上升趋势(强)", 3: "上升趋势", 2: "震荡", 1: "下降趋势", 0: "下降趋势(强)"}[score]
    return out


# ── 任务③ 分档 ──────────────────────────────────────────────────────────
def classify_tier(mv_ratio: float, cfg: dict) -> tuple[str, str]:
    """按 mv_ratio = 当前总市值 / 三年后合理估值 落档，返回 (档位, 颜色)"""
    if mv_ratio is None or (isinstance(mv_ratio, float) and np.isnan(mv_ratio)):
        return "无法估值", "#8a8f98"
    for t in cfg["monitor"]["tiers"]:
        lo_, hi_ = t.get("min_ratio"), t.get("max_ratio")
        if (lo_ is None or mv_ratio > lo_) and (hi_ is None or mv_ratio <= hi_):
            return t["label"], t.get("color", "#8a8f98")
    return "未分档", "#8a8f98"


TIER_ORDER = ["非常低估", "低估", "一般低估", "高估", "无法估值"]


def upside(mv_ratio):
    """由 mv_ratio 反推上行空间 = 合理估值/市值 - 1"""
    if mv_ratio is None or (isinstance(mv_ratio, float) and np.isnan(mv_ratio)) or mv_ratio <= 0:
        return np.nan
    return 1.0 / mv_ratio - 1.0


def tier_summary(df: pd.DataFrame) -> pd.DataFrame:
    """按档位汇总：数量 / 占比 / 平均折价 / 上行空间中位 / 上升趋势占比"""
    rows = []
    for t in TIER_ORDER:
        sub = df[df["档位"] == t]
        rows.append(dict(
            档位=t,
            数量=len(sub),
            占比=len(sub) / len(df) if len(df) else np.nan,
            平均折价=sub["折价率_三年后"].mean() if len(sub) else np.nan,
            上行空间中位=sub["市值比_三年后"].apply(upside).median() if len(sub) else np.nan,
            上升趋势占比=(sub["趋势"].str.contains("上升").mean() if len(sub) else np.nan),
        ))
    return pd.DataFrame(rows)


# ── 小工具 ──────────────────────────────────────────────────────────────
def yi(x) -> float:
    """元 → 亿元"""
    return float(x) / 1e8 if pd.notna(x) else np.nan


def wan_to_yi(x) -> float:
    """万元 → 亿元"""
    return float(x) / 1e4 if pd.notna(x) else np.nan


def fmt_pct(x, digits: int = 1) -> str:
    return "—" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x*100:.{digits}f}%"


def fmt_num(x, digits: int = 2) -> str:
    return "—" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:,.{digits}f}"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d")


def sparkline(values, width: int = 90, height: int = 26, color: str = "#5b6472") -> str:
    """把一段收盘价画成内联 SVG 迷你走势图（不依赖任何前端库）"""
    v = [float(x) for x in values if pd.notna(x)]
    if len(v) < 2:
        return '<span style="color:#8a8f98">—</span>'
    lo, hi = min(v), max(v)
    rng = (hi - lo) or 1.0
    n = len(v)
    pts = " ".join(
        f"{i * width / (n - 1):.1f},{height - 2 - (x - lo) / rng * (height - 4):.1f}"
        for i, x in enumerate(v)
    )
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'preserveAspectRatio="none" style="vertical-align:middle"><polyline points="{pts}" '
        f'fill="none" stroke="{color}" stroke-width="1.4" stroke-linejoin="round"/></svg>'
    )

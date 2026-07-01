#!/usr/bin/env python3
"""stock_data MCP Server — 多级数据源查询。

数据源优先级（每个查询工具都遵循）：
  1. PostgreSQL 数据库（investassist）
  2. Tushare API（需 TUSHARE_TOKEN 环境变量）
  3. AKShare API（免费，无需 token）
  4. 本地 CSV/Parquet 文件

启动方式：
  python mcp_server.py
  python mcp_server.py --data-dir /path/to/stock_data

注册到 WorkBuddy (mcp.json):
  {
    "mcpServers": {
      "stock-data": {
        "command": "/path/to/venv/bin/python",
        "args": ["/path/to/mcp_server.py", "--data-dir", "/path/to/stock_data"]
      }
    }
  }
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from pathlib import Path
from datetime import date, datetime
from typing import Any

import duckdb
import pandas as pd
from mcp.server import FastMCP

# ── 日志 ────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("stock-data")

# ── 配置 ────────────────────────────────────────────────────────────────────
DATA_DIR = Path(os.environ.get("STOCK_DATA_DIR", Path(__file__).parent))
DB_HOST = os.environ.get("STOCK_DB_HOST", "/tmp")
DB_PORT = int(os.environ.get("STOCK_DB_PORT", "5432"))
DB_NAME = os.environ.get("STOCK_DB_NAME", "investassist")
DB_USER = os.environ.get("STOCK_DB_USER", "james")
DB_PASS = os.environ.get("STOCK_DB_PASS", "")
TUSHARE_TOKEN = os.environ.get("TUSHARE_TOKEN", "")

mcp = FastMCP("stock-data")


# ═════════════════════════════════════════════════════════════════════════════
# Layer 1: PostgreSQL 数据源
# ═════════════════════════════════════════════════════════════════════════════

class PostgresSource:
    def __init__(self):
        self._conn = None
        self._available: bool | None = None

    @property
    def conn(self):
        if self._conn is None and self._available is not False:
            try:
                import psycopg2  # type: ignore
                self._conn = psycopg2.connect(
                    host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
                    user=DB_USER, password=DB_PASS,
                    connect_timeout=3,
                )
                self._conn.autocommit = True
                self._available = True
            except Exception as e:
                logger.warning("PostgreSQL 连接失败: %s", e)
                self._available = False
        return self._conn

    @property
    def available(self) -> bool:
        if self._available is None:
            self.conn  # trigger connect
        return self._available is True

    def query(self, sql: str, params: tuple | None = None) -> list[dict]:
        if not self.available:
            return []
        try:
            import psycopg2.extras  # type: ignore
            cur = self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(sql, params)
            rows = cur.fetchall()
            cur.close()
            return [{k: str(v) if isinstance(v, (date, datetime)) else v for k, v in r.items()} for r in rows]
        except Exception as e:
            logger.warning("PostgreSQL 查询失败: %s", e)
            return []

    # ── MCP 工具对应的查询方法 ──

    def list_symbols(self, market: str, limit: int, offset: int) -> list[dict]:
        # DB 中 market 存的是板块名(主板/创业板/科创板/北交所)，全部都是 A 股
        # market=all/a 返回全部，hk/etf/index 返回空（DB 无这些数据）
        if market in ("hk", "etf", "index"):
            return []  # 降级到其他数据源
        sql = "SELECT ts_code AS symbol, name, market, industry, area FROM stocks ORDER BY ts_code LIMIT %s OFFSET %s"
        return self.query(sql, (limit, offset))

    def search_symbol(self, keyword: str, limit: int) -> list[dict]:
        rows = self.query(
            "SELECT ts_code AS symbol, name, industry, area, market FROM stocks "
            "WHERE ts_code ILIKE %s OR name ILIKE %s ORDER BY ts_code LIMIT %s",
            (f"%{keyword}%", f"%{keyword}%", limit),
        )
        for r in rows:
            r["source"] = "postgres"
        return rows

    def get_daily(self, symbol: str, start_date: str | None, end_date: str | None, limit: int) -> list[dict]:
        conds = ["ts_code = %s"]
        params: list = [symbol]
        if start_date:
            conds.append("trade_date >= %s")
            params.append(start_date)
        if end_date:
            conds.append("trade_date <= %s")
            params.append(end_date)
        sql = f"SELECT ts_code AS symbol, trade_date, open, high, low, close, pre_close, change, pct_chg, vol, amount FROM daily_quote WHERE {' AND '.join(conds)} ORDER BY trade_date DESC LIMIT %s"
        params.append(limit)
        return self.query(sql, tuple(params))

    def get_fundamental(self, symbol: str, report_type: str, period: str | None) -> dict | None:
        table_map = {"income": "income", "balancesheet": "balance_sheet", "cashflow": "cashflow"}
        table = table_map.get(report_type)
        if not table:
            return None
        if period:
            if len(period) == 8:  # 20251231 → year=2025, type=4
                year = int(period[:4])
                rtype = "4" if period.endswith("1231") else ("1" if period.endswith("0331") else ("2" if period.endswith("0630") else "3"))
            else:
                return None
            rows = self.query(
                f"SELECT * FROM {table} WHERE ts_code=%s AND report_year=%s AND report_type=%s",
                (symbol, year, rtype),
            )
        else:
            rows = self.query(
                f"SELECT * FROM {table} WHERE ts_code=%s ORDER BY report_year DESC, report_type DESC LIMIT 1",
                (symbol,),
            )
        if rows:
            return rows[0]
        return None

    def get_index_list(self) -> list[dict]:
        return self.query(
            "SELECT symbol, name, MAX(trade_date) AS latest_date, "
            "(array_agg(close ORDER BY trade_date DESC))[1] AS latest_close, "
            "COUNT(*) AS rows FROM index_daily GROUP BY symbol, name ORDER BY symbol"
        )

    def get_index_daily(self, symbol: str, start_date: str | None, end_date: str | None, limit: int) -> list[dict]:
        conds = ["symbol = %s"]
        params: list = [symbol]
        if start_date:
            conds.append("trade_date >= %s")
            params.append(start_date)
        if end_date:
            conds.append("trade_date <= %s")
            params.append(end_date)
        sql = f"SELECT symbol, name, trade_date, open, high, low, close, pre_close, change, pct_chg, vol AS volume, amount FROM index_daily WHERE {' AND '.join(conds)} ORDER BY trade_date DESC LIMIT %s"
        params.append(limit)
        return self.query(sql, tuple(params))

    def get_stats(self) -> dict:
        stats = {}
        counts = self.query(
            "SELECT "
            "(SELECT COUNT(*) FROM stocks) AS stocks, "
            "(SELECT COUNT(*) FROM daily_quote) AS daily, "
            "(SELECT COUNT(*) FROM income) AS income, "
            "(SELECT COUNT(*) FROM balance_sheet) AS balance_sheet, "
            "(SELECT COUNT(*) FROM cashflow) AS cashflow, "
            "(SELECT COUNT(*) FROM index_daily) AS index_daily"
        )
        if counts:
            stats = {k: int(v) if v is not None else 0 for k, v in counts[0].items()}
        stats["source"] = "postgres"
        return stats


pg = PostgresSource()


# ═════════════════════════════════════════════════════════════════════════════
# Layer 2: Tushare 数据源
# ═════════════════════════════════════════════════════════════════════════════

class TushareSource:
    def __init__(self):
        self._api = None
        self._available: bool | None = None

    @property
    def api(self):
        if self._api is None and self._available is not False:
            if not TUSHARE_TOKEN:
                self._available = False
                return None
            try:
                import tushare as ts  # type: ignore
                ts.set_token(TUSHARE_TOKEN)
                self._api = ts.pro_api()
                self._available = True
            except Exception as e:
                logger.warning("Tushare 初始化失败: %s", e)
                self._available = False
        return self._api

    @property
    def available(self) -> bool:
        if self._available is None:
            self.api
        return self._available is True

    def get_daily(self, symbol: str, start_date: str | None, end_date: str | None, limit: int) -> list[dict]:
        if not self.available:
            return []
        try:
            kwargs = {"ts_code": symbol, "limit": limit}
            if start_date:
                kwargs["start_date"] = start_date.replace("-", "")
            if end_date:
                kwargs["end_date"] = end_date.replace("-", "")
            df = self.api.daily(**kwargs)
            if df is None or df.empty:
                return []
            return _normalize_daily(df.to_dict("records"), "tushare")
        except Exception as e:
            logger.warning("Tushare get_daily 失败: %s", e)
            return []

    def search_symbol(self, keyword: str, limit: int) -> list[dict]:
        if not self.available:
            return []
        try:
            df = self.api.stock_basic(list_status="L", fields="ts_code,name,industry,area")
            if df is None or df.empty:
                return []
            mask = df["ts_code"].str.contains(keyword, na=False) | df["name"].str.contains(keyword, na=False)
            matched = df[mask].head(limit)
            results = []
            for _, r in matched.iterrows():
                results.append({
                    "symbol": r["ts_code"], "name": r["name"],
                    "industry": r.get("industry", ""), "area": r.get("area", ""),
                    "source": "tushare",
                })
            return results
        except Exception as e:
            logger.warning("Tushare search_symbol 失败: %s", e)
            return []

    def list_symbols(self, market: str, limit: int, offset: int) -> list[dict]:
        if not self.available:
            return []
        try:
            kwargs = {"list_status": "L", "limit": limit, "offset": offset}
            if market == "a":
                kwargs["exchange"] = "SSE,SZSE"
            df = self.api.stock_basic(**kwargs)
            if df is None or df.empty:
                return []
            results = []
            for _, r in df.iterrows():
                results.append({
                    "symbol": r["ts_code"], "name": r["name"],
                    "industry": r.get("industry", ""), "area": r.get("area", ""),
                    "market": "A", "source": "tushare",
                })
            return results
        except Exception as e:
            logger.warning("Tushare list_symbols 失败: %s", e)
            return []

    def get_fundamental(self, symbol: str, report_type: str, period: str | None) -> dict | None:
        if not self.available:
            return None
        try:
            api_map = {"income": "income", "balancesheet": "balancesheet", "cashflow": "cashflow"}
            api_name = api_map.get(report_type)
            if not api_name:
                return None
            api_func = getattr(self.api, api_name)
            kwargs = {"ts_code": symbol}
            if period:
                if len(period) == 8:
                    kwargs["period"] = period
                else:
                    kwargs["end_date"] = period
            else:
                # 最近年报
                year = date.today().year - 1
                kwargs["period"] = f"{year}1231"
            df = api_func(**kwargs)
            if df is None or df.empty:
                return None
            return _normalize_record(df.iloc[0].to_dict(), "tushare")
        except Exception as e:
            logger.warning("Tushare get_fundamental 失败: %s", e)
            return None


ts_source = TushareSource()


# ═════════════════════════════════════════════════════════════════════════════
# Layer 3: AKShare 数据源
# ═════════════════════════════════════════════════════════════════════════════

class AKShareSource:
    def __init__(self):
        self._available: bool | None = None

    @property
    def available(self) -> bool:
        if self._available is None:
            try:
                import akshare as ak  # type: ignore
                self._ak = ak
                self._available = True
            except Exception as e:
                logger.warning("AKShare 初始化失败: %s", e)
                self._available = False
        return self._available

    def _resolve_symbol(self, symbol: str) -> str:
        """将 ts_code (如 600519.SH) 转为纯数字代码。"""
        return symbol.split(".")[0]

    def get_daily(self, symbol: str, start_date: str | None, end_date: str | None, limit: int) -> list[dict]:
        if not self.available:
            return []
        try:
            code = self._resolve_symbol(symbol)
            period = "daily"
            kwargs = {"symbol": code, "period": period, "adjust": "qfq"}
            if start_date:
                kwargs["start_date"] = start_date.replace("-", "")
            if end_date:
                kwargs["end_date"] = end_date.replace("-", "")
            df = self._ak.stock_zh_a_hist(**kwargs)
            if df is None or df.empty:
                return []
            # AKShare 列名映射
            col_map = {
                "日期": "trade_date", "开盘": "open", "收盘": "close",
                "最高": "high", "最低": "low", "成交量": "vol",
                "成交额": "amount", "涨跌幅": "pct_chg", "涨跌额": "change",
                "换手率": "turnover_rate",
            }
            df = df.rename(columns=col_map)
            if "trade_date" in df.columns:
                df["trade_date"] = df["trade_date"].astype(str)
            df["ts_code"] = symbol
            df = df.sort_values("trade_date", ascending=False).head(limit)
            records = _normalize_daily(df.to_dict("records"), "akshare")
            return records
        except Exception as e:
            logger.warning("AKShare get_daily 失败: %s", e)
            return []

    def search_symbol(self, keyword: str, limit: int) -> list[dict]:
        if not self.available:
            return []
        try:
            df = self._ak.stock_info_a_code_name()
            if df is None or df.empty:
                return []
            mask = df["code"].astype(str).str.contains(keyword, na=False) | df["name"].str.contains(keyword, na=False)
            matched = df[mask].head(limit)
            results = []
            for _, r in matched.iterrows():
                code = str(r["code"])
                # 推断后缀
                suffix = ".SH" if code.startswith("6") else ".SZ"
                results.append({
                    "symbol": f"{code}{suffix}", "name": r["name"],
                    "source": "akshare",
                })
            return results
        except Exception as e:
            logger.warning("AKShare search_symbol 失败: %s", e)
            return []

    def list_symbols(self, market: str, limit: int, offset: int) -> list[dict]:
        if not self.available:
            return []
        try:
            df = self._ak.stock_info_a_code_name()
            if df is None or df.empty:
                return []
            results = []
            for _, r in df.iloc[offset:offset + limit].iterrows():
                code = str(r["code"])
                suffix = ".SH" if code.startswith("6") else ".SZ"
                results.append({
                    "symbol": f"{code}{suffix}", "name": r["name"],
                    "market": "A", "source": "akshare",
                })
            return results
        except Exception as e:
            logger.warning("AKShare list_symbols 失败: %s", e)
            return []

    def get_fundamental(self, symbol: str, report_type: str, period: str | None) -> dict | None:
        if not self.available or report_type != "income":
            return None
        try:
            code = self._resolve_symbol(symbol)
            df = self._ak.stock_financial_abstract(symbol=code)
            if df is None or df.empty:
                return None
            # AKShare 财报摘要，取最新一条
            latest = df.iloc[-1].to_dict()
            result = {
                "ts_code": symbol,
                "source": "akshare",
            }
            for k, v in latest.items():
                result[k] = str(v) if v is not None else None
            return result
        except Exception as e:
            logger.warning("AKShare get_fundamental 失败: %s", e)
            return None

    def get_macro(self, indicator: str, limit: int) -> list[dict]:
        if not self.available:
            return []
        try:
            indicator_map = {
                "shibor": "macro_china_shibor",
                "lpr": "macro_china_lpr",
                "cpi": "macro_china_cpi_monthly",
                "pmi": "macro_china_pmi",
                "money_supply": "macro_china_money_supply",
                "bond_yield_10y": "bond_china_yield",
            }
            func_name = indicator_map.get(indicator)
            if not func_name:
                return []
            func = getattr(self._ak, func_name, None)
            if func is None:
                return []
            df = func()
            if df is None or df.empty:
                return []
            records = _normalize_daily(df.to_dict("records"), "akshare")
            return records[-limit:] if limit else records
        except Exception as e:
            logger.warning("AKShare get_macro 失败: %s", e)
            return []


ak_source = AKShareSource()


# ═════════════════════════════════════════════════════════════════════════════
# Layer 4: 本地文件数据源（兜底）
# ═════════════════════════════════════════════════════════════════════════════

def _resolve(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else DATA_DIR / p


def _file_daily(symbol: str, start_date: str | None, end_date: str | None, limit: int) -> list[dict]:
    # 1. index/
    index_path = DATA_DIR / "index" / f"{symbol}.csv"
    if index_path.exists():
        df = pd.read_csv(index_path, parse_dates=["trade_date"])
        if start_date:
            df = df[df["trade_date"] >= start_date]
        if end_date:
            df = df[df["trade_date"] <= end_date]
        df = df.sort_values("trade_date", ascending=False).head(limit)
        return _normalize_daily(df.to_dict("records"), "file:index")

    # 2. market/*.parquet
    market_dir = DATA_DIR / "market"
    parquet_files = list(market_dir.glob("*.parquet")) if market_dir.exists() else []
    if parquet_files:
        try:
            con = duckdb.connect(":memory:")
            con.execute(f"CREATE VIEW market AS SELECT * FROM read_parquet('{market_dir}/*.parquet')")
            conds = [f"symbol = '{symbol}'"]
            if start_date:
                conds.append(f"datetime >= '{start_date}'")
            if end_date:
                conds.append(f"datetime <= '{end_date}'")
            sql = f"SELECT * FROM market WHERE {' AND '.join(conds)} ORDER BY datetime DESC LIMIT {limit}"
            df = con.execute(sql).fetchdf()
            con.close()
            if not df.empty:
                return _normalize_daily(df.to_dict("records"), "file:market")
        except Exception:
            pass

    # 3. daily/*.csv
    daily_path = DATA_DIR / "daily" / f"{symbol}.csv"
    if daily_path.exists():
        df = pd.read_csv(daily_path, parse_dates=["datetime"])
        if start_date:
            df = df[df["datetime"] >= start_date]
        if end_date:
            df = df[df["datetime"] <= end_date]
        df = df.sort_values("datetime", ascending=False).head(limit)
        return _normalize_daily(df.to_dict("records"), "file:daily")

    return []


def _file_fundamental(symbol: str, report_type: str, period: str | None) -> dict | None:
    type_map = {"income": "income", "balancesheet": "balancesheet", "cashflow": "cashflow"}
    subdir = type_map.get(report_type, report_type)
    fund_dir = DATA_DIR / "fundamental" / subdir
    if not fund_dir.exists():
        return None
    if period:
        csv_path = fund_dir / f"{period}.csv"
    else:
        csv_files = sorted(fund_dir.glob("*.csv"), reverse=True)
        csv_path = csv_files[0] if csv_files else None
    if csv_path is None or not csv_path.exists():
        return None
    try:
        df = pd.read_csv(csv_path, low_memory=False)
        row = df[df["ts_code"] == symbol]
        if row.empty:
            return None
        return _normalize_record(row.iloc[0].to_dict(), "file")
    except Exception:
        return None


def _file_index_list() -> list[dict]:
    index_dir = DATA_DIR / "index"
    results = []
    for f in sorted(index_dir.glob("*.csv")):
        try:
            df = pd.read_csv(f, parse_dates=["trade_date"])
            latest = df.iloc[-1] if not df.empty else None
            results.append({
                "code": f.stem, "rows": len(df),
                "latest_date": str(latest["trade_date"])[:10] if latest is not None else None,
                "latest_close": float(latest["close"]) if latest is not None and "close" in latest else None,
            })
        except Exception:
            results.append({"code": f.stem, "error": "读取失败"})
    return results


def _file_macro(indicator: str, limit: int) -> list[dict]:
    csv_path = DATA_DIR / "macro" / f"{indicator}.csv"
    if not csv_path.exists():
        return []
    df = pd.read_csv(csv_path)
    df = df.tail(limit)
    return _normalize_daily(df.to_dict("records"), "file:macro")


# ── 标准化函数 ──────────────────────────────────────────────────────────────

COLUMN_RENAME = {
    "ts_code": "ts_code", "symbol": "symbol",
    "trade_date": "trade_date", "datetime": "trade_date",
    "日期": "trade_date", "open": "open", "开盘": "open",
    "high": "high", "最高": "high", "low": "low", "最低": "low",
    "close": "close", "收盘": "close", "vol": "vol", "成交量": "vol",
    "amount": "amount", "成交额": "amount", "change": "change", "涨跌额": "change",
    "pct_chg": "pct_chg", "涨跌幅": "pct_chg",
}


def _normalize_daily(records: list[dict], source: str) -> list[dict]:
    normalized = []
    for r in records:
        nr = {"source": source}
        for k, v in r.items():
            mapped = COLUMN_RENAME.get(k, k)
            nr[mapped] = v
        # 统一trade_date格式
        if "trade_date" in nr and nr["trade_date"] is not None:
            td = str(nr["trade_date"])
            if " " in td:
                td = td.split(" ")[0]
            nr["trade_date"] = td
        normalized.append(nr)
    return normalized


def _normalize_record(rec: dict, source: str) -> dict:
    result = {"source": source}
    for k, v in rec.items():
        if isinstance(v, (date, datetime)):
            result[k] = str(v)
        elif v is not None:
            result[k] = v
    return result


# ═════════════════════════════════════════════════════════════════════════════
# DataSourceManager — 统一多级降级查询
# ═════════════════════════════════════════════════════════════════════════════

def _try_sources(*tries) -> dict:
    """依次尝试多个 (source_name, func)，返回第一个非空结果 + 命中的 source 名。"""
    for source_name, func in tries:
        try:
            result = func()
            if result:
                if isinstance(result, list) and len(result) == 0:
                    continue
                return {"hit": source_name, "data": result}
        except Exception as e:
            logger.debug("%s 失败: %s", source_name, e)
    return {"hit": None, "data": None}


# ═════════════════════════════════════════════════════════════════════════════
# MCP 工具
# ═════════════════════════════════════════════════════════════════════════════


@mcp.tool()
def list_symbols(market: str = "all", limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """列出可用的股票标的（多级数据源）。

    Args:
        market: all(全部) | a(沪深A股) | hk(港股) | etf(ETF) | index(指数)
        limit: 每页数量 (默认100)
        offset: 偏移量
    """
    result = _try_sources(
        ("postgres", lambda: pg.list_symbols(market, limit, offset)),
        ("tushare", lambda: ts_source.list_symbols(market, limit, offset)),
        ("akshare", lambda: ak_source.list_symbols(market, limit, offset)),
    )

    if result["data"]:
        return {
            "source": result["hit"],
            "total": len(result["data"]),
            "offset": offset,
            "results": result["data"],
        }

    # 兜底: 本地文件
    meta_dir = DATA_DIR / "meta"
    results: list[dict] = []
    if market in ("all", "a", "hk", "etf"):
        f_a = meta_dir / "stock_basic.csv" if market in ("all", "a") else None
        f_hk = meta_dir / "hk_basic.csv" if market in ("all", "hk") else None
        f_etf = meta_dir / "etf_basic.csv" if market in ("all", "etf") else None
        for f in [f_a, f_hk, f_etf]:
            if f and f.exists():
                df = pd.read_csv(f)
                for _, r in df.iterrows():
                    results.append({"symbol": str(r.iloc[0]), "name": str(r.iloc[1]) if len(r) > 1 else "", "source": "file"})
    if market == "index":
        for f in sorted((DATA_DIR / "index").glob("*.csv")):
            results.append({"symbol": f.stem, "name": f.stem, "source": "file:index"})
    return {"source": "file", "total": len(results), "offset": offset, "results": results[offset:offset + limit]}


@mcp.tool()
def get_daily(
    symbol: str, start_date: str | None = None, end_date: str | None = None, limit: int = 100,
) -> dict[str, Any]:
    """获取指定标的的日线行情（多级数据源：DB → Tushare → AKShare → 本地文件）。

    Args:
        symbol: 标的代码 (如 600519.SH, 000001.SH, HSI)
        start_date: 开始日期 (YYYY-MM-DD)
        end_date: 结束日期 (YYYY-MM-DD)
        limit: 最大返回行数 (默认100)
    """
    result = _try_sources(
        ("postgres", lambda: pg.get_daily(symbol, start_date, end_date, limit)),
        ("tushare", lambda: ts_source.get_daily(symbol, start_date, end_date, limit)),
        ("akshare", lambda: ak_source.get_daily(symbol, start_date, end_date, limit)),
    )

    if result["data"]:
        records = result["data"]
        return {
            "symbol": symbol, "source": result["hit"],
            "count": len(records), "data": records,
            "date_range": [records[-1].get("trade_date", ""), records[0].get("trade_date", "")] if records else None,
        }

    # 兜底: 本地文件
    records = _file_daily(symbol, start_date, end_date, limit)
    if records:
        return {
            "symbol": symbol, "source": "file",
            "count": len(records), "data": records,
            "date_range": [records[-1].get("trade_date", records[-1].get("datetime", "")), records[0].get("trade_date", records[0].get("datetime", ""))] if records else None,
        }

    return {"error": f"未找到标的: {symbol}", "tried": ["postgres", "tushare", "akshare", "file"]}


@mcp.tool()
def search_symbol(keyword: str, limit: int = 20) -> dict[str, Any]:
    """按名称或代码搜索标的（多级数据源：DB → Tushare → AKShare → 本地文件）。

    Args:
        keyword: 搜索关键词 (股票名称或代码)
        limit: 最大返回数
    """
    result = _try_sources(
        ("postgres", lambda: pg.search_symbol(keyword, limit)),
        ("tushare", lambda: ts_source.search_symbol(keyword, limit)),
        ("akshare", lambda: ak_source.search_symbol(keyword, limit)),
    )

    if result["data"]:
        return {"keyword": keyword, "source": result["hit"], "count": len(result["data"]), "results": result["data"]}

    # 兜底: 本地文件
    results: list[dict] = []
    daily_dir = DATA_DIR / "daily"
    if daily_dir.exists():
        kw_lower = keyword.lower()
        for f in daily_dir.glob("*.csv"):
            if kw_lower in f.stem.lower():
                results.append({"symbol": f.stem, "source": "file:daily"})
                if len(results) >= limit:
                    break
    if len(results) < limit:
        f = DATA_DIR / "meta" / "stock_basic.csv"
        if f.exists():
            df = pd.read_csv(f)
            mask = df["name"].str.contains(keyword, na=False) | df["ts_code"].str.contains(keyword, na=False)
            for _, r in df[mask].head(limit - len(results)).iterrows():
                results.append({
                    "symbol": str(r["ts_code"]), "name": str(r["name"]),
                    "industry": str(r.get("industry", "")), "source": "file:stock_basic",
                })
    return {"keyword": keyword, "source": "file", "count": len(results), "results": results}


@mcp.tool()
def get_fundamental(symbol: str, report_type: str = "income", period: str | None = None) -> dict[str, Any]:
    """获取财报数据（多级数据源：DB → Tushare → AKShare → 本地文件）。

    Args:
        symbol: 标的代码 (如 600519.SH)
        report_type: income(利润表) | balancesheet(资产负债表) | cashflow(现金流量表)
        period: 报告期 (如 20251231 年报)，为空返回最近一期
    """
    result = _try_sources(
        ("postgres", lambda: (
            [rec] if (rec := pg.get_fundamental(symbol, report_type, period)) else []
        )),
        ("tushare", lambda: (
            [rec] if (rec := ts_source.get_fundamental(symbol, report_type, period)) else []
        )),
        ("akshare", lambda: (
            [rec] if (rec := ak_source.get_fundamental(symbol, report_type, period)) else []
        )),
    )

    if result["data"] and result["data"][0] is not None:
        return {"symbol": symbol, "report_type": report_type, "source": result["hit"], "data": result["data"][0]}

    # 兜底: 本地文件
    rec = _file_fundamental(symbol, report_type, period)
    if rec:
        return {"symbol": symbol, "report_type": report_type, "source": "file", "data": rec}

    return {"error": f"未找到财报数据: {symbol} {report_type}", "tried": ["postgres", "tushare", "akshare", "file"]}


@mcp.tool()
def get_macro(indicator: str | None = None, limit: int = 50) -> dict[str, Any]:
    """获取宏观经济数据（多级数据源：AKShare → 本地文件）。

    Args:
        indicator: shibor|lpr|cpi|pmi|money_supply|bond_yield_10y，空则列出可用指标
        limit: 最大返回行数
    """
    if indicator is None:
        macro_dir = DATA_DIR / "macro"
        available_file = sorted([f.stem for f in macro_dir.glob("*.csv")]) if macro_dir.exists() else []
        available_ak = ["shibor", "lpr", "cpi", "pmi", "money_supply", "bond_yield_10y"]
        return {"available_indicators": list(set(available_file + available_ak))}

    # AKShare first for macro (DB doesn't have macro tables)
    result = _try_sources(
        ("akshare", lambda: ak_source.get_macro(indicator, limit)),
    )

    if result["data"]:
        return {"indicator": indicator, "source": result["hit"], "count": len(result["data"]), "data": result["data"]}

    # 兜底: 本地文件
    records = _file_macro(indicator, limit)
    if records:
        return {"indicator": indicator, "source": "file", "count": len(records), "data": records}

    return {"error": f"指标不存在: {indicator}", "available": list(set([s.stem for s in (DATA_DIR / "macro").glob("*.csv")] if (DATA_DIR / "macro").exists() else []))}


@mcp.tool()
def get_meta(table: str | None = None) -> dict[str, Any]:
    """获取元数据（股票基础信息等，DB优先，本地文件兜底）。

    Args:
        table: stocks | trade_cal，空则列出可用表
    """
    if table is None:
        db_tables = pg.query("SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_name NOT LIKE '%\\_20%' ESCAPE '\\' AND table_type='BASE TABLE'")
        file_tables = sorted([f.stem for f in (DATA_DIR / "meta").glob("*.csv")]) if (DATA_DIR / "meta").exists() else []
        return {"available_tables": list(set([t["table_name"] for t in db_tables] + file_tables))}

    # DB
    if table == "stocks":
        rows = pg.query("SELECT COUNT(*) AS rows, ARRAY_AGG(DISTINCT market) AS markets FROM stocks")
        if rows:
            return {"table": table, "source": "postgres", **rows[0]}
    if table == "trade_cal":
        rows = pg.query("SELECT COUNT(*) AS rows FROM trade_cal")
        if rows:
            return {"table": table, "source": "postgres", **rows[0]}

    # 兜底: 文件
    csv_path = DATA_DIR / "meta" / f"{table}.csv"
    if csv_path.exists():
        df = pd.read_csv(csv_path, low_memory=False)
        return {"table": table, "source": "file", "rows": len(df), "columns": list(df.columns), "sample": df.head(5).to_dict("records")}

    return {"error": f"表不存在: {table}"}


@mcp.tool()
def get_index_list() -> dict[str, Any]:
    """列出所有指数及其最近行情（DB < index_daily > 优先，本地文件兜底）。"""
    rows = pg.get_index_list()
    if rows:
        return {"source": "postgres", "total": len(rows), "indices": rows}

    rows = _file_index_list()
    return {"source": "file", "total": len(rows), "indices": rows}


@mcp.tool()
def get_stats() -> dict[str, Any]:
    """获取数据仓库统计摘要（DB优先，本地文件兜底）。"""
    stats = pg.get_stats()
    if stats.get("stocks", 0) > 0:
        return stats

    # 兜底: 文件统计
    daily_dir = DATA_DIR / "daily"
    index_dir = DATA_DIR / "index"
    macro_dir = DATA_DIR / "macro"
    meta_dir = DATA_DIR / "meta"
    market_dir = DATA_DIR / "market"
    fund_dir = DATA_DIR / "fundamental"

    stats = {
        "source": "file",
        "daily_symbols": len(list(daily_dir.glob("*.csv"))) if daily_dir.exists() else 0,
        "market_days": len(list(market_dir.glob("*.parquet"))) if market_dir.exists() else 0,
        "indices": len(list(index_dir.glob("*.csv"))) if index_dir.exists() else 0,
        "macro_indicators": len(list(macro_dir.glob("*.csv"))) if macro_dir.exists() else 0,
        "meta_tables": len(list(meta_dir.glob("*.csv"))) if meta_dir.exists() else 0,
    }
    if fund_dir.exists():
        for sub in fund_dir.iterdir():
            if sub.is_dir():
                stats[f"fundamental_{sub.name}"] = len(list(sub.glob("*.csv")))
    return stats


# ═════════════════════════════════════════════════════════════════════════════
# 启动
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="stock_data MCP Server")
    parser.add_argument("--data-dir", default=str(Path(__file__).parent), help="stock_data 仓库路径")
    args = parser.parse_args()
    DATA_DIR = Path(args.data_dir)
    if not DATA_DIR.exists():
        raise SystemExit(f"数据目录不存在: {DATA_DIR}")
    mcp.run()

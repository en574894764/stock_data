"""PostgreSQL 适配器 —— 替换 Argus LocalAdapter(CSV)，直读 investassist 数据库。

实现 Argus core.contracts 中的 DataHub 相关接口，性能比 CSV 快 10-100x。
"""

from __future__ import annotations

import os
from datetime import date

import pandas as pd

BAR_COLUMNS = ["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"]


class PGBarAdapter:
    """PostgreSQL 日线行情适配器 —— 对齐 Argus BaseAdapter.get_bar 接口"""

    name = "pg_investassist"

    def __init__(self):
        self.conn = None
        self._connect()

    def _connect(self):
        import psycopg2
        kwargs = {
            "host": os.environ.get("PGHOST", "/tmp"),
            "dbname": os.environ.get("PGDATABASE", "investassist"),
            "user": os.environ.get("PGUSER", "james"),
            "connect_timeout": 5,
        }
        pwd = os.environ.get("PGPASSWORD")
        if pwd:
            kwargs["password"] = pwd
        self.conn = psycopg2.connect(**kwargs)
        self.conn.autocommit = True

    def available(self) -> bool:
        return self.conn is not None and not self.conn.closed

    def get_bar(self, symbols: list[str], freq: str = "1d",
                start: date | str | None = None, end: date | str | None = None,
                adjust: str = "qfq") -> pd.DataFrame:
        """从 daily_quote 表读取日线数据，返回 BAR_COLUMNS 格式的 DataFrame。

        Args:
            symbols: 标的列表，如 ['000001.SZ', '600519.SH']
            freq: 频率，暂只支持 1d
            start: 起始日期
            end: 结束日期
            adjust: 复权方式，> 暂不支持实时复权，返回前复权数据
        """
        if not symbols or not self.available():
            return pd.DataFrame(columns=BAR_COLUMNS)

        # 预处理日期
        def _d(d):
            if d is None: return None
            return d.isoformat() if isinstance(d, date) else str(d)

        start_str = _d(start) or "2006-01-01"
        end_str = _d(end) or date.today().isoformat()

        placeholders = ",".join(["%s"] * len(symbols))
        sql = f"""
            SELECT ts_code as symbol, trade_date as datetime,
                   open, high, low, close, vol as volume, amount
            FROM daily_quote
            WHERE ts_code IN ({placeholders})
              AND trade_date >= %s AND trade_date <= %s
            ORDER BY ts_code, trade_date
        """

        try:
            cur = self.conn.cursor()
            params = list(symbols) + [start_str, end_str]
            cur.execute(sql, params)
            rows = cur.fetchall()
            cur.close()

            if not rows:
                return pd.DataFrame(columns=BAR_COLUMNS)

            df = pd.DataFrame(rows, columns=BAR_COLUMNS)
            df["datetime"] = pd.to_datetime(df["datetime"])
            return df.sort_values(["symbol", "datetime"]).reset_index(drop=True)

        except Exception:
            return pd.DataFrame(columns=BAR_COLUMNS)


class PGUniverseAdapter:
    """PostgreSQL 标的筛选适配器 —— 按条件查询股票池"""

    def __init__(self):
        import psycopg2
        kwargs = {
            "host": os.environ.get("PGHOST", "/tmp"),
            "dbname": os.environ.get("PGDATABASE", "investassist"),
            "user": os.environ.get("PGUSER", "james"),
            "connect_timeout": 5,
        }
        pwd = os.environ.get("PGPASSWORD")
        if pwd:
            kwargs["password"] = pwd
        self.conn = psycopg2.connect(**kwargs)

    def list_by_index(self, index_code: str) -> list[dict]:
        """按指数获取成分股（简化：返回所有A股最新有成交的标的）"""
        cur = self.conn.cursor()
        sql = """
            SELECT ts_code, name, exchange
            FROM stocks
            WHERE delist_date IS NULL AND exchange IN ('SSE','SZSE','BSE')
            ORDER BY ts_code
        """
        cur.execute(sql)
        return [{"ts_code": r[0], "name": r[1], "exchange": r[2]} for r in cur.fetchall()]

    def search(self, keyword: str, limit: int = 50) -> list[dict]:
        """按关键词搜索标的"""
        cur = self.conn.cursor()
        sql = """
            SELECT ts_code, name, exchange
            FROM stocks
            WHERE (ts_code ILIKE %s OR name ILIKE %s)
              AND delist_date IS NULL
            ORDER BY ts_code
            LIMIT %s
        """
        kw = f"%{keyword}%"
        cur.execute(sql, (kw, kw, limit))
        return [{"ts_code": r[0], "name": r[1], "exchange": r[2]} for r in cur.fetchall()]

    def get_benchmark(self, code: str, start: date | str, end: date | str) -> dict | None:
        """获取指数作为基准"""
        start_s = start.isoformat() if isinstance(start, date) else str(start)
        end_s = end.isoformat() if isinstance(end, date) else str(end)
        cur = self.conn.cursor()
        sql = """
            SELECT trade_date, close FROM index_daily
            WHERE symbol = %s AND trade_date >= %s AND trade_date <= %s
            ORDER BY trade_date
        """
        cur.execute(sql, (code, start_s, end_s))
        rows = cur.fetchall()
        if not rows:
            return None
        return {
            "code": code,
            "dates": [str(r[0]) for r in rows],
            "equity": [float(r[1]) for r in rows],
        }

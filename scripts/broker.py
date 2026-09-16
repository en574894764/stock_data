#!/usr/bin/env python3
"""下单适配层 (Broker) — 信号 → 订单 的统一接口
=============================================
背景
    execute_signals.py 已能跑通"信号→成交"半自动链路 (--simulate / --confirm),
    但行情读取硬编码 daily_quote, 不支持 ETF (etf_quote), 且没有统一的下单抽象。
    本模块补齐两块:
      ① 行情路由: 按代码类型分流 daily_quote / etf_quote
      ② 下单接口: BaseBroker 抽象 + 模拟/手动实现 + 券商 API stub

行情路由
    ETF 代码 = 6 位裸数字 (510300 / 159915 / 511990 ...)
    股票 = 带 .SH/.SZ/.BJ 后缀 (600000.SH), 港股 = .HK (00700.HK)
    → is_etf() 判定, 查询时分流 etf_quote / daily_quote

下单接口
    BaseBroker  (抽象)      实盘实现只需覆盖 buy/sell/get_positions/get_cash
    SimBroker   (模拟)      虚拟资金 100 万, 复用 execute_signals 状态机
    ManualBroker(手动)      人工下单后回填成交价 (现 --confirm 模式)
    QmtBroker   (stub)      国金/华鑫等 QMT, xtquant 库, 需券商开通
    PTradeBroker(stub)      恒生 PTrade, 需券商开通

用法 (行情路由 + 抽象接口供 execute_signals/build_nav 复用):
    from broker import is_etf, get_open_prices, get_quotes_wide
"""
from __future__ import annotations

import os
import sys
import warnings

import pandas as pd

# pd.read_sql 用 DBAPI2 连接会打 SQLAlchemy 警告, 静默
warnings.filterwarnings("ignore", message=".*SQLAlchemy.*")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

VIRTUAL_CASH = 1_000_000  # 模拟盘虚拟资金 (与 execute_signals 一致)


# ---------------------------------------------------------------- 行情路由
def is_etf(code: str) -> bool:
    """ETF = 6 位裸数字代码; 股票带 .SH/.SZ/.BJ 后缀, 港股带 .HK。"""
    return isinstance(code, str) and code.isdigit() and len(code) == 6


def _quote_table(code: str) -> str:
    return "etf_quote" if is_etf(code) else "daily_quote"


def _code_col(code: str) -> str:
    return "code" if is_etf(code) else "ts_code"


def get_open_prices(conn, codes, date) -> dict:
    """date 日开盘价, 按代码类型分流 etf_quote / daily_quote。
    停牌/无行情的不在返回里。"""
    if not codes:
        return {}
    etf = [c for c in codes if is_etf(c)]
    stk = [c for c in codes if not is_etf(c)]
    out = {}
    cur = conn.cursor()
    for tbl, cols, ccol in (("etf_quote", etf, "code"), ("daily_quote", stk, "ts_code")):
        if not cols:
            continue
        ph = ",".join(["%s"] * len(cols))
        cur.execute(f"SELECT {ccol}, open FROM {tbl} WHERE trade_date = %s AND {ccol} IN ({ph})",
                    (date, *cols))
        for r in cur.fetchall():
            if r[1] is not None and float(r[1]) > 0:
                out[r[0]] = float(r[1])
    cur.close()
    return out


def get_quotes_wide(conn, codes, start) -> dict:
    """日线 (open/close/pre_close/pct_chg) 宽表, 按代码类型分流 etf_quote / daily_quote。
    返回 {"open": DF, "close": DF, "pre_close": DF, "pct_chg": DF},
    每个 index=DatetimeIndex, columns=code (pct_chg 保持原表口径: daily_quote 为百分数, etf_quote 为百分数)。"""
    frames = []
    for tbl, cols, ccol in (("etf_quote", [c for c in codes if is_etf(c)], "code"),
                            ("daily_quote", [c for c in codes if not is_etf(c)], "ts_code")):
        if not cols:
            continue
        ph = ",".join(["%s"] * len(cols))
        df = pd.read_sql(
            f"SELECT trade_date, {ccol} AS code, open, close, pre_close, pct_chg FROM {tbl} "
            f"WHERE {ccol} IN ({ph}) AND trade_date >= %s", conn, params=cols + [start])
        if df.empty:
            continue
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        frames.append(df)
    empty = {k: pd.DataFrame() for k in ("open", "close", "pre_close", "pct_chg")}
    if not frames:
        return empty
    df = pd.concat(frames, ignore_index=True)
    for col in ("open", "close", "pre_close", "pct_chg"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    out = {}
    for col in ("open", "close", "pre_close", "pct_chg"):
        w = df.pivot(index="trade_date", columns="code", values=col).sort_index()
        out[col] = w
    return out


# ---------------------------------------------------------------- 下单抽象
class BaseBroker:
    """下单接口抽象。子类只需实现 buy / sell / get_positions / get_cash。
    语义: weight 为目标权重 (占该策略账户), 实盘实现里换算成金额/股数。"""
    name = "base"

    def buy(self, code: str, weight: float, price: float | None = None, note: str = "") -> dict:
        raise NotImplementedError

    def sell(self, code: str, weight: float, price: float | None = None, note: str = "") -> dict:
        raise NotImplementedError

    def get_positions(self) -> dict:
        raise NotImplementedError

    def get_cash(self) -> float:
        raise NotImplementedError


class SimBroker(BaseBroker):
    """模拟成交 (虚拟资金 100 万, 按目标权重换股数)。不落库, 只算成交单, 供 execute_signals 复用。"""

    name = "sim"

    def __init__(self, cash: float = VIRTUAL_CASH):
        self.cash = cash

    def _volume(self, weight: float, price: float) -> int:
        """目标权重 → 股数 (A股 100 股一手, 向下取整)。"""
        if not price or price <= 0:
            return 0
        value = self.cash * weight
        return int(round(value / price / 100) * 100) or 100

    def buy(self, code, weight, price=None, note=""):
        vol = self._volume(weight, price or 0.0)
        return {"action": "BUY", "code": code, "weight": weight,
                "price": price, "volume": vol, "value": vol * (price or 0.0), "note": note or "simulate"}

    def sell(self, code, weight, price=None, note=""):
        vol = self._volume(weight, price or 0.0)
        return {"action": "SELL", "code": code, "weight": weight,
                "price": price, "volume": vol, "value": vol * (price or 0.0), "note": note or "simulate"}

    def get_positions(self) -> dict:
        return {}

    def get_cash(self) -> float:
        return self.cash


class ManualBroker(BaseBroker):
    """手动下单: 用户在券商 App 下单后, 回填实际成交价/量 (现 --confirm 模式)。"""

    name = "manual"

    def buy(self, code, weight, price=None, note=""):
        return {"action": "BUY", "code": code, "weight": weight, "price": price, "note": note or "confirm"}

    def sell(self, code, weight, price=None, note=""):
        return {"action": "SELL", "code": code, "weight": weight, "price": price, "note": note or "confirm"}

    def get_positions(self) -> dict:
        return {}

    def get_cash(self) -> float:
        return 0.0


class QmtBroker(BaseBroker):
    """国金/华鑫等券商的 QMT (迅投 miniQMT)。接入需:
      1. 券商开通 QMT 量化权限 + 下载客户端
      2. pip install xtquant  (或从 QMT 安装目录拿 xtquant 包)
      3. 客户端登录后, 用 XtQuantTrader 连接:
             from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
             from xtquant.xttype import StockAccount
        下单用 order_stock(account, code, xtconstant.STOCK_BUY/SELL, volume, price_type, price)
      未接入前保持 stub (raise), 不影响模拟/手动链路。"""
    name = "qmt"

    def _raise(self, *_a, **_k):
        raise NotImplementedError("QMT 未接入: 需券商 QMT 权限 + xtquant 库, 见 broker.py 注释")

    buy = _raise
    sell = _raise
    get_positions = _raise
    get_cash = _raise


class PTradeBroker(BaseBroker):
    """恒生 PTrade 量化终端。接入需:
      1. 券商开通 PTrade 权限
      2. 用其 Python API (ptrade / 恒生量化 SDK) 下单
      未接入前保持 stub。"""
    name = "ptrade"

    def _raise(self, *_a, **_k):
        raise NotImplementedError("PTrade 未接入: 需券商 PTrade 权限, 见 broker.py 注释")

    buy = _raise
    sell = _raise
    get_positions = _raise
    get_cash = _raise


BROKERS = {
    "sim": SimBroker,
    "manual": ManualBroker,
    "qmt": QmtBroker,
    "ptrade": PTradeBroker,
}


def get_broker(name: str) -> BaseBroker:
    if name not in BROKERS:
        raise ValueError(f"未知 broker: {name}, 可选 {list(BROKERS)}")
    return BROKERS[name]()


if __name__ == "__main__":
    import factor_eval as fe
    conn = fe.get_conn()
    print("is_etf 判定:", {c: is_etf(c) for c in ["510300", "600000.SH", "00700.HK", "159915"]})
    px = get_open_prices(conn, ["510300", "511990", "600000.SH"], pd.Timestamp.today().date())
    print("今日开盘价样本:", px)
    conn.close()
    print("broker 可用:", list(BROKERS))

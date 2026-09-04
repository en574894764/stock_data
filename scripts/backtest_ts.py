#!/usr/bin/env python3
"""
时序回测引擎 (单策略 × 单标的 / 多标的独立信号)
================================================
- vectorbt 1.x 封装: Portfolio.from_signals
- 数据: PG daily_quote.pct_chg (复权口径) → cumprod 构造复权指数价格, 信号在指数上计算
- 统一输出契约 (与截面引擎 factor_eval / 组合器 combo_backtest 衔接):
    daily_returns: pd.Series  (DatetimeIndex, 组合日收益率)
    holdings:      pd.DataFrame(date, symbol, weight, strategy, engine)
- 内置策略 (信号规则库, 可扩展):
    ma_cross    双均线交叉          --fast 20 --slow 60
    boll_break  布林带突破          --window 20 --k 2.0 (突破上轨持有, 跌破中轨离场)
    rsi_rev     RSI 反转            --window 14 --lo 30 --hi 70
    macd_cross  MACD 金叉死叉       --fast 12 --slow 26 --signal 9
    trend       均线趋势过滤        --window 200 (适合指数/ETF 择时)

用法:
    python3 scripts/backtest_ts.py --symbol 600519.SH --strategy ma_cross
    python3 scripts/backtest_ts.py --symbol 510300.SH,510500.SH --strategy trend --window 200
    python3 scripts/backtest_ts.py --symbol 000300.SH --strategy ma_cross --fast 5 --slow 20 \
        --start 2019-01-01 --end 2026-09-01 --out reports/ts_000300_ma.md
"""
import argparse
import os
import sys
import warnings

import numpy as np
import pandas as pd
import psycopg2

warnings.filterwarnings("ignore")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
YEAR_BARS = 244  # A股年交易日数
ENGINE = "ts_vectorbt"


# ---------------------------------------------------------------- 数据
def get_conn():
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "/tmp"),
        port=os.environ.get("PGPORT", "5432"),
        dbname=os.environ.get("PGDATABASE", "investassist"),
        user=os.environ.get("PGUSER", "james"),
        password=os.environ.get("PGPASSWORD", ""),
    )


def load_price(conn, symbol: str, start: str, end: str) -> pd.Series:
    """复权指数价格: pct_chg 累乘 (消除除权跳空; 首日基准 100).
    标的自动路由: 显式前缀 etf:/idx: 或裸 6 位码→etf_quote; 其余按 daily_quote→etf_quote→index_daily 兜底."""
    if symbol.startswith("etf:"):
        routes = [("etf_quote", "code", symbol[4:])]
    elif symbol.startswith("idx:"):
        routes = [("index_daily", "symbol", symbol[4:])]
    elif symbol.isdigit():
        routes = [("etf_quote", "code", symbol)]
    else:
        routes = [("daily_quote", "ts_code", symbol), ("etf_quote", "code", symbol),
                  ("index_daily", "symbol", symbol)]
    for table, col, code in routes:
        cur = conn.cursor()
        cur.execute(
            f"SELECT trade_date, pct_chg FROM {table} "
            f"WHERE {col} = %s AND trade_date >= %s AND trade_date <= %s ORDER BY trade_date",
            (code, start, end))
        rows = cur.fetchall()
        cur.close()
        if rows:
            idx = pd.to_datetime([r[0] for r in rows])
            pct = pd.to_numeric(pd.Series([r[1] for r in rows], index=idx), errors="coerce") / 100.0
            pct = pct.fillna(0.0)
            return (1.0 + pct).cumprod() * 100.0
    raise SystemExit(f"无数据: {symbol} {start}~{end} (daily_quote/etf_quote/index_daily 均未命中)")


# ---------------------------------------------------------------- 内置策略: (price, p) -> (entries, exits)
def sig_ma_cross(price, p):
    fast, slow = price.rolling(p["fast"]).mean(), price.rolling(p["slow"]).mean()
    return price > fast, price < slow


def sig_boll_break(price, p):
    ma = price.rolling(p["window"]).mean()
    sd = price.rolling(p["window"]).std()
    upper = ma + p["k"] * sd
    return price > upper, price < ma


def sig_rsi_rev(price, p):
    delta = price.diff()
    gain = delta.clip(lower=0).rolling(p["window"]).mean()
    loss = (-delta.clip(upper=0)).rolling(p["window"]).mean()
    rsi = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    return rsi < p["lo"], rsi > p["hi"]


def sig_macd_cross(price, p):
    ema_f = price.ewm(span=p["fast"], adjust=False).mean()
    ema_s = price.ewm(span=p["slow"], adjust=False).mean()
    dif = ema_f - ema_s
    dea = dif.ewm(span=p["signal"], adjust=False).mean()
    return dif > dea, dif < dea


def sig_trend(price, p):
    ma = price.rolling(p["window"]).mean()
    return price > ma, price < ma


STRATEGIES = {
    "ma_cross": (sig_ma_cross, {"fast": 20, "slow": 60}),
    "boll_break": (sig_boll_break, {"window": 20, "k": 2.0}),
    "rsi_rev": (sig_rsi_rev, {"window": 14, "lo": 30, "hi": 70}),
    "macd_cross": (sig_macd_cross, {"fast": 12, "slow": 26, "signal": 9}),
    "trend": (sig_trend, {"window": 200}),
}


# ---------------------------------------------------------------- 回测
def run_ts(symbol: str, strategy: str, start: str, end: str,
           params: dict = None, init_cash: float = 1_000_000, fees: float = 0.0015) -> dict:
    """单标的时序回测. 返回统一契约: daily_returns + holdings + stats + pf(vectorbt原生)."""
    import vectorbt as vbt

    if strategy not in STRATEGIES:
        raise SystemExit(f"未知策略: {strategy} (可选: {', '.join(STRATEGIES)})")
    sig_fn, defaults = STRATEGIES[strategy]
    p = {**defaults, **(params or {})}
    for k in ("fast", "slow", "signal", "window"):  # bar 数参数转 int
        if k in p:
            p[k] = int(p[k])

    conn = get_conn()
    price = load_price(conn, symbol, start, end)
    conn.close()
    if len(price) < max(p.values()) + 10:
        raise SystemExit(f"数据不足: {symbol} 仅 {len(price)} 根 bar")

    entries, exits = sig_fn(price, p)
    entries, exits = entries.fillna(False), exits.fillna(False)

    pf = vbt.Portfolio.from_signals(
        close=price, entries=entries, exits=exits,
        init_cash=init_cash, fees=fees, freq="1D",
    )
    # NOTE: 绩效统一用自研 calc_stats (244 bar 年化, 与 factor_eval 口径一致);
    # vbt 1.x 的 year_freq 语义是时间长度而非 bar 数, 弃用其年化指标

    daily_returns = pf.returns()
    if not isinstance(daily_returns, pd.Series):
        daily_returns = daily_returns.iloc[:, 0]

    # 持仓快照: 每 bar 仓位权重 = 持仓市值 / 组合净值 (asset_value 直接返回 Series)
    asset_value = pf.asset_value()
    equity = pf.value()
    in_pos = asset_value > 0
    weight = (asset_value / equity).fillna(0.0)
    weight = pd.Series(np.where(in_pos.to_numpy(), weight.to_numpy(), 0.0), index=price.index)
    holdings = pd.DataFrame({
        "date": price.index,
        "symbol": symbol,
        "weight": weight.to_numpy(),
        "strategy": strategy,
        "engine": ENGINE,
    })

    stats = calc_stats(daily_returns)
    n_tr = int(pf.trades.count())
    stats.update({
        "trades": n_tr,
        "win_rate": float(pf.trades.win_rate()) if n_tr else np.nan,
        "profit_factor": float(pf.trades.profit_factor()) if n_tr else np.nan,
    })
    return {"daily_returns": daily_returns, "holdings": holdings, "stats": stats, "pf": pf}


def calc_stats(ret: pd.Series) -> dict:
    """组合器/两引擎共用的绩效口径 (244 bar 年化)."""
    if len(ret) < 20:
        return {"ann_ret": np.nan, "ann_vol": np.nan, "sharpe": np.nan, "max_dd": np.nan, "total": np.nan}
    nav = (1.0 + ret).cumprod()
    years = len(ret) / YEAR_BARS
    ann_ret = nav.iloc[-1] ** (1 / years) - 1
    ann_vol = ret.std() * np.sqrt(YEAR_BARS)
    dd = (nav / nav.cummax() - 1).min()
    return {"ann_ret": ann_ret, "ann_vol": ann_vol,
            "sharpe": ann_ret / ann_vol if ann_vol > 0 else np.nan,
            "max_dd": dd, "total": nav.iloc[-1] - 1}


# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description="时序回测引擎 (单策略×单/多标的独立信号)")
    ap.add_argument("--symbol", required=True, help="ts_code, 多标的逗号分隔 (各自独立信号)")
    ap.add_argument("--strategy", required=True, choices=list(STRATEGIES))
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--end", default="2026-09-01")
    ap.add_argument("--init-cash", type=float, default=1_000_000)
    ap.add_argument("--fees", type=float, default=0.0015, help="单边费率 (默认 0.15%%)")
    # 策略参数 (按需传, 覆盖默认)
    for k in ("fast", "slow", "signal", "window", "k", "lo", "hi"):
        ap.add_argument(f"--{k}", type=float, default=None)
    ap.add_argument("--out", default=None, help="报告输出路径 (md)")
    args = ap.parse_args()

    params = {k: getattr(args, k) for k in ("fast", "slow", "signal", "window", "k", "lo", "hi")
              if getattr(args, k) is not None}
    symbols = [s.strip() for s in args.symbol.split(",")]

    lines = [f"# 时序回测报告: {args.strategy}\n"]
    lines.append(f"- 标的: {', '.join(symbols)} | 区间: {args.start} ~ {args.end}")
    p_str = ", ".join(f"{k}={v:g}" for k, v in params.items()) if params else "默认参数"
    lines.append(f"- 参数: {p_str} | 初始资金: {args.init_cash:,.0f} | 单边费率: {args.fees:.2%}\n")
    lines.append("| 标的 | 总收益 | 年化 | 波动 | 夏普 | 最大回撤 | 交易次数 | 胜率 | 盈亏比 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")

    all_ret, all_hold = [], []
    for sym in symbols:
        r = run_ts(sym, args.strategy, args.start, args.end, params, args.init_cash, args.fees)
        s = r["stats"]
        lines.append("| {} | {:.1%} | {:.1%} | {:.1%} | {:.2f} | {:.1%} | {} | {} | {} |".format(
            sym, s["total"], s["ann_ret"], s["ann_vol"], s["sharpe"],
            s["max_dd"], s["trades"],
            f"{s['win_rate']:.0%}" if s["trades"] else "-",
            f"{s['profit_factor']:.2f}" if s["trades"] else "-"))
        all_ret.append(r["daily_returns"].rename(sym))
        all_hold.append(r["holdings"])
        print(f"{sym}: 年化 {s['ann_ret']:.1%} 夏普 {s['sharpe']:.2f} 回撤 {s['max_dd']:.1%} 交易 {s['trades']}")

    # 等权基准: 买入持有
    rets = pd.concat(all_ret, axis=1)
    if len(symbols) > 1:
        bh = rets.mean(axis=1).rename("等权日买入持有")
        st = calc_stats(bh)
        lines.append(f"\n**等权买入持有基准**: 年化 {st['ann_ret']:.1%} | 夏普 {st['sharpe']:.2f} | 回撤 {st['max_dd']:.1%}")

    report = "\n".join(lines) + "\n"
    print("\n" + report)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w") as f:
            f.write(report)
        print(f"报告已写入: {args.out}")
        # 持仓快照落盘 (统一契约, 供组合器消费)
        hv_path = args.out.replace(".md", "_holdings.csv")
        pd.concat(all_hold).to_csv(hv_path, index=False)
        rets.to_parquet(args.out.replace(".md", "_returns.parquet"))
        print(f"持仓快照: {hv_path}\n日收益序列: {args.out.replace('.md', '_returns.parquet')}")


if __name__ == "__main__":
    main()

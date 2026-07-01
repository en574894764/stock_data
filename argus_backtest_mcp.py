#!/usr/bin/env python3
"""Argus Backtest MCP Server — 策略回测 + HTML 报告

Tools: list_strategies, search_universe, run_backtest, generate_report, compare_runs
数据源: PostgreSQL (investassist)，通过 argus_pg_adapter
"""

from __future__ import annotations

import json, os, sys, uuid
from datetime import date, datetime
from pathlib import Path

import pandas as pd

# 确保 argus 在 Python 路径
ARGUS_PATH = os.environ.get("ARGUS_PATH", "/Users/james/workspace/argus")
if ARGUS_PATH not in sys.path:
    sys.path.insert(0, ARGUS_PATH)

REPO = Path(__file__).parent
OUTPUTS = REPO / "outputs" / "backtest"
STRATEGIES_DIR = Path(os.environ.get("ARGUS_STRATEGIES_PATH", str(REPO / "strategies")))
OUTPUTS.mkdir(parents=True, exist_ok=True)
STRATEGIES_DIR.mkdir(parents=True, exist_ok=True)

_run_store: dict[str, dict] = {}


def _adapter():
    from argus_pg_adapter import PGBarAdapter
    return PGBarAdapter()


def _ua():
    from argus_pg_adapter import PGUniverseAdapter
    return PGUniverseAdapter()


def _discover():
    strategies = {}
    # 1. 内置 (从 argus 项目)
    try:
        from argus.strategy.samples import MACrossStrategy, TopMomentumStrategy
        strategies["MACross"] = {"name": "MACross", "description": "双均线交叉 — 均线上穿买入，下穿卖出",
                                  "params": {"fast": 5, "slow": 20}, "source": "builtin"}
        strategies["TopMomentum"] = {"name": "TopMomentum", "description": "动量选股 — 选择近期涨幅最高的N只",
                                      "params": {"top_n": 10, "lookback": 20}, "source": "builtin"}
    except Exception as e:
        print(f"[WARN] 内置策略加载失败: {e}", file=sys.stderr)
        # 手动注册内置策略的元数据（类在运行时动态加载）
        strategies["MACross"] = {"name": "MACross", "description": "双均线交叉",
                                  "params": {"fast": 5, "slow": 20}, "source": "builtin"}
        strategies["TopMomentum"] = {"name": "TopMomentum", "description": "动量选股",
                                      "params": {"top_n": 10, "lookback": 20}, "source": "builtin"}

    # 自定义
    for f in sorted(STRATEGIES_DIR.glob("*.py")):
        if f.name.startswith("_"): continue
        try:
            ns = {}
            exec(f.read_text(), ns)
            for name, obj in ns.items():
                if isinstance(obj, type) and hasattr(obj, "name") and hasattr(obj, "generate_signals") and name != "Strategy":
                    strategies[name] = {"name": getattr(obj, "name", name),
                                         "description": (obj.__doc__ or "").split("\n")[0],
                                         "params": dict(getattr(obj, "PARAMS", {})),
                                         "source": str(f.relative_to(STRATEGIES_DIR))}
        except Exception as e:
            print(f"[WARN] {f.name}: {e}", file=sys.stderr)
    return strategies


def _load(name: str):
    info = _discover()[name]
    if info["source"] == "builtin":
        # 动态导入，跳过 redis 依赖
        import importlib, types
        try:
            from argus.strategy.samples import MACrossStrategy as M, TopMomentumStrategy as T
            return M if name == "MACross" else T
        except ImportError:
            # 手动构建策略类 (轻量版，不依赖 redis)
            from argus.core.contracts import Action, Signal, Strategy
            import argus.indicators as ind

            if name == "MACross":
                class _MACross(Strategy):
                    name = "MACross"
                    PARAMS = {"fast": 5, "slow": 20}
                    def generate_signals(self, ctx):
                        f, s = self.params["fast"], self.params["slow"]
                        longs = []
                        for sym in ctx.universe:
                            hist = ctx.history(sym)
                            if hist is None or len(hist) < s + 1: continue
                            try:
                                c = hist["close"].astype(float)
                                if ind.ma(c, f).iloc[-1] > ind.ma(c, s).iloc[-1]:
                                    p = ctx.price(sym)
                                    if p and p > 0: longs.append((sym, float(p)))
                            except: pass
                        if not longs: return []
                        w = 1.0 / len(longs)
                        return [Signal(symbol=s, action=Action.BUY, ts=ctx.now, weight=w, reason="金叉") for s, _ in longs]
                return _MACross

            class _TopM(Strategy):
                name = "TopMomentum"
                PARAMS = {"top_n": 10, "lookback": 20}
                def generate_signals(self, ctx):
                    scores = []
                    for sym in ctx.universe:
                        hist = ctx.history(sym)
                        if hist is None or len(hist) < 2: continue
                        try:
                            c = hist["close"].astype(float)
                            mo = (c.iloc[-1] / c.iloc[0] - 1)
                            scores.append((sym, mo))
                        except: pass
                    scores.sort(key=lambda x: x[1], reverse=True)
                    top = scores[:self.params["top_n"]]
                    if not top: return []
                    w = 1.0 / len(top)
                    return [Signal(symbol=s, action=Action.BUY, ts=ctx.now, weight=w, reason=f"动量 {mo:.1%}") for s, mo in top]
            return _TopM

    # 自定义策略
    path = STRATEGIES_DIR / info["source"]
    ns = {}
    exec(path.read_text(), ns)
    for cls_name, obj in ns.items():
        if isinstance(obj, type) and getattr(obj, "name", None) == info["name"] and hasattr(obj, "generate_signals"):
            return obj
    raise ValueError(f"无法加载策略: {name}")


# ═══════════════════════════════════════════════════════════════════

def tool_list_strategies() -> str:
    return json.dumps(list(_discover().values()), ensure_ascii=False, indent=2)


def tool_search_universe(keyword: str = "", limit: int = 50) -> str:
    ua = _ua()
    return json.dumps(ua.search(keyword, limit) if keyword else ua.list_by_index("all")[:limit], ensure_ascii=False)


def tool_run_backtest(strategy: str, symbols: str, start: str, end: str,
                      params: str = "{}", init_cash: float = 1_000_000) -> str:
    run_id = str(uuid.uuid4())[:8]
    symbol_list = [s.strip() for s in symbols.split(",") if s.strip()]
    strat_params = json.loads(params) if isinstance(params, str) else params
    s, e = date.fromisoformat(start), date.fromisoformat(end)

    adapter = _adapter()
    panel = adapter.get_bar(symbol_list, start=s, end=e)
    if panel.empty:
        return json.dumps({"error": "无数据", "symbols": symbol_list})

    StratClass = _load(strategy)
    strat = StratClass(**strat_params)

    try:
        from argus.strategy.engine import SimpleBacktestEngine
        result = SimpleBacktestEngine(adapter).run(
            strat, symbol_list, s, e,
            costs={"commission": 0.0003, "stamp_tax": 0.0005, "slippage": 0.0005})
    except Exception as ex:
        return json.dumps({"error": f"回测失败: {ex}"})

    _run_store[run_id] = {
        "run_id": run_id, "strategy": strategy, "params": strat_params,
        "symbols": symbol_list, "start": start, "end": end,
        "metrics": result.metrics,
        "equity": result.equity_curve,
        "dates": [d.isoformat() for d in result.dates] if result.dates else [],
        "_result": result,
    }

    return json.dumps({
        "run_id": run_id, "strategy": strategy,
        "symbols": symbol_list, "period": f"{start} ~ {end}",
        "metrics": {k: round(v, 4) for k, v in result.metrics.items()},
    }, ensure_ascii=False, indent=2)


def tool_generate_report(run_id: str, benchmark: str = "000300.SH") -> str:
    stored = _run_store.get(run_id)
    if not stored:
        return json.dumps({"error": f"run_id {run_id} 不存在"})

    result = stored["_result"]
    s, e = date.fromisoformat(stored["start"]), date.fromisoformat(stored["end"])
    bench = _ua().get_benchmark(benchmark, s, e)
    adapter = _adapter()

    def bars_provider(sym):
        return adapter.get_bar([sym], start=s, end=e)

    try:
        from argus.strategy.report import render_html
        html = render_html(result, benchmark=bench, bars_provider=bars_provider)
    except Exception as ex:
        return json.dumps({"error": f"报告渲染失败: {ex}"})

    path = OUTPUTS / f"backtest_{run_id}.html"
    path.write_text(html, encoding="utf-8")

    return json.dumps({
        "run_id": run_id, "report_path": str(path),
        "metrics": stored["metrics"],
    }, ensure_ascii=False, indent=2)


def tool_compare_runs(run_ids: str) -> str:
    ids = [r.strip() for r in run_ids.split(",")]
    rows = []
    for rid in ids:
        s = _run_store.get(rid)
        if s:
            rows.append({"run_id": rid, "strategy": s["strategy"], "params": s["params"], **s["metrics"]})
    if not rows:
        return json.dumps({"error": "无有效 run_id"})
    df = pd.DataFrame(rows)
    if "sharpe" in df.columns:
        df = df.sort_values("sharpe", ascending=False)
    return df.to_json(orient="records", force_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════════
# MCP Server (FastMCP)
# ═══════════════════════════════════════════════════════════════════

from mcp.server import FastMCP

mcp = FastMCP("argus-backtest")


@mcp.tool()
def list_strategies() -> str:
    """列出所有可用的回测策略及其参数说明"""
    return tool_list_strategies()


@mcp.tool()
def search_universe(keyword: str = "", limit: int = 50) -> str:
    """按关键词搜索股票标的，或返回全部A股列表"""
    return tool_search_universe(keyword, limit)


@mcp.tool()
def run_backtest(strategy: str, symbols: str, start: str, end: str,
                 params: str = "{}", init_cash: float = 1_000_000) -> str:
    """运行一次量化回测。输入策略名、逗号分隔标的、起止日期、参数JSON"""
    return tool_run_backtest(strategy, symbols, start, end, params, init_cash)


@mcp.tool()
def generate_report(run_id: str, benchmark: str = "000300.SH") -> str:
    """根据 run_id 生成自包含 HTML 回测报告(plotly图表+交易明细)"""
    return tool_generate_report(run_id, benchmark)


@mcp.tool()
def compare_runs(run_ids: str) -> str:
    """对比多次回测结果，按夏普降序排列"""
    return tool_compare_runs(run_ids)


if __name__ == "__main__":
    mcp.run()

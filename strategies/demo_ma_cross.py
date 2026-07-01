"""示例策略: 双均线交叉 — 均线上穿买入，下穿卖出 (等权组合)

策略逻辑:
  - 每日收盘后计算 fast 日均线和 slow 日均线
  - fast > slow → 看多，等权买入所有看多标的
  - fast < slow → 清仓该标的
  - 无看多标的 → 空仓

用法:
  python argus_backtest_mcp.py
  > run_backtest("DualMACross", "000001.SZ,600519.SH", "2020-01-01", "2025-12-31", '{"fast":5,"slow":20}')
"""

from argus.core.contracts import Action, Signal, Strategy


class DualMACross(Strategy):
    name = "双均线交叉"
    PARAMS = {"fast": 5, "slow": 20}
    INDICATOR_SPEC = {"ma": [5, 20]}  # 报告 K 线图会画 MA5 / MA20

    def generate_signals(self, ctx):
        fast = self.params.get("fast", 5)
        slow = self.params.get("slow", 20)
        longs = []

        for sym in ctx.universe:
            hist = ctx.history(sym)
            if hist is None or len(hist) < slow + 1:
                continue

            try:
                close = hist["close"].astype(float)
                import argus.indicators as ind
                ma_fast = ind.ma(close, fast).iloc[-1]
                ma_slow = ind.ma(close, slow).iloc[-1]
            except Exception:
                continue

            if ma_fast > ma_slow:
                px = ctx.price(sym)
                if px and px > 0:
                    longs.append((sym, float(px)))

        if not longs:
            return []  # 空仓

        w = 1.0 / len(longs)
        return [
            Signal(symbol=s, action=Action.BUY, ts=ctx.now,
                   weight=w, reason=f"金叉 MA{fast}>{slow}",
                   ref_price=px)
            for s, px in longs
        ]

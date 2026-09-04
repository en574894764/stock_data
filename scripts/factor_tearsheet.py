#!/usr/bin/env python3
"""
alphalens 因子 tear sheet (P2)
===============================
对指定因子生成完整 alphalens tear sheet (IC 时序/分层收益/换手/自相关等),
PNG 输出到 reports/tearsheets/<factor>_NN.png。

用法:
    python3 scripts/factor_tearsheet.py                          # 生产 6 因子
    python3 scripts/factor_tearsheet.py --factors ret_20d_rev    # 指定因子
"""
import argparse
import os
import sys
import warnings

warnings.filterwarnings("ignore")
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import factor_eval as fe

PROD_FACTORS = ["ret_20d_rev", "turnover_20", "ivol_60", "ln_mv", "ep_ttm", "sue_delta"]
CACHE = os.path.join(REPO, "factor_cache")


def tearsheet(name: str, out_dir: str) -> int:
    from alphalens.utils import get_clean_factor_and_forward_returns
    from alphalens.tears import create_full_tear_sheet

    fpath = os.path.join(CACHE, f"{name}.parquet")
    rpath = os.path.join(CACHE, "_daily_ret.parquet")
    if not os.path.exists(fpath):
        print(f"跳过 {name}: 无缓存")
        return 0
    factor_wide = pd.read_parquet(fpath).sort_index().sort_index(axis=1)
    daily_ret = pd.read_parquet(rpath).sort_index().sort_index(axis=1)

    # 对齐 (因子股票池 ⊄ 行情矩阵 — 老坑: 先交集)
    common = factor_wide.columns.intersection(daily_ret.columns)
    factor_wide, daily_ret = factor_wide[common], daily_ret[common]
    # 区间对齐
    lo = factor_wide.index.min()
    daily_ret = daily_ret[daily_ret.index >= lo]

    # prices: 复权价格指数 (alphalens 需要 price 而非 return)
    prices = (1.0 + daily_ret.fillna(0.0)).cumprod() * 100.0
    # alphalens 要求价格连续 (NaN 前向填充; 停牌段 ffill 收益为 0 ≈ 停牌价不变, 可接受)
    prices = prices.ffill().dropna(axis=1, how="all")

    factor = factor_wide.stack()
    factor.index.names = ["date", "asset"]
    fd = get_clean_factor_and_forward_returns(factor, prices, quantiles=5,
                                              periods=(1, 5, 20), max_loss=50)
    # alphalens 0.4.6 的 GridFigure 每段落结束调 plt.close() 销毁图 →
    # patch 掉 close, tearsheet 跑完后统一保存再恢复
    orig_close = plt.close
    plt.close = lambda *a, **k: None
    try:
        create_full_tear_sheet(fd, long_short=True)
    finally:
        plt.close = orig_close

    os.makedirs(out_dir, exist_ok=True)
    n = 0
    for i in plt.get_fignums():
        n += 1
        plt.figure(i).savefig(os.path.join(out_dir, f"{name}_{n:02d}.png"),
                              dpi=110, bbox_inches="tight")
    plt.close("all")
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--factors", nargs="*", default=PROD_FACTORS)
    ap.add_argument("--out-dir", default=os.path.join(REPO, "reports", "tearsheets"))
    args = ap.parse_args()

    for name in args.factors:
        n = tearsheet(name, args.out_dir)
        print(f"✅ {name}: {n} 张图 → {args.out_dir}/{name}_*.png")


if __name__ == "__main__":
    main()

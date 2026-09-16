#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
价值投资 · 一键跑通
─────────────────────────────────────────────────────────
  ① step1_screen      财报选股（ROE + 资产负债率）
  ② step2_fair_value  合理估值（去年年报口径 / 三年后口径）
  ③ step3_daily_monitor 每日市值扫描 + 四档打标 + 趋势
  ④ render_report     生成 HTML 日报

用法：
  python run_daily.py                 # 全流程
  python run_daily.py --skip-screen   # 沿用已有的选股 list（选股参数没变时更快）
  python run_daily.py --feishu        # 跑完顺带推一张飞书摘要卡
"""

from __future__ import annotations

import argparse
import sys
import time

import pandas as pd

import render_report as rr
import step1_screen as s1
import step2_fair_value as s2
import step3_daily_monitor as s3
import valuelib as vl


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="价值投资 · 每日全流程")
    ap.add_argument("--skip-screen", action="store_true", help="跳过任务①，直接复用 outputs/screen_list.csv")
    ap.add_argument("--year", type=int, help="覆盖基准年报年份")
    ap.add_argument("--feishu", action="store_true", help="跑完推送飞书摘要卡")
    args = ap.parse_args(argv)

    t0 = time.time()
    cfg = vl.load_config()
    if args.year:
        cfg["screen"]["base_year"] = args.year
    base_year = vl.resolve_base_year(cfg)

    print("=" * 68)
    print(f"价值投资 · 每日流程  基准年报 {base_year}  启动 {pd.Timestamp.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 68)

    if not args.skip_screen:
        print("\n▶ ① 财报选股")
        s1.main(["--year", str(base_year)])
    else:
        print("\n▶ ① 财报选股 —— 跳过（复用现有 list）")

    print("\n▶ ② 合理估值")
    s2.main(["--year", str(base_year)])

    print("\n▶ ③ 每日扫描 + 分档 + 趋势")
    df, meta = s3.scan(cfg)
    csv_path = vl.OUTPUT_DIR / f"daily_scan_{meta['trade_date']:%Y%m%d}.csv"
    df.drop(columns=["_curve"]).to_csv(csv_path, index=False, encoding="utf-8-sig")

    summary = s3.tier_summary(df)
    print(f"   交易日 {meta['trade_date']} | 扫描 {len(df)} 只 | CSV → {csv_path}")
    print(summary[["档位", "数量", "占比", "平均折价", "上升趋势占比"]].to_string(index=False))

    print("\n▶ ④ 生成 HTML 日报")
    html_path = rr.render(df, meta, cfg)
    print(f"   日报 → {html_path}")

    print(f"\n完成，用时 {time.time() - t0:.1f}s")

    if args.feishu:
        try:
            from feishu_push import push_summary
            push_summary(df, meta, summary, html_path)
            print("   飞书卡片已推送")
        except Exception as e:  # 推送失败不影响主流程
            print(f"   [警告] 飞书推送失败：{e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

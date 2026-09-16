#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
价值投资日报 · 飞书推送
─────────────────────────────────────────────────────────
复用项目根 report_builder.py 的凭证与发送链路（Interactive Card 2.0，
凭证优先取项目 .env 的 FEISHU_APP_ID / FEISHU_APP_SECRET）。

卡片里不放 markdown 表格（飞书卡片表格渲染不稳），用列表形式给：
  · 四档数量概览
  · 每档 Top5（按折价率），带折价率与趋势
  · 日报 HTML 路径

用法：python feishu_push.py            # 读 outputs/ 最新 daily_scan_*.csv 推送
     python run_daily.py --feishu     # 跑完全流程顺带推送
"""

from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # 为了 import report_builder（在项目根）

import valuelib as vl


def load_env() -> None:
    """把项目根 .env 注入环境变量（不覆盖已有值）"""
    p = ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in __import__("os").environ:
            __import__("os").environ[k] = v


def build_card_text(df, meta, summary) -> str:
    td, by = meta["trade_date"], meta["base_year"]
    cfg = meta["cfg"]
    sc, val = cfg["screen"], cfg["valuation"]
    n_up = int((df["趋势"].str.contains("上升", na=False)).sum())
    n_dn = int((df["趋势"].str.contains("下降", na=False)).sum())

    L = [f"**交易日 {td} · 基准年报 {by} 年 · 合格 list {len(df)} 只**",
         f"选股口径：ROE ≥ {sc['roe_min']}% 且 资产负债率 ≤ {sc['debt_to_assets_max']}%"
         f" ｜ 合理 PE {val['pe_multiple']}×，三年后口径",
         f"趋势面：上升趋势 {n_up} 只 / 下降趋势 {n_dn} 只", ""]

    colors = {"非常低估": "🟢", "低估": "🟩", "一般低估": "🟡", "高估": "🔴"}
    show_top = {"非常低估": 8, "低估": 5, "一般低估": 3, "高估": 0}
    for _, r in summary.iterrows():
        t = r["档位"]
        if t not in colors:
            continue
        L.append(f"{colors[t]} **{t} {int(r['数量'])} 只**（占比 {vl.fmt_pct(r['占比'],0)}，"
                 f"上升趋势 {vl.fmt_pct(r['上升趋势占比'],0)}）")
        top_n = show_top.get(t, 0)
        if top_n:
            sub = df[df["档位"] == t].nsmallest(top_n, "市值比_三年后")
            for _, s in sub.iterrows():
                L.append(f"　· {s['名称']}（{s['代码']}）"
                         f" 折价 {vl.fmt_pct(s['折价率_三年后'])} · {s['趋势']}")
        L.append("")

    html = vl.REPORT_DIR / f"value_report_{td:%Y%m%d}.html"
    L.append(f"📄 完整日报：`{html}`")
    L.append("⚠️ 纯财报静态估算，不构成投资建议")
    return "\n".join(L)


def push_summary(df, meta, summary, html_path=None) -> bool:
    """推飞书卡片，返回是否成功"""
    from report_builder import _push_feishu_card
    text = build_card_text(df, meta, summary)
    return _push_feishu_card(text, title=f"🐾 价值投资日报 {meta['trade_date']}", template="turquoise")


def main() -> int:
    load_env()
    import glob
    import pandas as pd

    files = sorted(vl.OUTPUT_DIR.glob("daily_scan_*.csv"))
    if not files:
        print("没有 daily_scan_*.csv，请先跑 run_daily.py")
        return 1
    latest = files[-1]
    df = pd.read_csv(latest, dtype={"代码": str})
    td = pd.to_datetime(latest.stem.rsplit("_", 1)[-1], format="%Y%m%d").date()
    cfg = vl.load_config()
    meta = dict(trade_date=td, base_year=vl.resolve_base_year(cfg), cfg=cfg)
    ok = push_summary(df, meta, vl.tier_summary(df))
    print("推送:", "成功" if ok else "失败")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""5:5 组合账户层 (Combo Allocator) — 多因子 + 趋势 两条腿的顶层组合
====================================================================
把两条独立策略 (多因子个股 + ETF 趋势) 的净值按目标权重合成组合净值,
监控权重漂移, 漂移超阈值带时输出再平衡建议。

架构 (与现有"每策略独立账户"兼容):
    - 两条腿各自独立跑 (signal_log / position / trade_log / portfolio_nav)
    - 本模块是「组合视图」: 读两腿 portfolio_nav → 合成 → 落 combo_nav
    - 再平衡 = 组合层建议 (实盘资金调配由用户手动执行, 本模块只算偏离+给动作)

组合配置存 combo_config 表 (config 即组合), 净值落 combo_nav 表。

用法:
    python3 scripts/combo_allocator.py              # 合成净值 + 漂移监控
    python3 scripts/combo_allocator.py --push       # 飞书推送组合报告
    python3 scripts/combo_allocator.py --rebalance  # 只看再平衡建议
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
sys.path.insert(0, REPO)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(REPO, ".env"))
except ImportError:
    pass

import factor_eval as fe  # noqa: E402

DEFAULT_COMBO = "combo_5f5"

DDL = """
CREATE TABLE IF NOT EXISTS combo_config (
    combo_id     VARCHAR(64) PRIMARY KEY,
    name         VARCHAR(128) NOT NULL,
    legs         JSONB NOT NULL,          -- [{strategy_id, target_weight, label}]
    rebal_band   DOUBLE PRECISION NOT NULL DEFAULT 0.05,  -- 偏离阈值带 (±)
    is_active    BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS combo_nav (
    combo_id     VARCHAR(64) NOT NULL,
    trade_date   DATE NOT NULL,
    nav          DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (combo_id, trade_date)
);
"""

# 默认组合: 多因子 50% + ETF 趋势 50%, 偏离阈值带 ±5%
DEFAULT_COMBO_DEF = {
    "combo_id": DEFAULT_COMBO,
    "name": "多因子+趋势 5:5 组合",
    "legs": [
        {"strategy_id": "prod_6f_eq", "target_weight": 0.5, "label": "多因子个股"},
        {"strategy_id": "prod_etf_mom", "target_weight": 0.5, "label": "ETF 趋势"},
    ],
    "rebal_band": 0.05,
}


def ensure_tables(cur):
    cur.execute(DDL)


def upsert_default(cur, combo_def=None):
    combo_def = combo_def or DEFAULT_COMBO_DEF
    from psycopg2.extras import Json
    cur.execute("""
        INSERT INTO combo_config (combo_id, name, legs, rebal_band)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (combo_id) DO UPDATE
        SET name = EXCLUDED.name, legs = EXCLUDED.legs,
            rebal_band = EXCLUDED.rebal_band, updated_at = now()
    """, (combo_def["combo_id"], combo_def["name"], Json(combo_def["legs"]),
          combo_def["rebal_band"]))


def load_combo(cur, combo_id) -> dict:
    cur.execute("SELECT combo_id, name, legs, rebal_band FROM combo_config WHERE combo_id=%s AND is_active",
                (combo_id,))
    row = cur.fetchone()
    if not row:
        raise SystemExit(f"组合不存在或未激活: {combo_id}")
    return {"combo_id": row[0], "name": row[1], "legs": row[2], "rebal_band": row[3]}


def load_leg_navs(cur, legs) -> dict:
    """读两条腿 portfolio_nav → {strategy_id: Series(trade_date→nav)}。"""
    navs = {}
    for leg in legs:
        sid = leg["strategy_id"]
        cur.execute("SELECT trade_date, nav FROM portfolio_nav WHERE strategy_id=%s ORDER BY trade_date", (sid,))
        rows = cur.fetchall()
        if not rows:
            print(f"  ⚠️ {sid} ({leg.get('label', sid)}) 无 portfolio_nav, 跳过")
            continue
        s = pd.Series({r[0]: float(r[1]) for r in rows}, name=sid)
        s.index = pd.to_datetime(s.index)
        navs[sid] = s
    return navs


def compose(combo: dict, navs: dict) -> dict:
    """合成组合净值 (买入持有口径) + 当前实际权重漂移。"""
    legs = combo["legs"]
    have = {l["strategy_id"]: l for l in legs if l["strategy_id"] in navs}
    if len(have) < 2:
        return {"nav": None, "drift": None, "missing": [l["strategy_id"] for l in legs if l["strategy_id"] not in navs]}

    # 对齐日期 (并集, 前向填充; 组合从两腿都有净值的首个日期起)
    df = pd.DataFrame({sid: navs[sid] for sid in have}).ffill()
    df = df.dropna()
    if df.empty:
        return {"nav": None, "drift": None, "missing": []}

    weights = {sid: have[sid]["target_weight"] for sid in have}
    # 组合净值 = Σ w_i × nav_i (买入持有, 初始各 w_i)
    combo_nav = sum(weights[sid] * df[sid] for sid in have)

    # 实际权重漂移: w_i(t) = w_i × nav_i(t) / Σ w_j × nav_j(t)
    total = sum(weights[sid] * df[sid] for sid in have)
    drift = {sid: float(weights[sid] * df[sid].iloc[-1] / total.iloc[-1]) for sid in have}
    return {"nav": combo_nav, "drift": drift, "weights": weights, "missing": []}


def rebalance_suggestion(combo: dict, drift: dict) -> list:
    """偏离 |实际-目标| 超阈值带 → 再平衡动作 (相对口径, 用户乘实际总资金)。"""
    band = combo["rebal_band"]
    out = []
    for leg in combo["legs"]:
        sid = leg["strategy_id"]
        if sid not in drift:
            continue
        target = leg["target_weight"]
        actual = drift[sid]
        delta = actual - target
        if abs(delta) > band:
            action = "减配" if delta > 0 else "增配"
            out.append({
                "strategy_id": sid, "label": leg.get("label", sid),
                "target": target, "actual": actual, "delta": delta,
                "action": action, "amount_pct": abs(delta) * 100,
            })
    return out


def save_combo_nav(cur, combo_id, nav: pd.Series):
    cur.execute("DELETE FROM combo_nav WHERE combo_id=%s", (combo_id,))
    if nav is None or nav.empty:
        return
    from psycopg2.extras import execute_values
    execute_values(cur, """INSERT INTO combo_nav (combo_id, trade_date, nav) VALUES %s
        ON CONFLICT (combo_id, trade_date) DO NOTHING""",
        [(combo_id, d.date(), float(v)) for d, v in nav.items()])


def stats(nav: pd.Series) -> dict:
    if nav is None or len(nav) < 60:
        return {}
    r = nav.pct_change().dropna()
    years = len(r) / 244
    ann = (nav.iloc[-1] / nav.iloc[0]) ** (1 / years) - 1
    vol = r.std() * np.sqrt(244)
    dd = float((nav / nav.cummax() - 1).min())
    return {"ann": ann, "vol": vol, "sharpe": ann / vol if vol > 0 else np.nan, "dd": dd}


def fmt(v):
    return "-" if v is None or not np.isfinite(v) else f"{v*100:.1f}%"


def render(combo: dict, res: dict, suggestions: list) -> str:
    L = [f"### 🧩 {combo['name']} — 组合账户\n"]
    legs = combo["legs"]
    L.append(f"- 腿配置: " + " + ".join(f"{l.get('label', l['strategy_id'])} {l['target_weight']*100:.0f}%"
                                         for l in legs))
    if res.get("missing"):
        L.append(f"- ⚠️ 缺净值: {', '.join(res['missing'])} (先跑 build_nav)\n")
        return "\n".join(L)

    nav = res["nav"]
    drift = res["drift"]
    s = stats(nav)
    if s:
        L.append(f"- 组合净值: **{nav.iloc[-1]:.4f}** | 累计 {fmt(nav.iloc[-1]/nav.iloc[0]-1)} | "
                 f"年化 {fmt(s['ann'])} | 夏普 {s['sharpe']:.2f} | 回撤 {fmt(s['dd'])}")

    L.append("\n| 腿 | 目标权重 | 实际权重 | 偏离 |")
    L.append("|---|---|---|---|")
    for leg in legs:
        sid = leg["strategy_id"]
        if sid in drift:
            target = leg["target_weight"]
            actual = drift[sid]
            delta = actual - target
            L.append(f"| {leg.get('label', sid)} | {target*100:.0f}% | {actual*100:.1f}% | {delta*100:+.1f}pp |")
        else:
            L.append(f"| {leg.get('label', sid)} | {leg['target_weight']*100:.0f}% | 无净值 | - |")

    if suggestions:
        L.append("\n**再平衡建议** (偏离超 ±%.0f%% 阈值带):" % (combo['rebal_band'] * 100))
        for sg in suggestions:
            L.append(f"- {sg['label']}: {sg['action']} **{sg['amount_pct']:.1f}%** "
                     f"(目标 {sg['target']*100:.0f}% → 实际 {sg['actual']*100:.1f}%)")
    else:
        L.append("\n✅ 两腿权重偏离在阈值带内, 无需再平衡")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--combo", default=DEFAULT_COMBO)
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--rebalance", action="store_true", help="只输出再平衡建议")
    args = ap.parse_args()

    conn = fe.get_conn()
    cur = conn.cursor()
    ensure_tables(cur)
    upsert_default(cur)
    conn.commit()

    combo = load_combo(cur, args.combo)
    navs = load_leg_navs(cur, combo["legs"])
    res = compose(combo, navs)
    drift = res.get("drift") or {}
    suggestions = rebalance_suggestion(combo, drift)
    save_combo_nav(cur, combo["combo_id"], res.get("nav"))
    conn.commit()

    report = render(combo, res, suggestions)
    print(report)

    if args.push:
        try:
            from report_builder import _push_feishu_card
            _push_feishu_card(report, f"🧩 组合账户 · {combo['name']}", "purple")
        except Exception as e:
            print(f"[Feishu] 推送失败: {e}")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()

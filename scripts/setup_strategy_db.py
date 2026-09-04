#!/usr/bin/env python3
"""
策略运行时表初始化 (六子系统生产链 P0)
=====================================
四张表:
  strategy_config  策略定义 (jsonb 配置即策略)
  signal_log       每日信号流水 (pending → executed/expired)
  position         持仓状态 (每策略当前快照)
  trade_log        成交记录 (信号执行结果)

默认写入一个策略: prod_6f_eq —— 已实证的 6 因子等权月度组合
(Q5 年化 ~22%, 样本外夏普 1.06, 详见 reports/factor_eval_*.md)

用法: python3 scripts/setup_strategy_db.py [--drop]
"""
import argparse
import json
import os
import sys

import psycopg2
from psycopg2.extras import Json, execute_values

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DDL = """
-- 策略定义: 配置即策略 (改 jsonb 即改策略, 无需改代码)
CREATE TABLE IF NOT EXISTS strategy_config (
    strategy_id   VARCHAR(64) PRIMARY KEY,
    name          VARCHAR(128) NOT NULL,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    config        JSONB NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 信号流水: 每次调仓生成的买卖信号 (初始 pending, 执行后回填)
CREATE TABLE IF NOT EXISTS signal_log (
    signal_id     BIGSERIAL PRIMARY KEY,
    strategy_id   VARCHAR(64) NOT NULL REFERENCES strategy_config(strategy_id),
    trade_date    DATE NOT NULL,           -- 信号日 (T 日收盘后生成)
    exec_date     DATE,                    -- 计划执行日 (T+1 开盘)
    ts_code       VARCHAR(12) NOT NULL,
    action        VARCHAR(8)  NOT NULL,    -- BUY / SELL
    target_weight DOUBLE PRECISION NOT NULL,  -- 调仓后目标权重
    score         DOUBLE PRECISION,        -- 合成因子分 (BUY 时记录)
    rank_in_pool  INTEGER,
    reason        TEXT,                    -- 信号归因 (新进/负分剔除/调仓再平衡)
    status        VARCHAR(12) NOT NULL DEFAULT 'pending',  -- pending/executed/expired
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (strategy_id, trade_date, ts_code, action)
);
CREATE INDEX IF NOT EXISTS idx_signal_status ON signal_log(strategy_id, status, trade_date);
CREATE INDEX IF NOT EXISTS idx_signal_date ON signal_log(trade_date);

-- 持仓状态: 每策略最新快照 (由 executed 信号驱动更新)
CREATE TABLE IF NOT EXISTS position (
    strategy_id   VARCHAR(64) NOT NULL REFERENCES strategy_config(strategy_id),
    ts_code       VARCHAR(12) NOT NULL,
    weight        DOUBLE PRECISION NOT NULL DEFAULT 0,
    entry_date    DATE NOT NULL,
    last_signal   DATE NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (strategy_id, ts_code)
);

-- 成交记录: 信号执行结果 (人工/后续自动执行回填)
CREATE TABLE IF NOT EXISTS trade_log (
    trade_id      BIGSERIAL PRIMARY KEY,
    signal_id     BIGINT NOT NULL REFERENCES signal_log(signal_id),
    executed_at   TIMESTAMPTZ,
    price         DOUBLE PRECISION,        -- 实际成交价
    volume        DOUBLE PRECISION,        -- 股数
    value         DOUBLE PRECISION,        -- 金额
    note          TEXT
);
"""

# 默认策略: 已实证 6 因子等权月度组合
DEFAULT_STRATEGY = {
    "strategy_id": "prod_6f_eq",
    "name": "6因子等权月度组合 (生产默认)",
    "config": {
        "factors": {                   # 因子及方向权重 (value 越大预期收益越高)
            "ret_20d_rev": 1.0,
            "turnover_20": 1.0,
            "ivol_60": 1.0,
            "ln_mv": 1.0,
            "ep_ttm": 1.0,
            "sue_delta": 1.0,
        },
        "rebalance": "monthly",        # monthly | biweekly | n_days:20
        "top_n": 50,                   # 持仓数量
        "weighting": "equal",          # equal (等权)
        "min_history_days": 120,       # 上市满 N 自然日
        "min_cross_section": 50,       # 最小截面数
        "cost_one_side": 0.0015,       # 单边成本 (记录用)
        "timing_filter": None,         # 择时层: null=满仓 | {"factor": "index_ma200"} 之类的预留
    },
}


def get_conn():
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "/tmp"),
        port=os.environ.get("PGPORT", "5432"),
        dbname=os.environ.get("PGDATABASE", "investassist"),
        user=os.environ.get("PGUSER", "james"),
        password=os.environ.get("PGPASSWORD", ""),
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--drop", action="store_true", help="先 DROP 四表 (清空策略运行时数据, 因子/行情不受影响)")
    args = ap.parse_args()

    conn = get_conn()
    cur = conn.cursor()

    if args.drop:
        print("⚠️  DROP strategy 四表 ...")
        cur.execute("DROP TABLE IF EXISTS trade_log, position, signal_log, strategy_config CASCADE")
        conn.commit()

    cur.execute(DDL)
    conn.commit()
    print("✅ 四表就绪: strategy_config / signal_log / position / trade_log")

    # 默认策略 upsert
    cur.execute("""
        INSERT INTO strategy_config (strategy_id, name, config)
        VALUES (%s, %s, %s)
        ON CONFLICT (strategy_id) DO UPDATE
        SET name = EXCLUDED.name, config = EXCLUDED.config, updated_at = now()
    """, (DEFAULT_STRATEGY["strategy_id"], DEFAULT_STRATEGY["name"], Json(DEFAULT_STRATEGY["config"])))
    conn.commit()
    print(f"✅ 默认策略: {DEFAULT_STRATEGY['strategy_id']}")
    print(json.dumps(DEFAULT_STRATEGY["config"], ensure_ascii=False, indent=2))

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()

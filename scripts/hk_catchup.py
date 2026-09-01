#!/usr/bin/env python
"""一次性：把港股 FRESH 批（last=8/28）推进到源端最新（8/31）。

背景：fetch_hk 的 7 天容忍让"源端滞后 1 天"的缺口永远追不上——
每天 18:30 跑时源端只有 T-1 数据，PG last(T-2 之前) 总在容忍内被跳过。
港股指数已单独收紧为 3 天；个股保持 7 天（避免每天全量拉 2747 只），
累积缺口用本脚本手动收敛。

用法：nohup python scripts/hk_catchup.py > logs/hk_catchup.log 2>&1 &
"""
from __future__ import annotations

import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import fetch_and_backup as fab
from fetch_and_backup import get_pg_conn, log


def main():
    # 收紧容忍为 1 天：cutoff = target - 1 = 8/31，
    # last=8/28 的 FRESH 批将全部进入 lagging 被拉取
    fab.HK_LAG_TOLERANCE_DAYS = 1
    target = date(2026, 9, 1)

    conn = get_pg_conn()
    log(f"hk_catchup 启动: target={target}, 容忍=1天 (cutoff={target})")

    t0 = time.time()
    n = fab.fetch_hk(conn, target, dry_run=False)
    conn.commit()
    log(f"hk_catchup 完成: +{n} 行, 耗时 {(time.time()-t0)/60:.1f} 分钟")


if __name__ == "__main__":
    main()

# step_factor 首次 CRON 实战检查记录

## 2026-09-04 08:30 (检查 2026-09-03 晚运行)

**结论: 全绿，step_factor 首次 CRON 实战成功。**

- 因子步骤于 18:53:31 启动，出现在 export.py 之后、GitHub 备份之前，符合设计位置
- 三步均成功（无 WARN/ERROR，整体"失败步骤: 无"）：
  - backfill_daily_basic.py --days 3：~7s，daily_basic MAX(trade_date)=2026-09-03
  - compute_factors.py：~2m14s，factor_value MAX(trade_date)=2026-09-03，21 因子全部覆盖，每因子 ~5,200 行（ep_ttm 3,632 / roe_lf 5,428 / sue_* 5,8-6,0k 属正常口径差异）
  - crowding_monitor.py：~7m23s，reports/crowding_monitor.md 已写至 2026-09-03
- 拥挤度预警（业务信息非故障）：turnover_20 🔴 94% 分位；ivol_60 🟡 86% 分位；其余 4 因子绿
- 整体 pipeline：push ✅ (5,585 files)，耗时 2191s（比前日多 ~667s，主要为因子步骤）

## 观察到的待办（非阻塞，未修）

1. pipeline_report_20260903.md 中没有因子步骤章节 — report_builder.py 未覆盖新步骤，可考虑补充 factor_value/daily_basic 新鲜度行
2. crowding_monitor.py 在 pipeline 中未带 --days 60 参数，跑了全历史扫描（~7min）— 可加参数提速（也可能是刻意的全量口径，需 James 确认）
3. crowding_monitor stdout 未落入独立日志，只有终端输出（报告文件已有内容，影响小）

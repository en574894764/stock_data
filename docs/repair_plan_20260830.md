# stock_data 数据管道修复方案

- **版本**: v1（待评审）
- **日期**: 2026-08-30
- **作者**: Claw
- **状态**: ⏳ 评审中 —— 评审通过后开始写代码

---

## 1. 背景与问题清单

**现状**：数据自 2026-07-15 停更约 6.5 周（最近交易日 8/28）。根因是 quant_sys daemon 挂掉（launchctl 退出码 78）+ crontab 无任务，且现有脚本即使每天跑也**拉不全、校不出**。

代码审查发现的全部缺陷（按严重度）：

| # | 位置 | 缺陷 | 后果 | 实证 |
|---|------|------|------|------|
| P0-1 | backfill_financial.py:225 | `puller(conn, sym, 2025)` 年份硬编码 | 2026 年 Q1/半年报永远补不到 | fundamental/20260630.csv 仅 1 行 |
| P0-2 | backfill_financial.py:169 | `generate_series(2020, year-1)` 排除当年 | 当年缺口不进发现范围 | 同上 |
| P0-3 | validate.py:248 | `cutoff_stale = date(2026, 6, 1)` 硬编码 | 数据晚于 6/1 即判"新鲜"，校验随时间失效 | 今天跑 stale≈0，7567 只停更股全部漏报 |
| P0-4 | 架构 | fetch 写 `daily/` CSV，validate 查 PG | 缺失数永不收敛，循环空转 | 7/15 日志三轮 111→111→111 |
| P1-1 | fetch_and_backup.py | `--latest` 只覆盖 A 股日线 | 港股/ETF/指数/宏观无更新路径 | 港股停 6/3、ETF 停 6/18、指数停 7/10 |
| P1-2 | fetch_and_backup.py:352 | `--from-report` 显式跳过 `.HK` | 港股缺口被主动忽略 | 港股 60 交易日缺口 |
| P1-3 | fetch_and_backup.py:666 | 腾讯源去重 `_line.startswith(today)` 永远 False | 重复写入 + 脏数据 | 000001.SZ 7/15 两行（一行 open=0 vol=0） |
| P1-4 | fetch_and_backup.py:209,540 | 零值过滤只挡"四价全 0" | 半脏数据（open=0, close≠0）照写 | 同上 |
| P1-5 | fetch_and_backup.py:702 | `--latest` 只拉最新一个交易日 | 停更多天后重跑不补中间断档 | — |
| P1-6 | pipeline.py:85-89 | `step_export` 不检查 returncode | 导出失败无感知 | DB 有 7/14 数据，Q3 CSV 停在 7/10 |
| P2-1 | backfill_financial.py | 每只股票拉一次全市场接口再过滤一只 | 500 只 = 500 次全量下载 + 0.3s sleep | — |
| P2-2 | backfill_financial.py | `except: pass` 吞掉全部 DB 错误 | 静默失败 | — |
| P2-3 | backfill_financial.py | fina_indicator（财务指标）无回补逻辑 | 指标表断更 | — |
| P2-4 | export.py:18 | DSN 用 `localhost:5432` TCP，其余脚本用 `/tmp` socket | 连接方式不统一（当前可用，但脆弱） | — |
| P2-5 | fetch_and_backup.py:163 | tushare token 硬编码在源码里 | 安全隐患 | — |
| P2-6 | validate.py:238 | `--quick` 抽查 200 只 | 日常校验有漏检面 | — |
| P3-1 | 调度 | 依赖 quant_sys daemon（已挂）+ 无本地调度 | 单点故障，停更 6.5 周无告警 | launchctl 退出码 78 |

---

## 2. 修复目标

1. **拉全**：每个交易日收盘后自动更新 A股（含北交所）/ 港股 / ETF / 指数 / 财报 / 宏观
2. **校得出**：校验基准随日期动态计算，任何缺口当天发现并推送飞书告警
3. **自愈**：停更任意时长后重跑一次即自动补齐（幂等，可重复执行）
4. **一致**：PostgreSQL 为唯一权威源，所有 CSV/parquet 由导出生成，单一数据流
5. **自治**：stock_data 不依赖 quant_sys daemon 存活，自己挂调度
6. **一次性补齐**当前 6.5 周缺口

---

## 3. 总体架构：数据流单向化

### 现状（两条路打架）

```
路径A: quant_sys daemon → tushare → PG daily_quote → export.py → data/daily 季度CSV
路径B: fetch_and_backup → tushare/腾讯/AKShare → daily/ 按标的CSV（旁路，不进PG）
校验:  validate.py 查 PG —— 与路径B写入目标错位
```

### 目标（单向流）

```
                ┌─ A股+北交所: tushare pro.daily（按 trade_date 全市场）
                ├─ 港股:      tushare pro.hk_daily（无权限则 akshare stock_hk_daily 按标的）
fetch_and_backup├─ ETF:       tushare pro.fund_daily
（只写 PG）      ├─ 指数A股:   tushare pro.index_daily
                └─ 指数海外:   akshare index_global（写 PG index_daily）

backfill_financial（只写 PG）
                ├─ 三大报表: AKShare 按报告期批量（东财源）
                └─ 财务指标: tushare pro.fina_indicator

export.py（PG → CSV，全量幂等）
                ├─ data/daily/{a_shares,etf,hk} 季度打包
                ├─ daily/ 按标的 CSV（新增导出目标）
                ├─ data/fundamental 合并表 + data/meta
                └─ index/*.csv（新增导出目标）

宏观（例外：直写 CSV，无 PG 表，量小频率低）
                └─ fetch_macro.py: akshare 六接口 → macro/*.csv

下游缓存
                └─ restore.py --export-parquet 重建 1d/（Argus 缓存，已有能力，不改）
```

**关键原则**：任何数据只有一条写入路径（PG 或特定 CSV），导出全部幂等可重跑。

---

## 4. 模块方案

### M1 validate.py — 校验基准动态化（P0）

| 改动 | 位置 | 做法 |
|------|------|------|
| cutoff 动态化 | :248 | `cutoff_stale = TODAY - timedelta(days=args.stale_days)`，新增 `--stale-days` 参数默认 7 |
| 清理硬编码注释 | :197-199 | "数据到 2026-06-18 以后" 等文字同步改为动态描述 |
| 日报增加新鲜度总览 | 输出段 | 汇总各数据域（A股/港股/ETF/指数/财报/宏观）MAX(date) vs 最近交易日，直接给出"落后 N 交易日"——本次诊断脚本逻辑固化进去 |
| --quick 语义修正 | :235 | pipeline 日常不再抽查，改为全量跑（核心查询是一条 GROUP BY，实测 <10s，无需抽查） |
| 港股单独口径 | :288 | 港股交易日历与 A 股不同，stale 判定用"落后 A 股基准 + 2 个交易日容差" |

**验收**：今天全量跑，能报出 A股 7567 只 / 港股 2623 只 / ETF / 指数全部缺口明细。

### M2 backfill_financial.py — 财报回补修复（P0）

| 改动 | 做法 |
|------|------|
| 年份参数化 | 删除硬编码 `2025`；`--year` 默认当年，循环范围 `2020 ~ 当年` |
| 当年纳入发现 | `find_gaps` 的 `generate_series(2020, year)`；当年只查**已过披露截止日**的报告期（Q1→4/30、H1→8/31、Q3→10/31、年报→次年4/30），未到截止日的不算缺口 |
| 四报告期循环 | AKShare 按报告期拉：`ak.stock_yjbb_em(date="YYYY0331/0630/0930/1231")`，替换现在只拉 1231 |
| 批量化（性能） | 一次接口调用返回全市场 → 内存中过滤出缺口股票 → 批量 upsert。**从 500 次全量下载降为 4 次**，耗时从 ~5 分钟降到 ~30 秒 |
| fina_indicator 回补 | 新增 puller：tushare `pro.fina_indicator(period=...)`（token 已有，按期全市场返回），写入 financial_indicator 表（已有唯一键，upsert 幂等） |
| 去掉 `except: pass` | 改为记录错误日志 + continue，汇总失败数 |
| ann_date 更新语义 | 东财源无披露日字段，`ann_date` 用拉取当日填充并标注 `report_type` 不变——注意 ON CONFLICT 只更新财务字段，不覆盖已有 ann_date（避免把 quant_sys 写入的真实披露日改掉） |

**不改**（列为后续可选）：字段覆盖维持摘要级（income 10 列 / balance 9 列 / cashflow 7 列）；`fundamental/` 按报告期子目录（84 列 tushare 全字段）不在本模块范围（见决策点 D3）。

**验收**：跑完后 `fundamental/20260630.csv` 对应的 PG 数据 ≥4000 行（8/30 已披露的半年报量级）；Q1 缺口清零。

### M3 fetch_and_backup.py — 数据源补全 + 区间自愈（P1）

| 改动 | 做法 |
|------|------|
| `--latest` 改区间模式 | 从 PG 各表 `MAX(trade_date)+1` 拉到最近交易日（逐日调用），停更 N 天重跑一次自动补齐中间断档 |
| 港股数据源 | 优先 `pro.hk_daily(trade_date=...)` 写入 `daily_quote`（延续现状：港股在 daily_quote 表，hk_quote 废弃不动）；**无接口权限则回退 akshare `stock_hk_daily(symbol=...)` 按标的拉区间**（2623 只 × 一次区间调用，预计 20-40 分钟） |
| ETF 数据源 | `pro.fund_daily(trade_date=...)` 写 `etf_quote`（唯一键 code+trade_date 已有） |
| 指数数据源 | A股指数 `pro.index_daily(trade_date=...)`；海外指数（DJI/SPX/IXIC/N225/FTSE/GDAXI/RUT/HKTECH/HSHKCI/HSI）akshare `index_global` 按标的拉区间，写 PG `index_daily`（唯一键 symbol+trade_date 已有） |
| 写入方向统一 | 所有 fetch 只写 PG，**删除直写 `daily/` CSV 的旁路**（tushare/腾讯/akshare 三个写 CSV 的函数全部改造） |
| 腾讯源去重修复 | 判断改为 `line.split(",")[1] == today`（第二列是 datetime）；该源降级为"PG 不可用时的应急手动通道"，不进日常 pipeline |
| 零值过滤收紧 | 跳过条件改为 `open<=0 or close<=0 or high<=0 or low<=0`（任一价非正即脏），并顺带清理存量脏行（见 M8） |
| token 外置 | 移到 `.env`（gitignore）+ 环境变量注入；源码里的内置 token 删除（建议后续在 tushare 后台换新 token，旧的已进 git 历史） |
| 顺带清理 | 000001.SZ.csv 7/15 起的重复行/全 0 行（M8 统一做） |

**验收**：删除 PG 某测试标的最近 5 天数据 → 跑 `fetch --latest` → 自动补齐 5 天且与前后数据连续。

### M4 export.py — 导出补全 + 健壮性（P1）

| 改动 | 做法 |
|------|------|
| DSN 统一 | 改用 `host=/tmp` socket（与其他脚本一致），保留 `PG_EXPORT_DSN` 覆盖 |
| 新增按标的导出 | `daily/` 按标的 CSV：`SELECT ... FROM daily_quote WHERE ts_code=X ORDER BY trade_date` 全量重写单文件（12433 个文件全量导约 3-5 分钟，每日可接受；增量优化列为后续可选） |
| 新增指数导出 | `index_daily` → `index/*.csv`（按 symbol 分文件） |
| returncode 检查 | 自身失败时非零退出 + 明确日志（配合 M5） |

**验收**：跑一次后，`daily/`、`data/daily/`、`index/`、PG 三方抽查 20 只标的数据完全一致；无重复 (symbol, date) 行。

### M5 pipeline.py — 编排收敛（P1）

| 改动 | 做法 |
|------|------|
| export 结果检查 | `step_export` 检查 returncode，失败 → 飞书告警 + 非零退出 |
| 收敛判据修正 | fetch 改写 PG 后 `stale_daily` 天然可收敛；另加"轮间 stale 无下降则 break + 告警"（杜绝 111→111→111 空转 50 分钟） |
| 宏观步骤 | 流程中插入 `fetch_macro.py`（M6） |
| 告警增强 | 飞书报告增加各数据域新鲜度表 + 缺口 TOP10；export/fetch 任一失败即推告警卡片 |
| 非交易日分支 | 保留现有逻辑，同步使用新的 validate |

### M6 fetch_macro.py（新建）— 宏观直写 CSV（P2）

| 数据 | 接口（akshare） | 频率 |
|------|----------------|------|
| shibor | `macro_china_shibor_all` | 日 |
| 国债收益率（中美） | `bond_zh_us_rate` | 日 |
| LPR | `macro_china_lpr` | 月（每月 20 日） |
| CPI | `macro_china_cpi_monthly` | 月 |
| PMI | `macro_china_pmi` | 月 |
| M0/M1/M2 | `macro_china_money_supply` | 月 |

- 直写 `macro/*.csv`（无 PG 表，量小，不值得建表——见决策点 D2）
- 保持各文件现有列结构与排序习惯（注意 cpi/pmi/money_supply/lpr/shibor 是**倒序**存的，bond_yield 是**正序**——逐一兼容，写入前先读旧文件尾/头判断）
- 月度数据幂等：按期号覆盖

### M7 调度自治（P2）

- 新建 launchd plist `com.stock-data.pipeline.plist`：工作日 18:30 跑 `pipeline.py --cron`（A 股 15:00 收盘，tushare 日线一般 17:00-18:00 就绪；海外指数当日数据北京时间次日 5 点后才有，**海外指数每天会天然滞后一个交易日，属预期**，validate 用容差覆盖）
- 周六 10:00 跑一次全量校验 + 财报回补（周中披露的财报周末补全）
- `RunAtLoad=false`、`KeepAlive=false`（一次性任务，非守护）
- token 通过 plist `EnvironmentVariables` 注入
- quant_sys daemon 的修复**不在本方案范围**（另一个仓库），单独处理；stock_data 不再依赖它

### M8 一次性补数 + 数据清治（评审通过后第一批执行）

顺序执行，全程幂等，每步 git commit 可回滚：

1. **清理存量脏数据**：删除 `daily/` 中重复 (symbol, datetime) 行与全 0 行；PG 中同样清理
2. **A股+北交所补数**：7/15 → 8/28，约 31 个交易日 × ~5500 只 ≈ 17 万行（tushare 逐日，~15 分钟）
3. **港股补数**：6/3 → 8/28，约 60 个交易日 × ~2700 只 ≈ 16 万行（~40 分钟）
4. **ETF 补数**：6/18 → 8/28（~10 分钟）
5. **指数补数**：A股 7/10 →、海外各自断点 → 8/28（~10 分钟）
6. **财报补数**：2026 Q1 + 半年报（M2 修复后跑，~10 分钟）
7. **宏观补数**：shibor 7/13 →、bond_yield 5/29 →、lpr 7/8 月、CPI/PMI/M2 6/7 月（~5 分钟）
8. **全量导出**：export 全跑 + `restore.py --export-parquet` 重建 1d/
9. **全量校验**：validate 全量 + 新鲜度报告，目标 stale=0（真实停牌/退市除外）
10. **git commit + push**

预计总时长 1.5~2.5 小时（含限速 sleep），可后台跑。

---

## 5. 需评审决策的点

| # | 决策点 | 选项 | 我的建议 |
|---|--------|------|---------|
| D1 | 数据权威源 | A. PG 单向流（fetch→PG→export→CSV）<br>B. 维持双路，validate 改查 CSV | **A**。B 改动小但两条路的错位问题迟早复发 |
| D2 | 宏观走哪 | A. 直写 CSV（无 PG 表）<br>B. 建 PG 表统一进导出流 | **A**。6 个低频小文件，建表重构收益低 |
| D3 | `fundamental/` 子目录（84 列全字段，按报告期分文件） | A. 本次不修，标记 deprecated，回测改读 PG<br>B. 加 tushare 全字段回补（按标的×期拉，工作量大）<br>C. 保留现状但补半年报一次性快照 | **C 先行**：补一次性快照保可用；长期转 A。请结合回测侧实际消费方确认 |
| D4 | 港股复权口径 | akshare `stock_hk_daily` 默认不复权，需与现有 6000+ 只历史数据抽样对比确认口径一致 | 补数前先抽 20 只对比 6/3 前后衔接点，不一致则改用 tushare 或加复权参数 |
| D5 | tushare token | A. 沿用现 token（已在 git 历史）<br>B. 换新 token + .env 外置 | **B**。旧 token 视为已泄漏 |
| D6 | `market/` 目录（6186 个按日 parquet，仅 mcp_server.py 读取） | A. 确认使用方后纳入重建<br>B. 废弃 | 需你确认 Argus 是否还在读；**默认 B**（mcp_server 一并清理读取逻辑） |

---

## 6. 实施顺序

```
阶段1 (P0):  M2 backfill → M1 validate          [先修工具]
阶段2 (P1):  M3 fetch → M4 export → M5 pipeline  [数据流重构]
阶段3 (P2):  M6 宏观 → M7 调度                    [补齐自治]
阶段4:       M8 一次性补数 + 全量校验              [用修好的工具补缺口]
阶段5:       试运行 3 个交易日无人值守验收
```

每阶段独立 commit，可单独回滚。阶段 1+2 完成后即可执行 M8 补数（不必等 M6/M7）。

## 7. 总体验收标准

1. ✅ validate 全量：A股/北交所/港股/ETF/指数 stale=0（真实停牌、退市除外）
2. ✅ 三方一致：PG ↔ `daily/` ↔ `data/daily/` 抽查 20 只，日期与 OHLC 完全一致
3. ✅ 无脏行：全库无重复 (symbol, date)；无 open/close≤0 的行
4. ✅ 财报：2026 Q1 + 半年报入库量 ≥4000 行/期
5. ✅ 自愈测试：人工删 PG 任意标的 5 天数据 → 单跑 fetch 自动补齐
6. ✅ 停摆测试：模拟 daemon/pipeline 停 3 天 → 恢复后一次运行补齐全部断档
7. ✅ 连续 3 个交易日 launchd 无人值守运行成功，飞书日报正常推送
8. ✅ 校验灵敏度：手动把 cutoff 设为 3 天再跑，能列出全部滞后标的

## 8. 风险与回滚

| 风险 | 概率 | 缓解 |
|------|------|------|
| tushare 无 hk_daily 权限 | 中 | 方案已含 akshare 兜底；补数前先用 1 次调用验证 |
| 港股复权口径不一致 | 中 | D4：补数前抽样衔接点对比，不一致即停 |
| akshare 接口列名变更 | 中 | 防御性解析（按列名映射，缺列报错不写库）+ 每次运行记录接口版本日志 |
| 补数触发 tushare 限频 | 低 | 逐日调用 + 0.6s sleep，超限自动退避重试 |
| 导出全量重写 daily/ 耗时 | 低 | 实测后若 >10 分钟再优化增量导出（列为后续） |
| 数据流切换期错乱 | 低 | 切换前跑一次全量对账（脚本化）；每步 git commit + PG upsert 幂等，可整段重跑 |

**回滚**：代码按模块独立 commit，`git revert` 即可；数据侧 PG upsert 幂等、CSV 在 git 里逐行可 diff，任何一步发现污染都可定点恢复。

## 9. 附：接口权限验证清单（写代码前先跑）

- [ ] `pro.hk_daily(trade_date=...)` —— 决定港股走哪条路
- [ ] `pro.fund_daily(trade_date=...)`
- [ ] `pro.index_daily(trade_date=...)`
- [ ] `pro.fina_indicator(period=...)`
- [ ] akshare 六个宏观接口连通性 + 列名快照
- [ ] 港股复权口径抽样对比（D4）

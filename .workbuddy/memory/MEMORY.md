# stock_data 项目长期备忘

## 数据链路（2026-08-30 修复后）
- **单向流**：fetch_and_backup.py → PG → scripts/export.py → CSV → git。validate.py 查 PG（与 fetch 写入目标一致，缺口可收敛）
- PG：host=/tmp dbname=investassist user=james；daily_quote 按年分表 (ts_code, trade_year, trade_date) 主键；etf_quote (code,trade_date)；index_daily (symbol,trade_date)
- 港股存 daily_quote（ts_code LIKE '%.HK'），hk_quote 表已废弃
- 调度：launchd `com.stock-data.pipeline`，周一至五 18:30 + 周六 10:00，`pipeline.py --cron`，日志 logs/launchd_{out,err}.log
- 密钥：项目 `.env`（gitignored）——TUSHARE_TOKEN + FEISHU_APP_ID/FEISHU_APP_SECRET；pipeline.py `load_env()` 注入子进程

## 数据源口径
- A股/ETF/A股指数：tushare pro（daily/fund_daily/index_daily，逐日 trade_date=）
- 港股：akshare `stock_hk_daily(symbol, adjust="")` 按标的 90 天窗口（pro.hk_daily 限频 1次/小时不可用；口径已验证 0 偏差）
- 港股指数：sina `stock_hk_index_daily_sina`（HSI/HSTECH/HSCEI 可用；HKTECH/HSHKCI 无源）；DJI/SPX 等海外指数无源保留旧 CSV
- 宏观：akshare（中文列名需映射）；CPI 用 `macro_china_cpi`（统计局），`macro_china_cpi_monthly` 是金十月率报告，别用
- 财报：tushare *_vip 批量接口；须过滤北交所重复挂牌变体（ts_code 含 `!`，如 833243!1.BJ）

## 飞书推送
- 凭证从 `.env` 环境变量读（report_builder `_get_feishu_credentials` 环境变量优先于 settings.json——settings.json 里的飞书凭证已丢失）
- lark-cli 凭证备用位置：`~/.lark-cli/config.json` + 钥匙串（service="lark-cli", account="appsecret:<appId>"）；可用 app `cli_aab9666b25b81cd2`（argus）
- Interactive Card 2.0，报告蓝色/告警红色模板

## 常见陷阱
- zsh `source .env` 不自动 export → 用 `set -a; source .env; set +a`
- psycopg2：SQL 含字面 `%` 时 params 为空不能传空元组（IndexError）
- akshare sina 接口 date 列是 datetime.date → 先 `pd.to_datetime()`
- `pd.to_datetime` 对 YYYYMM 6位字符串歧义解析 → 显式 `format="%Y%m"`
- launchd StartCalendarInterval 的 Weekday：1=周一…6=周六，与 cron 的 0=周日不同
- pipeline.py 的 backfill_financial 调用无 --max 参数（该脚本只有 --dry-run/--year/--years/--symbol/--force/--table）
- launchd Background 下 git add 8000+ CSV 偶发 60s 超时（手动 ~13s），step_backup timeout 必须 ≥600s
- step_backup 必须检查每步 returncode，否则 timeout 静默退出备份会"看似成功实则丢失"
- akshare 港股小盘股/低流动性标的部分时间 akshare 源不更新（如 08033/01049/00626 长期停在 8/19），属源端特性非 pipeline bug
- akshare sina 港股指数（HSI/HSTECH/HSCEI）：`pd.to_datetime(date)` bug 已修；容忍度已收紧为 HK_INDEX_LAG_TOLERANCE_DAYS=3（7 天太宽会漏周末+节假日缺口，实测 2026-08 停在 8/28 四天被误判 fresh）
- launchd ProcessType 已改 **Utility**（原 Background 把 git 拖慢 10~30 倍，4s 的 commit 120s+ 超时）；pipeline step_backup 所有 git timeout ≥300s
- stock_valuation 是**人工估值记录表**（fair_value/ideal_buy_price/折现率），非行情数据，不适用机器质检口径（validate 已移除）
- stocks 表刷新脚本 `scripts/refresh_stocks.py`：tushare stock_basic 全量 upsert；T 前缀退市变体（T600018.SH）symbol 超 varchar(6) 须过滤
- fundamental 四表已按 report_year 拆分导出（`balance_sheet_2025.csv` 等）；历史年份（<=当前年-2）存在即跳过 → git 对象复用；旧整表 CSV 已解除追踪（gitignore 排除）
- **不要用 git-lfs 存每日全量重写的 CSV**：免费配额 1GB 存储/月带宽，每天 ~300MB 新对象 3-4 天爆配额
- **git 历史已 filter-repo 重写（2026-09-01）**：4 个 fundamental 整表大 CSV 的历史 blob 已移除，.git 3.9GB→1.5GB，已 force push；重写前完整备份在 `/Users/james/workspace/stock_data_backup_20260901.bundle`（1.6GB，确认无误后可删）
- filter-repo 踩坑：失败会在 `.git/filter-repo/` 留 `already_ran` 标记导致重跑被拦（EOFError），须先 `rm -rf .git/filter-repo`；默认移除 origin remote 事后重加

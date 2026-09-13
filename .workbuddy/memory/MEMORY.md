# stock_data 项目长期备忘（2026-09-12 压缩重写）

## 数据链路
- 单向流：fetch_and_backup.py → PG → scripts/export.py → CSV → git；validate.py 查 PG
- PG：host=/tmp dbname=investassist user=james；daily_quote 按年分表 PK(ts_code,trade_year,trade_date)；etf_quote(code,trade_date) **code=6位裸码**；index_daily(symbol,trade_date)；港股在 daily_quote(%.HK)
- 调度：launchd com.stock-data.pipeline 周一至五18:30+周六10:00；ProcessType=**Utility**（Background 拖慢 git 10-30 倍）；step_backup git timeout≥300s 且须查每步 returncode
- 密钥：项目 .env（TUSHARE_TOKEN+FEISHU_*）；飞书凭证 env 优先（settings.json 已丢）；lark-cli 备用 ~/.lark-cli/config.json+钥匙串；Interactive Card 2.0
- 源口径：A股/ETF/指数=tushare；港股=akshare stock_hk_daily(小盘股源端停更是特性)；港股指数=sina(HSI/HSTECH/HSCEI, 容忍度3天)；CPI 用 macro_china_cpi；财报 *_vip 须滤北交所 `!` 变体

## 关键坑
- **同文件多个 Edit 并行互相覆盖**，Edit 必须串行
- **因子宽表股票池 ⊄ 行情矩阵池**：.loc 取数前必 intersection（踩过3次）
- read_sql / pivot_table 的 trade_date 是 date 对象 vs Timestamp：`in` 永远 False → 任何宽表构建后统一 index=to_datetime（IC 静默全 nan 的元凶，踩过 2 次）
- **factor_cache parquet 刷新条件**：sl.load_factor 仅当 PG max > 缓存 max 才更新 → 重训/回填后若日期范围未超缓存须手动删 `factor_cache/<factor>.parquet`，否则静默沿用旧值（表现为 A/B 对比"完全相同"的假阴性）
- 千万行 upsert：CSV→copy_expert→staging→ON CONFLICT（executemany 不可用）
- ln_mv=-ln(总市值万元)（方向翻转）；daily_quote.pct_chg 为百分数且含除权修正（连乘=全收益，已验证）
- zsh source .env 需 set -a；launchd Weekday 1=周一；回测成本按调仓期分摊（每日扣全额=年化-47%假象）
- git 历史已 filter-repo 重写（.git 1.5GB）；勿用 git-lfs 存每日重写 CSV；filter-repo 重跑须先 rm -rf .git/filter-repo

## 因子与生产策略
- factor_value PK(factor_name,trade_date,ts_code)，值大=预期收益高，2015起；step_factor 每日增量（backfill_daily_basic→compute_factors→crowding→lgbm_signal）
- 实证：技术指标 IC 弱不进组合；ROE 单因子失效；IC 加权跑输等权；价值4因子有效但 Q4>Q5 价值陷阱；**质量是门限不是排序器**（纯 ROE Top50 十年 -3.8%）；双门限(价值∩ROE)最优 6.3%/夏普0.36；市场 PB 分位对沪深300 未来1年有预测力但线性仓位规则反噬（择时要宽阈值低频）；月度vs年度调仓仅差0.7pp
- **prod_lgbm_neu = LGBM+市值中性化**（21.5%/0.97/-33.6%，中性化超额+11.1pp=真α）；lgbm_score 稀疏因子只在 2019 起 20 日网格，回测必须锚 2019-01（锚点差 5pp 年化）；生产预测只用 t 截面特征
- **组合优化=min_var_cap10 最优**（单票10%上限，样本外夏普 1.44-1.56，回撤减半）；选股区跳过7%与 min_var 叠加反而变差（二选一选 min_var）；strategy_lib 支持 weighting 非等权
- 择时层四重否决（timing/confirm/trend_filter 全负贡献）：6因子α集中在趋势未确认日，趋势门删掉喂食日；"上涨才做多"归宿=ETF动量卫星仓
- 双引擎回测：backtest_ts.py(vectorbt, 绩效自研244bar)+combo_backtest.py；回测结论报多锚点均值
- 模拟盘：step_signals 每日 generate→execute--simulate→build_nav→daily_review→benchmark_track（连续3月跑输飞书红色告警）；prod_6f_eq 已对齐回测口径（top_n=30 + weighting=min_var cap10/窗口60/剔<0.5%），prod_lgbm_neu 同口径
- **LGBM 特征集 = factor_value 自动发现**（DISTINCT factor_name）→ 实验因子入库即自动成为生产特征；compute_lgbm_signal 已有 `EXCLUDE_FACTORS` 显式排除表，新实验因子裁决后须登记，否则污染生产
- 隔夜-日内结构因子已裁决**无增益不入生产**（id_mom_20 vs ret_20d_rev 相关 0.83、on_vol_20 vs ivol_60 相关 0.64；等权池 10 方案全跑输、LGBM 30 特征 OOS 夏普 1.56→1.11）；隔夜收益口径 = open/pre_close-1（pre_close 为除权调整口径，已验证 close/pre_close-1 ≡ pct_chg/100）

## 十倍股实证（2026-09-12）
- scripts/tenbagger_analysis.py + outputs/tenbagger_analysis.json；2016-09-12→2026-09-11 终值≥10x：20/2637（0.76%），电子硬件链 12/20
- 共同点：起点不便宜（PE中位81 vs 市场63）、市值持平、ROE平庸（中际旭创1.9%/PE183）、盈利驱动（净利+21.5x）、13/20 在2024年后才兑现（幂律，剔最高3周收益减半）
- 2016时点静态规则天花板~2%：便宜派0/20，质量+成长2/20（牧原+兆易）→ 十倍股核心=产业趋势拐点不可外推；方法论=趋势定位+成长宽筛+追涨确认（东吴：涨50%后概率22→70%，5x→10x 43%）+拿住+逻辑证伪才卖；价值=防守，GARP=交集

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

## 价值投资落地工具（2026-09-14）
- `scripts/stock_diligence.py`：排雷(6红旗+Beneish M-Score+Altman Z-Score)+选股(Piotroski F9)+估值(PE/PB分位 CUME_DIST)+买卖点；输出 `outputs/stock_diligence.csv`(全市场5015只评分)+`.json`+`_report.html`
- **阈值本地化**：Altman Z<1.81 在A股命中53%、Beneish M>-2.22 命中24%（美股口径偏严，Z中位1.76/96.8%非安全区）→ 实际用相对分位；金融股(银行/保险/证券/多元金融)剔除出 M/Z
- 口径：三表 `report_type` varchar，'4'=年报('1'Q1/'2'中报/'3'Q3)；缺 DEPI折旧(取1.0中性)/留存收益(用归母权益近似)/商誉字段
- 工程：factor_cache `_val_pct.parquet` 等按 last_date 缓存估值分位（避免每次6min全表CUME_DIST）；node=`.../22.22.2-3/bin/node`

## 风格归因（2026-09-14，`scripts/attribution.py` → `reports/attribution.md`）
- **6因子组合 = smart-beta，不是 alpha**：92 期分解（算术累计）equal 市场 +84%/风格 +30%/特异 α **−14%（t=−0.90 不显著）**；min_var_cap10 市场 +78%/风格 +30%/α **−9%（t=−0.51）** —— 剔除风格无可识别选股力
- **方法必守**：风格因子收益须在**全池**截面 OLS 估计（只用 Top30 会因选股内生性污染 f_k）；加市场截距项；暴露用 **rank-normal** 而非原始 z（原始 z 报出"价值暴露 +4.23σ"不可解释，换口径后 α 由正转负=**估计口径即结论**）
- 全池因子 IC：turnover_20 +7.2% / ivol_60 +6.9% / ret_20d_rev +6.6% / ln_mv +5.1% / ep_ttm +3.2% / sue_gr +0.2% / **roe_lf −1.3%（负向）**
- **五分层前向收益（年化）**：size Q1+11.2→Q5**+25.2**（单调，价差14.1）、reversal +2.1→**+21.8**（单调，价差19.7）但组合暴露≈0（−0.10σ/+0.35σ）；**ep_ttm Q4+18.0>Q5+14.1（陷阱）却是最大暴露 +1.99σ**；roe_lf 完全递减（Q1+16.2→Q5+11.8）；lowvol/liquidity 的 Q3-Q4 见顶（减仓高波动就够，再压榨无增益）
- **min_var_cap10 真实作用 = 更极端的有效因子暴露**（低波0.90→1.40σ、换手0.66→1.00σ、小市值−0.10→−0.32σ、成长0.79→0.57σ），非更优权重；代价 **银行行业权重 17.9%→27.8%（+9.9pp）**
- **⚠ 反向结论**：给 `score_cross` 加 winsorize/rank-normal 的建议**已被实验否决**（`scripts/winsorize_test.py`）：多相位均值 raw_z **15.9%** vs rank_normal **13.0%（−3.0pp）**，主相位 18.7%/1.11 vs 12.0%/0.77 → **原始 z 的尾部加权贡献真实收益**。但 raw_z 相位离散 8.8pp（最差10.1%）vs rank_normal 2.9pp（最差11.6%）= 高均值/高方差，本样本内**未裁决，不动生产**
- **未否决且风险明确的一条**：组合层加行业 ≤15% + 风格暴露 ±1σ 约束

## 相位稳健性（2026-09-14，`scripts/phase_robustness.py` → `reports/phase_robustness.md`）
- **判据**：`fws.offset_grid` 的 offset 取 **0..19（一个完整月度周期）**，不是 2/5/10/15 四个。**4 相位会严重失真离散度估计**（4 相位报 rank 2.9pp vs raw 8.8pp；20 相位实为 2.2pp vs 3.4pp）
- **裁决：不拉齐尺度**。20 相位均值 raw **14.7%** / 夏普 0.86 / 最差 9.8% ＞ rank 12.3% / 0.81 / 8.2% ＞ winsor(±3σ) 11.3% / 0.65 / 6.3%；胜率 raw 18/20（vs winsor）、15/20（vs rank）
  → **原始 z 的尾部加权是 feature 不是 bug**；`winsor` 粗暴裁尾全面最差；rank 唯一优势=回撤(−19.3% vs −22.7%)与 OOS 夏普(1.06 vs 0.98)
- **回测口径修正**：主相位 18.7% 偏高，**20 相位期望 14.7%、最差 9.8%** → 汇报与实盘预期锚 **14~15%**，回撤预算 −25% 量级
- **🔍 待验证的更重要发现**：调仓日「日历位置」有强结构，三口径同向 —— raw 月初(off 0-7) 18.0% / 月中(8-14) 10.9% / 月末(15-19) 14.5%，**月初−月中 +7.1pp**（组内 std 仅 1.6/0.8pp）。疑似 A 股月初效应，须按 in-month 日序重验
- **不拉齐 → 因子研究仍被尺度污染 → 改判据不改生产**：任何因子增删/替换**必须同时跑 raw 与 rank 两口径，只有两边同向才采信**（sue_q_np 即 raw 变差而 rank 改善的反例）

## QFA 单季 + SUE 多子因子（2026-09-14，`scripts/compute_qfa_sue.py` / `qfa_sue_eval.py`）
- 9 因子：q_np_yoy/q_or_yoy/q_op_yoy/q_roe_d/q_acc_np/sue_q_np/sue_q_or/sue_q_op/sue_q_np_d
- **单季口径确实更锐利**（验证华泰）：sue_q_np_d IC **+2.19%/ICIR 0.40/正率65%**；sue_q_op +1.94%；sue_q_np +1.88% —— 均 > 现有 sue_gr +0.85% / sue_delta +1.38%；q_acc_np 与成长因子相关仅 **0.03/0.05**（真新信息）
- **但组合层零增益**：新增 sue_q_np 多相位均值 **+0.0pp**；**替换 sue_delta→sue_q_np 崩 4.7pp**（18.7%→13.7%）
- **机制**：非 sue_delta 有独特信息，而是 **sue_q_np 尾部更厚 → 在未 winsorize 的合成分数里挤占其他因子名义权重**（证据：换 rank-normal 后劣化从 −4.7pp 收窄到 −1.6pp）
- **一律不入库**（LGBM 自动发现会污染生产）；首跑遗留 q_np_yoy 已登记 `EXCLUDE_FACTORS`
- 单季还原：`income.report_type` ∈{'1','2','3','4'}=报告期，金额字段**年内累计**；q1=cum1、qk=cum_k−cum_{k−1}（季度连续）；同 (code,year,type) **取最早公告**；SUE 基准=**季节性随机游走**（去年同季）+ 相对惊喜 s=(q_t−q_{t−4})/|q_{t−4}| 按自身 8 季标准化；截断 yoy±5/SUE±15
- 裁决文档 `reports/qfa_sue_conclusion.md`

## 方法论（2026-09-14 新增）
- **归因/回测给出的"改进建议"必须单独做同口径对照实验**——本次建议 #1（winsorize）就被自己的对照实验否决，说明"看起来该改"≠"改了更好"
- 单因子 IC 高 **不能**预测组合贡献（合成分数未 winsorize 时，因子有效权重由尾部厚度决定，≠名义权重）
- 评估阶段**不要先落库**：9 因子 × 11.8M 行 = 106M 行 upsert 单因子 >6min；在内存构造宽表即可，只写胜出者
- 千万行 upsert 性能：`to_csv` **不要传 `float_format`**（会掉出 C 快速路径，11.8M 行多花 5 倍时间）；`pit_wide` 用 numpy 预分配再切片赋值（逐列 DataFrame 赋值慢 10 倍）

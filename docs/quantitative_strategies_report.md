# 量化交易策略大全

> 生成时间：2026-07-02  
> 数据来源：CSDN、BigQuant、Substack、知乎、GitHub  
> 用途：策略选型参考，所有代码为示例框架，实盘前需充分回测验证

---

## 目录

1. [策略分类总览](#一策略分类总览)
2. [趋势跟踪类](#二趋势跟踪类)
3. [均值回归类](#三均值回归类)
4. [动量策略类](#四动量策略类)
5. [多因子选股类](#五多因子选股类)
6. [统计套利类](#六统计套利类)
7. [网格与价差类](#七网格与价差类)
8. [事件驱动类](#八事件驱动类)
9. [日内突破类](#九日内突破类)
10. [机器学习类](#十机器学习类)
11. [波动率策略类](#十一波动率策略类)
12. [风控与仓位管理](#十二风控与仓位管理)
13. [策略组合建议](#十三策略组合建议)

---

## 一、策略分类总览

| 大类 | 子策略 | 适用市场 | 持仓周期 | 胜率特征 | 风险等级 |
|------|--------|----------|----------|----------|----------|
| 趋势跟踪 | 双均线、MACD、海龟、通道突破、SAR | 单边行情 | 中长线 | 低胜率高赔率 | ★★★ |
| 均值回归 | 布林带、RSI、KDJ | 震荡行情 | 中短线 | 高胜率低赔率 | ★★ |
| 动量策略 | 时间序列动量、横截面动量、追涨 | 趋势市 | 中短线 | 中等 | ★★★ |
| 多因子选股 | 价值/动量/质量/低波/规模 | 全市场 | 月频/季频 | 稳健 | ★★ |
| 统计套利 | 配对交易、期现套利、跨品种套利 | 市场中性 | 短中线 | 高胜率 | ★★ |
| 网格交易 | 价格网格、时间网格 | 震荡行情 | 高频 | 积累型 | ★★★★ |
| 事件驱动 | 财报、ST摘帽、涨停、资产重组 | 事件期 | 短线 | 爆发型 | ★★★★ |
| 日内突破 | 伦敦突破、双推力、开盘区间 | 日内 | 当日 | 中等 | ★★★ |
| 机器学习 | XGBoost选股、LSTM预测、CNN技术图 | 全市场 | 自适应 | 数据依赖 | ★★★ |
| 波动率策略 | VIX择时、期权跨式、波动率套利 | 期权/期货 | 中短线 | 对冲型 | ★★★★ |

---

## 二、趋势跟踪类

**核心思想**：顺势而为，不预测顶底。

### 2.1 双均线策略 (MA Cross)

**逻辑**：短期均线上穿长期均线（金叉）买，下穿（死叉）卖。

```python
import numpy as np
import pandas as pd

def ma_cross_strategy(df, fast=5, slow=20):
    """
    双均线策略
    df: DataFrame, 需包含 close 列
    返回: signals (1=买入, -1=卖出, 0=持有)
    """
    df = df.copy()
    df['ma_fast'] = df['close'].rolling(fast).mean()
    df['ma_slow'] = df['close'].rolling(slow).mean()
    df['signal'] = 0
    
    # 金叉: prev ma_fast <= ma_slow, curr ma_fast > ma_slow
    df.loc[(df['ma_fast'].shift(1) <= df['ma_slow'].shift(1)) & 
           (df['ma_fast'] > df['ma_slow']), 'signal'] = 1
    
    # 死叉: prev ma_fast >= ma_slow, curr ma_fast < ma_slow
    df.loc[(df['ma_fast'].shift(1) >= df['ma_slow'].shift(1)) & 
           (df['ma_fast'] < df['ma_slow']), 'signal'] = -1
    
    return df['signal']
```

**适用**：趋势明显的市场（A股牛短熊长，需配合趋势过滤）  
**缺点**：震荡市假信号多，需要加入ADX等趋势滤波器

---

### 2.2 MACD 策略

**逻辑**：DIF(快慢EMA差) 上穿 DEA(信号线) 买入，下穿卖出。

```python
def macd_strategy(df, fast=12, slow=26, signal=9):
    """
    MACD策略
    """
    df = df.copy()
    df['ema_fast'] = df['close'].ewm(span=fast, adjust=False).mean()
    df['ema_slow'] = df['close'].ewm(span=slow, adjust=False).mean()
    df['dif'] = df['ema_fast'] - df['ema_slow']
    df['dea'] = df['dif'].ewm(span=signal, adjust=False).mean()
    df['macd'] = 2 * (df['dif'] - df['dea'])  # MACD柱
    
    df['signal'] = 0
    df.loc[(df['dif'].shift(1) <= df['dea'].shift(1)) & 
           (df['dif'] > df['dea']), 'signal'] = 1
    df.loc[(df['dif'].shift(1) >= df['dea'].shift(1)) & 
           (df['dif'] < df['dea']), 'signal'] = -1
    
    return df['signal'], df['dif'], df['dea'], df['macd']
```

**增强版**：DIF在零轴上方的金叉做多，零轴下方的死叉做空。

---

### 2.3 海龟交易法则 (Turtle Trading)

**逻辑**：价格突破N日高点入场，跌破M日低点止损。最经典的趋势跟踪范式。

```python
def turtle_strategy(df, entry_period=20, exit_period=10, atr_period=20):
    """
    海龟交易策略
    entry_period: 入场通道周期（经典=20）
    exit_period: 离场通道周期（经典=10）
    atr_period: ATR周期
    """
    df = df.copy()
    df['high_entry'] = df['high'].rolling(entry_period).max()
    df['low_entry'] = df['low'].rolling(entry_period).min()
    df['high_exit'] = df['high'].rolling(exit_period).max()
    df['low_exit'] = df['low'].rolling(exit_period).min()
    
    # ATR 计算（True Range 的滚动均值）
    df['tr'] = np.maximum(
        df['high'] - df['low'],
        np.maximum(
            abs(df['high'] - df['close'].shift(1)),
            abs(df['low'] - df['close'].shift(1))
        )
    )
    df['atr'] = df['tr'].rolling(atr_period).mean()
    
    # 仓位：1个ATR对应1%风险
    df['unit'] = 0.01 / df['atr']  # 每单位仓位
    
    df['signal'] = 0
    df.loc[df['close'] > df['high_entry'].shift(1), 'signal'] = 1   # 突破买入
    df.loc[df['close'] < df['low_exit'].shift(1), 'signal'] = -1    # 跌破离场
    
    return df['signal'], df['atr']
```

**优势**：规则清晰、完全客观  
**A股适配**：需使用唐奇安通道替代简单高低点，考虑涨跌停限制

---

### 2.4 通道突破策略 / 唐奇安通道 (Donchian Channel)

```python
def donchian_strategy(df, period=20):
    """
    唐奇安通道突破
    """
    df = df.copy()
    df['upper'] = df['high'].rolling(period).max()
    df['lower'] = df['low'].rolling(period).min()
    df['mid'] = (df['upper'] + df['lower']) / 2
    
    df['signal'] = 0
    df.loc[df['close'] > df['upper'].shift(1), 'signal'] = 1
    df.loc[df['close'] < df['lower'].shift(1), 'signal'] = -1
    
    return df['signal'], df['upper'], df['lower']
```

---

### 2.5 Parabolic SAR 策略

```python
def parabolic_sar_strategy(df, acceleration=0.02, maximum=0.2):
    """
    Parabolic SAR 趋势跟踪 + 止损
    """
    import talib
    df = df.copy()
    df['sar'] = talib.SAR(df['high'], df['low'], 
                          acceleration=acceleration, maximum=maximum)
    
    df['signal'] = 0
    # SAR在价格下方 = 上升趋势 → 做多
    df.loc[(df['sar'].shift(1) >= df['close'].shift(1)) & 
           (df['sar'] < df['close']), 'signal'] = 1
    
    # SAR在价格上方 = 下降趋势 → 平仓
    df.loc[(df['sar'].shift(1) < df['close'].shift(1)) & 
           (df['sar'] >= df['close']), 'signal'] = -1
    
    return df['signal'], df['sar']
```

---

## 三、均值回归类

**核心思想**：涨多了会跌，跌多了会涨。在震荡市赚反复波动的钱。

### 3.1 布林带策略 (Bollinger Bands)

```python
def bollinger_strategy(df, period=20, std_dev=2.0):
    """
    布林带均值回归策略
    """
    df = df.copy()
    df['sma'] = df['close'].rolling(period).mean()
    df['std'] = df['close'].rolling(period).std()
    df['upper'] = df['sma'] + std_dev * df['std']
    df['lower'] = df['sma'] - std_dev * df['std']
    df['bandwidth'] = (df['upper'] - df['lower']) / df['sma']  # 带宽
    
    df['signal'] = 0
    # 价格触及/跌破下轨 → 买入
    df.loc[df['close'] <= df['lower'], 'signal'] = 1
    # 价格触及/突破上轨 → 卖出
    df.loc[df['close'] >= df['upper'], 'signal'] = -1
    
    return df['signal'], df['upper'], df['lower'], df['sma']
```

**优化**：带宽收缩时（低波动）等待突破，带宽扩张时（高波动）做均值回归。

---

### 3.2 RSI 超买超卖策略

```python
def rsi_strategy(df, period=14, oversold=30, overbought=70):
    """
    RSI超买超卖策略
    """
    df = df.copy()
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / (loss + 1e-10)
    df['rsi'] = 100 - (100 / (1 + rs))
    
    df['signal'] = 0
    # RSI从超卖区回升 → 买入
    df.loc[(df['rsi'].shift(1) < oversold) & (df['rsi'] >= oversold), 'signal'] = 1
    # RSI从超买区回落 → 卖出
    df.loc[(df['rsi'].shift(1) > overbought) & (df['rsi'] <= overbought), 'signal'] = -1
    
    return df['signal'], df['rsi']
```

---

### 3.3 KDJ 策略

```python
def kdj_strategy(df, n=9, oversold=20, overbought=80):
    """
    KDJ策略：K金叉D且在超卖区→买入，K死叉D且在超买区→卖出
    """
    df = df.copy()
    low_n = df['low'].rolling(n).min()
    high_n = df['high'].rolling(n).max()
    
    rsv = (df['close'] - low_n) / (high_n - low_n + 1e-10) * 100
    
    df['k'] = rsv.ewm(com=2, adjust=False).mean()
    df['d'] = df['k'].ewm(com=2, adjust=False).mean()
    df['j'] = 3 * df['k'] - 2 * df['d']
    
    df['signal'] = 0
    # K上穿D且K < oversold → 金叉买入
    df.loc[(df['k'].shift(1) <= df['d'].shift(1)) & 
           (df['k'] > df['d']) & (df['k'] < oversold), 'signal'] = 1
    # K下穿D且K > overbought → 死叉卖出
    df.loc[(df['k'].shift(1) >= df['d'].shift(1)) & 
           (df['k'] < df['d']) & (df['k'] > overbought), 'signal'] = -1
    
    return df['signal'], df['k'], df['d'], df['j']
```

---

## 四、动量策略类

**核心思想**：强者恒强，弱者恒弱。近期表现好的标的会继续好。

### 4.1 时间序列动量 (Time-Series Momentum)

```python
def ts_momentum_strategy(df, lookback=60):
    """
    时间序列动量：过去N天累计收益 > 0 → 买入
    """
    df = df.copy()
    df['return_lookback'] = df['close'].pct_change(lookback)
    df['signal'] = (df['return_lookback'] > 0).astype(int)
    return df['signal']
```

### 4.2 横截面动量 (Cross-Sectional Momentum)

```python
def cs_momentum_strategy(prices_df, lookback=60, top_n=10):
    """
    横截面动量：买入过去N日涨幅前top_n的股票
    prices_df: DataFrame, 行=日期, 列=股票代码, 值=收盘价
    """
    returns = prices_df.pct_change(lookback)
    ranks = returns.rank(axis=1, ascending=False)
    
    # top_n 的股票信号为1 (买入), 其余为0
    signals = (ranks <= top_n).astype(int)
    return signals
```

### 4.3 Awesome Oscillator 动量

```python
def awesome_oscillator_strategy(df, fast=5, slow=34):
    """
    AO (Awesome Oscillator) 动量策略
    使用中位数价格 (H+L)/2 替代收盘价
    """
    df = df.copy()
    df['median'] = (df['high'] + df['low']) / 2
    df['ao'] = (df['median'].rolling(fast).mean() - 
                df['median'].rolling(slow).mean())
    
    df['signal'] = 0
    # 连续两日AO上升 → 买入
    df.loc[(df['ao'] > df['ao'].shift(1)) & 
           (df['ao'].shift(1) > df['ao'].shift(2)), 'signal'] = 1
    # 连续两日AO下降 → 卖出
    df.loc[(df['ao'] < df['ao'].shift(1)) & 
           (df['ao'].shift(1) < df['ao'].shift(2)), 'signal'] = -1
    
    return df['signal'], df['ao']
```

---

## 五、多因子选股类

**核心思想**：综合多维度因子评分，选出最优组合。机构量化主流方法。

### 5.1 经典因子体系

| 因子类别 | 代表因子 | 计算方式 |
|----------|----------|----------|
| 价值因子 | PE、PB、PS | 越低越好 |
| 质量因子 | ROE、毛利率、负债率 | 越高越好 |
| 动量因子 | 过去N月收益 | 越高越好 |
| 低波动因子 | 过去N日收益率标准差 | 越低越好 |
| 规模因子 | 流通市值 | 越小越好（A股效应） |
| 成长因子 | 净利润增长率、营收增长率 | 越高越好 |
| 换手率因子 | 日均换手率 | 适中或低 |
| 情绪因子 | 北向资金净流入、主力净流入 | 越高越好 |

### 5.2 多因子框架代码

```python
import numpy as np
import pandas as pd

def multi_factor_model(factor_data, weights=None):
    """
    多因子选股模型
    factor_data: DataFrame, 行=股票, 列=因子名（已标准化/去极值/中性化）
    weights: dict, 各因子权重
    返回: 每只股票的综合得分
    """
    if weights is None:
        weights = {
            'value': 0.20,      # 价值
            'quality': 0.15,    # 质量
            'momentum': 0.15,   # 动量
            'low_vol': 0.10,    # 低波动
            'size': 0.15,       # 小市值
            'growth': 0.15,     # 成长
            'sentiment': 0.10,  # 情绪
        }
    
    # 综合评分 = Σ(因子值 × 权重)
    composite_score = pd.Series(0, index=factor_data.index)
    for factor, weight in weights.items():
        if factor in factor_data.columns:
            composite_score += factor_data[factor] * weight
    
    return composite_score.sort_values(ascending=False)

def standardize_factors(df):
    """因子标准化：去极值 + Z-score标准化"""
    from scipy import stats
    
    result = df.copy()
    for col in result.columns:
        # 去极值（MAD法）
        median = result[col].median()
        mad = np.median(np.abs(result[col] - median))
        upper = median + 3 * 1.4826 * mad
        lower = median - 3 * 1.4826 * mad
        result[col] = result[col].clip(lower, upper)
        
        # Z-score标准化
        result[col] = (result[col] - result[col].mean()) / result[col].std()
    
    return result
```

### 5.3 Fama-French 三因子模型

```python
def fama_french_three_factor(returns, mkt_ret, smb, hml):
    """
    Fama-French三因子模型
    returns: 个股/组合超额收益
    mkt_ret: 市场超额收益
    smb: 小盘股-大盘股收益
    hml: 高PB-低PB收益
    """
    import statsmodels.api as sm
    
    X = pd.DataFrame({'MKT': mkt_ret, 'SMB': smb, 'HML': hml})
    X = sm.add_constant(X)
    
    model = sm.OLS(returns, X).fit()
    return model.summary()
```

---

## 六、统计套利类

**核心思想**：找到价格走势高度相关的资产对，利用价差偏离均值后回归获利。

### 6.1 配对交易 (Pairs Trading)

```python
import statsmodels.api as sm
from statsmodels.tsa.stattools import coint

def find_cointegrated_pairs(prices_df, p_value=0.05):
    """
    寻找协整股票对
    prices_df: 行=日期, 列=股票代码 (log价格)
    返回: 协整对列表 [(stock1, stock2, pvalue)]
    """
    n = prices_df.shape[1]
    pairs = []
    
    for i in range(n):
        for j in range(i + 1, n):
            s1, s2 = prices_df.iloc[:, i], prices_df.iloc[:, j]
            score, pval, _ = coint(s1, s2)
            if pval < p_value:
                pairs.append((prices_df.columns[i], prices_df.columns[j], pval))
    
    return sorted(pairs, key=lambda x: x[2])

def pairs_trading_signal(s1_prices, s2_prices, window=60, entry_z=2.0, exit_z=0.5):
    """
    配对交易信号生成
    entry_z: 入场Z-score阈值
    exit_z: 离场Z-score阈值
    """
    # 计算对冲比率
    s1 = sm.add_constant(s1_prices)
    model = sm.OLS(s2_prices, s1).fit()
    hedge_ratio = model.params.iloc[1]
    
    # 价差 = stock2 - hedge_ratio * stock1
    spread = s2_prices - hedge_ratio * s1_prices
    
    # Z-score
    spread_mean = spread.rolling(window).mean()
    spread_std = spread.rolling(window).std()
    z_score = (spread - spread_mean) / spread_std
    
    signals = pd.Series(0, index=s1_prices.index)
    # 价差异常高 → s2高估 → 做空s2, 做多s1
    signals[z_score > entry_z] = -1
    # 价差异常低 → s2低估 → 做多s2, 做空s1
    signals[z_score < -entry_z] = 1
    # 价差回归 → 平仓
    signals[abs(z_score) < exit_z] = 0
    
    return signals, spread, z_score, hedge_ratio
```

---

## 七、网格与价差类

**核心思想**：预设价格区间，跌了买、涨了卖，反复赚差价。

### 7.1 网格交易策略

```python
def grid_trading_strategy(current_price, grid_low, grid_high, grid_count, capital):
    """
    网格交易策略
    grid_low: 网格下限
    grid_high: 网格上限
    grid_count: 网格层数
    capital: 总资金
    """
    grid_size = (grid_high - grid_low) / grid_count
    per_grid_capital = capital / grid_count
    
    grids = []
    for i in range(grid_count):
        buy_price = grid_low + i * grid_size
        sell_price = buy_price + grid_size  # 上涨一格卖出
        grids.append({
            'level': i,
            'buy_price': buy_price,
            'sell_price': sell_price,
            'shares': int(per_grid_capital / buy_price / 100) * 100,
            'status': 'pending'  # pending / holding
        })
    
    return grids

def check_grid_signal(price, grids):
    """
    检查当前价格触发哪些网格操作
    """
    buy_signals = []
    sell_signals = []
    
    for grid in grids:
        if grid['status'] == 'pending' and price <= grid['buy_price']:
            buy_signals.append(grid)
            grid['status'] = 'holding'
        elif grid['status'] == 'holding' and price >= grid['sell_price']:
            sell_signals.append(grid)
            grid['status'] = 'pending'
    
    return buy_signals, sell_signals
```

**风险**：单边行情中浮亏持续扩大，必须设置止损线。

---

## 八、事件驱动类

**核心思想**：基于财报、政策、公告等事件进行交易。

### 8.1 财报超预期策略

```python
def earnings_surprise_strategy(actual_eps, estimated_eps, price):
    """
    财报超预期策略
    超预期比例 > 10% 且 高于去年同期的 → 买入
    """
    surprise_pct = (actual_eps - estimated_eps) / abs(estimated_eps)
    
    signal = 0
    if surprise_pct > 0.10:
        signal = 1   # 大幅超预期 → 买入
    elif surprise_pct < -0.10:
        signal = -1  # 大幅不及预期 → 卖出
    
    return signal, surprise_pct
```

### 8.2 ST摘帽策略

```python
def st_delisting_strategy(stock_info):
    """
    ST摘帽策略：买入刚摘帽的ST股
    逻辑：市场对摘帽股利好消息通常反应滞后，短线有套利空间
    """
    if stock_info['is_st'] == False and stock_info['was_st_yesterday'] == True:
        return 'BUY'  # 刚摘帽 → 买入
    return 'HOLD'
```

### 8.3 龙虎榜策略

```python
def longhu_bill_strategy(org_flow, retail_flow, threshold=0.3):
    """
    龙虎榜策略：机构净买入占比 > threshold → 跟买
    org_flow: 机构净买入额
    retail_flow: 游资净买入额
    """
    total_flow = org_flow + retail_flow
    org_ratio = org_flow / total_flow if total_flow > 0 else 0
    
    if org_ratio > threshold:
        return 'BUY'  # 机构主导 → 跟买
    return 'HOLD'
```

---

## 九、日内突破类

### 9.1 伦敦突破策略 (London Breakout)

```python
def london_breakout_strategy(df, lookback_hour=7):
    """
    伦敦突破策略：在 7:00-7:59 GMT 记录最高最低价，
    价格突破 → 入场，当日收盘平仓
    """
    # 筛选7点时段数据
    pre_open = df[df.index.hour == lookback_hour]
    
    if len(pre_open) == 0:
        return 'HOLD'
    
    high_range = pre_open['high'].max()
    low_range = pre_open['low'].min()
    
    current = df.iloc[-1]['close']
    
    if current > high_range:
        return 'LONG'
    elif current < low_range:
        return 'SHORT'
    return 'HOLD'
```

### 9.2 Dual Thrust 策略

```python
def dual_thrust_strategy(df, n=5, k1=0.5, k2=0.5):
    """
    Dual Thrust 日内突破策略
    n: 回溯天数
    k1: 上突破系数
    k2: 下突破系数
    """
    df = df.copy()
    hh = df['high'].shift(1).rolling(n).max()
    ll = df['low'].shift(1).rolling(n).min()
    hc = df['close'].shift(1).rolling(n).max()
    lc = df['close'].shift(1).rolling(n).min()
    
    range_val = np.maximum(hh - lc, hc - ll)
    open_price = df['open']
    
    upper = open_price + k1 * range_val
    lower = open_price - k2 * range_val
    
    df['signal'] = 0
    df.loc[df['close'] > upper, 'signal'] = 1
    df.loc[df['close'] < lower, 'signal'] = -1
    
    return df['signal']
```

---

## 十、机器学习类

### 10.1 XGBoost 股票涨跌预测

```python
import xgboost as xgb
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score

def xgboost_stock_predictor(features_df, target, test_size=0.2):
    """
    用XGBoost预测股票次日涨跌
    features_df: 特征矩阵 (因子值)
    target: 标签 (1=上涨, 0=下跌)
    """
    X_train, X_test, y_train, y_test = train_test_split(
        features_df, target, test_size=test_size, shuffle=False
    )
    
    model = xgb.XGBClassifier(
        n_estimators=100,
        max_depth=5,
        learning_rate=0.1,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=42
    )
    
    model.fit(X_train, y_train)
    y_pred = model.predict(X_test)
    y_prob = model.predict_proba(X_test)
    
    acc = accuracy_score(y_test, y_pred)
    print(f"准确率: {acc:.4f}")
    
    # 特征重要性
    importance = pd.Series(model.feature_importances_, 
                          index=features_df.columns).sort_values(ascending=False)
    
    return model, importance, y_prob
    
# 典型特征构造
def build_ml_features(df):
    """构建机器学习特征"""
    features = pd.DataFrame(index=df.index)
    
    # 价格特征
    features['return_1d'] = df['close'].pct_change(1)
    features['return_5d'] = df['close'].pct_change(5)
    features['return_20d'] = df['close'].pct_change(20)
    
    # 波动率
    features['volatility_5d'] = features['return_1d'].rolling(5).std()
    features['volatility_20d'] = features['return_1d'].rolling(20).std()
    
    # 成交量特征
    features['volume_ratio'] = df['volume'] / df['volume'].rolling(20).mean()
    features['turnover_change'] = df['volume'].pct_change(5)
    
    # 技术指标
    features['rsi'] = compute_rsi(df['close'], 14)
    features['ma_ratio_5_20'] = (df['close'].rolling(5).mean() / 
                                  df['close'].rolling(20).mean())
    
    # 标签：次日涨跌
    target = (df['close'].shift(-1) > df['close']).astype(int)
    
    return features.dropna(), target.dropna()
```

### 10.2 LSTM 时序预测

```python
def lstm_stock_predictor(data, lookback=60, epochs=50):
    """
    LSTM预测股价走势
    data: 价格序列
    lookback: 回溯窗口
    """
    from tensorflow.keras.models import Sequential
    from tensorflow.keras.layers import LSTM, Dense, Dropout
    from sklearn.preprocessing import MinMaxScaler
    
    scaler = MinMaxScaler()
    scaled_data = scaler.fit_transform(data.reshape(-1, 1))
    
    X, y = [], []
    for i in range(lookback, len(scaled_data)):
        X.append(scaled_data[i-lookback:i, 0])
        y.append(scaled_data[i, 0])
    
    X, y = np.array(X), np.array(y)
    X = X.reshape((X.shape[0], X.shape[1], 1))
    
    model = Sequential([
        LSTM(50, return_sequences=True, input_shape=(lookback, 1)),
        Dropout(0.2),
        LSTM(50, return_sequences=False),
        Dropout(0.2),
        Dense(25),
        Dense(1)
    ])
    
    model.compile(optimizer='adam', loss='mse')
    model.fit(X, y, batch_size=32, epochs=epochs, verbose=0)
    
    return model, scaler
```

---

## 十一、波动率策略类

### 11.1 VIX 恐慌指数择时

```python
def vix_timing_strategy(vix_values, threshold=25):
    """
    VIX恐慌指数择时：
    VIX > 25 → 市场恐慌 → 抄底买入
    VIX < 15 → 市场平静 → 减仓
    """
    signals = pd.Series(0, index=vix_values.index)
    signals[vix_values > threshold] = 1      # 恐慌 → 买
    signals[vix_values < 15] = -1            # 过热 → 卖
    return signals
```

### 11.2 布林带带宽波动率策略

```python
def volatility_breakout_strategy(df, lookback=20, threshold=1.5):
    """
    波动率突破：当前波动率超过历史均值的threshold倍 → 有行情
    """
    df = df.copy()
    df['returns'] = df['close'].pct_change()
    df['volatility'] = df['returns'].rolling(lookback).std()
    df['avg_volatility'] = df['volatility'].rolling(60).mean()
    
    df['signal'] = 0
    # 波动率放大 → 趋势启动信号，跟随方向
    df.loc[(df['volatility'] > threshold * df['avg_volatility']) & 
           (df['returns'] > 0), 'signal'] = 1
    df.loc[(df['volatility'] > threshold * df['avg_volatility']) & 
           (df['returns'] < 0), 'signal'] = -1
    
    return df['signal']
```

---

## 十二、风控与仓位管理

### 12.1 凯利公式 (Kelly Criterion)

```python
def kelly_position(win_rate, avg_win_pct, avg_loss_pct):
    """
    凯利公式计算最优仓位
    f* = (p * b - (1-p)) / b
    p: 胜率
    b: 盈亏比 = avg_win / avg_loss
    """
    if avg_loss_pct == 0:
        return 0
    b = avg_win_pct / avg_loss_pct
    kelly_f = (win_rate * b - (1 - win_rate)) / b
    return max(0, min(kelly_f * 0.5, 1.0))  # 半凯利更稳健
```

### 12.2 ATR 动态止损

```python
def atr_stop_loss(entry_price, atr, multiplier=2.0):
    """
    ATR动态止损：当前ATR的multiplier倍
    """
    stop_price = entry_price - multiplier * atr
    return stop_price
```

### 12.3 大盘风控过滤器

```python
def market_regime_filter(index_data, ma_period=60):
    """
    大盘风控：指数在MA60下方时空仓
    """
    index_data = index_data.copy()
    index_data['ma'] = index_data['close'].rolling(ma_period).mean()
    # 指数在均线下方 → 空仓
    allow_trading = (index_data['close'] >= index_data['ma']).astype(int)
    return allow_trading
```

---

## 十三、策略组合建议

### 13.1 按市场环境配置

| 市场状态 | 主策略 | 辅助策略 | 仓位 |
|----------|--------|----------|------|
| 单边上涨 | 趋势跟踪（海龟、MA） | 动量 | 80% |
| 单边下跌 | 空仓/做空 | 波动率对冲 | 20% |
| 区间震荡 | 均值回归（布林带、网格） | 配对交易 | 60% |
| 高波动 | 波动率突破 | ATR止损 | 40% |
| 低波动 | 多因子选股 | 事件驱动 | 50% |

### 13.2 个人学习路径

```
入门：双均线 → MACD → RSI/KDJ
进阶：布林带 → 海龟 → 多因子 → 配对交易
高级：机器学习选股 → LSTM时序 → Alpha因子 → 高频策略
```

### 13.3 回测建议指标

- **收益率**：总收益、年化收益、超额收益
- **风险**：最大回撤、年化波动率、VaR
- **风险调整**：夏普比率、卡玛比率、索提诺比率
- **交易**：胜率、盈亏比、交易次数、换手率
- **基准对比**：沪深300净值曲线对比

---

## 附录：推荐数据源与工具

| 工具 | 用途 | 费用 |
|------|------|------|
| Tushare Pro | A股全量数据 | 免费(积分制) |
| AkShare | 多源数据接口 | 免费 |
| Baostock | A股数据(含复权) | 免费 |
| VectorBT | 向量化回测 | 免费 |
| Backtrader | 事件驱动回测 | 免费 |
| Zipline-relatived | 回测框架 | 免费 |
| TA-Lib | 技术指标库 | 免费 |
| scikit-learn | 机器学习 | 免费 |
| XGBoost / LightGBM | 梯度提升树 | 免费 |

---

> **声明**：本报告仅供学习参考，所有策略代码为框架示例。实盘交易前必须经过充分的历史回测、样本外验证和模拟盘测试。市场有风险，投资需谨慎。

# stock_data

A股/港股/ETF 量化数据仓库，CSV 格式，可直接 git diff 逐行查看变更。

## 结构

```
data/
├── daily/                     # 日线行情 (CSV, 按年打包)
│   ├── a_shares/2026.csv      # 每年一个文件，增量追加
│   ├── hk/2026.csv
│   └── etf/2026.csv
├── fundamental/               # 财报 (CSV, 原地更新)
│   ├── income_stmt.csv
│   ├── balance_sheet.csv
│   ├── cashflow.csv
│   └── financial_indicator.csv
├── meta/                      # 元数据 (CSV)
│   ├── stock_basic.csv
│   ├── trade_cal.csv
│   ├── hk_basic.csv
│   └── etf_basic.csv
└── macro/                     # 宏观 (CSV)
    ├── shibor.csv
    ├── lpr.csv
    ├── cpi.csv
    ├── pmi.csv
    ├── money_supply.csv
    └── bond_yield_10y.csv
```

## 克隆即用

```bash
git clone git@github.com:en574894764/stock_data.git
cd stock_data
pip install duckdb pandas

# 查询数据
python -c "
import duckdb
con = duckdb.connect()
# 直接查 CSV
r = con.execute(\"SELECT * FROM 'data/daily/a_shares/2026.csv' LIMIT 5\").fetchall()
print(r)
"
```

## 从 quant_sys 同步

```bash
cd ~/workspace/quant_sys
.venv/bin/python /path/to/stock_data/scripts/export.py
```

## 行级 diff 示例

```bash
# 看今天更新的利润表
git diff HEAD~1 -- data/fundamental/income_stmt.csv

# 看某天新增的行情
git log -p -- data/daily/a_shares/2026.csv
```

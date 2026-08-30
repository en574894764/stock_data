#!/usr/bin/env python3
"""宏观指标拉取 → macro/*.csv 直写（M6 — 修复方案 2026-08-30）

决策点 D2：宏观数据量小（每个文件 ≤ 数千行），不值得建 PG 表，
akshare 直写 CSV，写入幂等（内容不变不写盘，减少 git 噪音）。

数据源（全部 akshare，接口已验证，见 scripts/verify_apis.py）：
  shibor.csv          macro_china_shibor_all     日频
  bond_yield_10y.csv  bond_zh_us_rate             日频
  lpr.csv             macro_china_lpr             月度（每月 20 日）
  cpi.csv             macro_china_cpi             月度（统计局 全国/城市/农村）
  pmi.csv             macro_china_pmi             月度
  money_supply.csv    macro_china_money_supply    月度

列结构：akshare 新版接口的中文列名 → 映射回旧文件的英文列结构
（shibor/lpr/cpi/money_supply 完全保持旧结构；pmi 旧文件本身列错位损坏，
改用干净的 month,zs_pmi,fzs_pmi 结构）。
排序习惯保持：shibor/lpr/cpi/pmi/money_supply 倒序（最新在前），
bond_yield_10y 正序 —— 与现有文件一致。

用法：
  python fetch_macro.py                  # 全部拉取
  python fetch_macro.py --only cpi,pmi   # 只拉指定文件
  python fetch_macro.py --dry-run        # 只看不写
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).parent
MACRO_DIR = REPO / "macro"

_DATE_PAT = re.compile(r"^(19|20)\d{2}([-/]\d{1,2}[-/]\d{1,2}|[-/]?\d{2}[-/]?\d{2}|[-/]?\d{1,2})$")


# ── 列映射（akshare 新中文列 → 旧英文列结构）─────────────────────────────────

def _cn_month_to_yyyymm(s):
    """'2026年07月份' → '202607'"""
    m = re.match(r"(\d{4})年(\d{1,2})月", str(s))
    return f"{m.group(1)}{int(m.group(2)):02d}" if m else str(s)


def _t_shibor(df: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame()
    out["date"] = df["日期"].astype(str).str.replace("-", "", regex=False).str[:8]
    out["on"] = df["O/N-定价"]
    for k in ("1W", "2W", "1M", "3M", "6M", "9M", "1Y"):
        out[k.lower()] = df[f"{k}-定价"]
    return out


def _t_lpr(df: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame()
    out["date"] = df["TRADE_DATE"].astype(str).str.replace("-", "", regex=False).str[:8]
    out["1y"] = df["LPR1Y"]
    out["5y"] = df["LPR5Y"]
    # 2019-08 LPR 改革前无 LPR 报价，过滤空值行（保持旧文件只含有效 LPR 数据的习惯）
    return out.dropna(subset=["1y", "5y"], how="all")


def _t_cpi(df: pd.DataFrame) -> pd.DataFrame:
    mapping = [
        ("全国-当月", "nt_val"), ("全国-同比增长", "nt_yoy"), ("全国-环比增长", "nt_mom"), ("全国-累计", "nt_accu"),
        ("城市-当月", "town_val"), ("城市-同比增长", "town_yoy"), ("城市-环比增长", "town_mom"), ("城市-累计", "town_accu"),
        ("农村-当月", "cnt_val"), ("农村-同比增长", "cnt_yoy"), ("农村-环比增长", "cnt_mom"), ("农村-累计", "cnt_accu"),
    ]
    out = pd.DataFrame()
    out["month"] = df["月份"].map(_cn_month_to_yyyymm)
    for src, dst in mapping:
        out[dst] = df[src]
    return out


def _t_pmi(df: pd.DataFrame) -> pd.DataFrame:
    """旧 pmi.csv 列错位损坏，改用干净结构：month,zs_pmi(制造业),fzs_pmi(非制造业)。"""
    out = pd.DataFrame()
    out["month"] = df["月份"].map(_cn_month_to_yyyymm)
    out["zs_pmi"] = df["制造业-指数"]
    out["zs_pmi_yoy"] = df["制造业-同比增长"]
    out["fzs_pmi"] = df["非制造业-指数"]
    out["fzs_pmi_yoy"] = df["非制造业-同比增长"]
    return out


def _t_money(df: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame()
    out["month"] = df["月份"].map(_cn_month_to_yyyymm)
    for prefix, src in (("m0", "流通中的现金(M0)"), ("m1", "货币(M1)"), ("m2", "货币和准货币(M2)")):
        out[prefix] = df[f"{src}-数量(亿元)"]
        out[f"{prefix}_yoy"] = df[f"{src}-同比增长"]
        out[f"{prefix}_mom"] = df[f"{src}-环比增长"]
    return out


def _t_identity(df: pd.DataFrame) -> pd.DataFrame:
    return df


SOURCES = [
    # (文件名, akshare 接口, 列映射, 展示名, 排序方向: desc=最新在前 / asc=最老在前)
    ("shibor.csv", "macro_china_shibor_all", _t_shibor, "Shibor", "desc"),
    ("bond_yield_10y.csv", "bond_zh_us_rate", _t_identity, "中美国债收益率", "asc"),
    ("lpr.csv", "macro_china_lpr", _t_lpr, "LPR", "desc"),
    ("cpi.csv", "macro_china_cpi", _t_cpi, "CPI", "desc"),
    ("pmi.csv", "macro_china_pmi", _t_pmi, "PMI", "desc"),
    ("money_supply.csv", "macro_china_money_supply", _t_money, "货币供应", "desc"),
]


# ── 顺序/幂等 ────────────────────────────────────────────────────────────────

def _scan_order(df: pd.DataFrame) -> str | None:
    """在 DataFrame 中找日期样式列，判断首尾顺序 → 'asc' | 'desc' | None。"""
    for c in df.columns:
        vals = df[c].astype(str).str.strip()
        date_like = vals[vals.str.match(_DATE_PAT)]
        if len(date_like) >= 2:
            first, last = date_like.iloc[0], date_like.iloc[-1]
            if first != last:
                return "desc" if first > last else "asc"
    return None


def _file_order(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        return _scan_order(pd.read_csv(path, dtype=str))
    except Exception:
        return None


def _same_content(path: Path, df: pd.DataFrame) -> bool:
    if not path.exists():
        return False
    try:
        old = pd.read_csv(path, dtype=str).fillna("")
        new = df.astype(str).fillna("")
        return old.shape == new.shape and list(old.columns) == list(new.columns) and old.equals(new)
    except Exception:
        return False


def fetch_one(ak, fname: str, api_name: str, transform, label: str,
              order: str | None = None, dry_run: bool = False) -> bool:
    path = MACRO_DIR / fname
    fn = getattr(ak, api_name, None)
    if fn is None:
        print(f"  ✗ {label}: akshare 无 {api_name} 接口")
        return False
    try:
        raw = fn()
    except Exception as e:
        print(f"  ✗ {label}: {str(e)[:120]}")
        return False
    if raw is None or raw.empty:
        print(f"  ✗ {label}: 返回空")
        return False

    try:
        df = transform(raw)
    except Exception as e:
        print(f"  ✗ {label}: 列映射失败（接口结构变更?）: {str(e)[:120]}")
        return False

    # 排序方向：显式配置优先，其次跟随现有文件习惯
    want_order = order or _file_order(path)
    if want_order:
        new_order = _scan_order(df)
        if new_order and new_order != want_order:
            df = df.iloc[::-1]

    # 最新数据日期（展示用）
    latest = None
    for c in df.columns:
        vals = df[c].astype(str).str.strip()
        date_like = vals[vals.str.match(_DATE_PAT)]
        if len(date_like) >= max(1, len(df) // 2):
            latest = date_like.iloc[0] if want_order != "asc" else date_like.iloc[-1]
            break

    if _same_content(path, df):
        print(f"  ✅ {label}: 无变化 ({len(df)} 行, 最新 {latest})")
        return True
    if dry_run:
        print(f"  [dry-run] {label}: 将写入 {len(df)} 行 (最新 {latest})")
        return True
    MACRO_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    print(f"  ✅ {label}: 写入 {len(df)} 行 → {fname} (最新 {latest})")
    return True


def main():
    parser = argparse.ArgumentParser(description="宏观指标拉取 → macro/*.csv")
    parser.add_argument("--only", help="只拉指定文件（逗号分隔，如 cpi.csv,pmi.csv）")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        import akshare as ak
    except ImportError:
        print("❌ akshare 未安装")
        sys.exit(1)

    sources = SOURCES
    if args.only:
        wanted = {s.strip() for s in args.only.split(",")}
        sources = [s for s in SOURCES if s[0] in wanted or s[0].replace(".csv", "") in {w.replace(".csv", "") for w in wanted}]
        if not sources:
            print(f"❌ 无匹配的宏观数据源: {args.only}")
            sys.exit(2)

    print("=== 宏观数据拉取 (akshare → macro/*.csv) ===")
    ok = 0
    for fname, api_name, transform, label, order in sources:
        if fetch_one(ak, fname, api_name, transform, label, order, args.dry_run):
            ok += 1
    print(f"\n完成: {ok}/{len(sources)} 成功")
    sys.exit(0 if ok == len(sources) else 1)


if __name__ == "__main__":
    main()

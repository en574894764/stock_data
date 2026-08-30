#!/usr/bin/env python3
"""stock_data 数据拉取 & GitHub 备份（v2 — 修复方案 2026-08-30 M3）

架构（数据流单向化，决策点 D1）：
  fetch 只写 PostgreSQL（唯一权威源） → scripts/export.py 导出 CSV → git 备份
  validate.py 校验 PG —— 与本脚本写入目标一致，缺口可收敛

数据域与数据源（区间自愈：从 PG MAX(trade_date)+1 拉到最近交易日）：
  A股日线   tushare pro.daily(trade_date)      → daily_quote（非 .HK）
  港股日线   akshare stock_hk_daily(symbol)     → daily_quote（.HK，按标的拉区间）
            pro.hk_daily 限频 1 次/小时，逐日补档不现实，故弃用
            （akshare 口径已验证与存量 0 偏差，见 scripts/verify_apis.py）
  ETF 日线  tushare pro.fund_daily(trade_date)  → etf_quote
  A股指数   tushare pro.index_daily(trade_date) → index_daily（只保留关注的 9 个）
  港股指数   akshare stock_hk_index_daily_sina   → index_daily（HSI/HSTECH/HSCEI；
            HKTECH/HSHKCI sina 不支持，跳过并告警）

写入幂等：全部 ON CONFLICT DO NOTHING，重复跑安全。
财报数据域不在此脚本（见 backfill_financial.py）；宏观不在此脚本（见 fetch_macro.py）。

用法：
  python fetch_and_backup.py                     # 增量拉取（区间自愈）+ 导出 + git 备份
  python fetch_and_backup.py --only a,etf        # 只拉指定数据域 (a,hk,etf,index)
  python fetch_and_backup.py --dry-run           # 只算区间，不调 API 不写库
  python fetch_and_backup.py --skip-export       # 只写 PG，不导出 CSV（pipeline 分步时用）
  python fetch_and_backup.py --cron              # 定时任务模式（静默输出）
  python fetch_and_backup.py --no-push           # 不推送 git
  python fetch_and_backup.py --tencent           # 应急通道：腾讯源直写 daily/*.csv
                                                 # （仅 PG 不可用时手动使用，不进日常 pipeline）

环境变量（.env 已 gitignore，或 shell 注入）：
  TUSHARE_TOKEN    Tushare Pro token（必需）
  PGHOST/PGDATABASE/PGUSER   PostgreSQL 连接（默认 /tmp socket + investassist）
"""

from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).parent
DAILY_DIR = REPO / "daily"
LOG_FILE = REPO / "logs" / "fetch.log"
ENV_FILE = REPO / ".env"

GITHUB_REMOTE = os.environ.get("GITHUB_REMOTE", "origin")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
MAX_RETRIES = 3

# 关注的 A 股指数（tushare index_daily 单日返回全市场，需过滤）
A_INDEX_SYMBOLS = ("000001.SH", "000016.SH", "000300.SH", "000688.SH", "000852.SH",
                   "000905.SH", "399001.SZ", "399005.SZ", "399006.SZ")
A_INDEX_NAMES = {"000001.SH": "上证指数", "000016.SH": "上证50", "000300.SH": "沪深300",
                 "000688.SH": "科创50", "000852.SH": "中证1000", "000905.SH": "中证500",
                 "399001.SZ": "深证成指", "399005.SZ": "中小100", "399006.SZ": "创业板指"}

# 港股指数（akshare sina 源；HKTECH/HSHKCI 暂无稳定免费源，跳过）
HK_INDEX_SYMBOLS = ("HSI", "HSTECH", "HSCEI")
HK_INDEX_NAMES = {"HSI": "恒生指数", "HSTECH": "恒生科技指数", "HSCEI": "恒生中国企业指数"}

# 港股判定"落后"的容忍天数（港股交易日历与 A 股不同 + 数据源延迟）
HK_LAG_TOLERANCE_DAYS = 7
# 港股按标的回看窗口：拉 last_date 之前 90 天内的行也一并补（近期断档自愈）
HK_BACKFILL_WINDOW_DAYS = 90

ALL_DOMAINS = ("a", "hk", "etf", "index")

CRON = {"on": False}  # 是否静默模式


# ── 基础设施 ────────────────────────────────────────────────────────────────

def load_env():
    """加载 REPO/.env（KEY=VALUE，# 注释），不覆盖已有环境变量。"""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def log(msg: str, level: str = "INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")
    if not CRON["on"] or level in ("ERROR", "WARN"):
        print(line)


def get_pg_conn():
    import psycopg2
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "/tmp"),
        dbname=os.environ.get("PGDATABASE", "investassist"),
        user=os.environ.get("PGUSER", "james"),
        password=os.environ.get("PGPASSWORD", ""),
        connect_timeout=5,
    )


def get_tushare_token() -> str:
    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    if not token:
        log("未设置 TUSHARE_TOKEN（.env 或环境变量），Tushare 数据域将跳过", "WARN")
    return token


def get_tushare_pro():
    import tushare as ts
    token = get_tushare_token()
    if not token:
        return None
    return ts.pro_api(token)


# ── 交易日历 ─────────────────────────────────────────────────────────────────

def get_latest_trade_date(conn) -> date | None:
    """最近一个 A 股交易日（不含未来）。"""
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(cal_date) FROM trade_cal WHERE is_open::int = 1 AND cal_date <= CURRENT_DATE")
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def get_trade_dates_between(conn, start: str, end: str) -> list[str]:
    """[start, end] 内的 A 股交易日（YYYYMMDD）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT cal_date FROM trade_cal WHERE is_open::int = 1 AND cal_date >= %s AND cal_date <= %s ORDER BY cal_date",
            (start, end))
        rows = cur.fetchall()
    return [r[0].strftime("%Y%m%d") for r in rows]


def get_max_date(conn, sql: str, params: tuple = None):
    with conn.cursor() as cur:
        if params:
            cur.execute(sql, params)
        else:
            cur.execute(sql)  # SQL 含字面 % 时不能传空 params
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def missing_dates_for(conn, max_date_sql: str, target: date, params: tuple = None) -> list[str]:
    """某数据域的待补交易日列表：MAX(trade_date)+1 ~ target。"""
    max_d = get_max_date(conn, max_date_sql, params)
    if max_d is None:
        log("该数据域 PG 无数据，无法确定增量起点（请先全量回补）", "WARN")
        return []
    start = (max_d + timedelta(days=1)).strftime("%Y%m%d")
    end = target.strftime("%Y%m%d")
    return get_trade_dates_between(conn, start, end)


# ── 写入 helper ──────────────────────────────────────────────────────────────

def _clean_float(v):
    """NaN/None → None；其余转 float。"""
    try:
        if v is None:
            return None
        f = float(v)
        return None if f != f else f  # NaN check
    except (TypeError, ValueError):
        return None


def upsert_rows(conn, sql: str, rows: list[tuple]) -> int:
    """execute_values 批量写入（幂等 ON CONFLICT DO NOTHING），返回提交行数。"""
    if not rows:
        return 0
    from psycopg2.extras import execute_values
    with conn.cursor() as cur:
        execute_values(cur, sql, rows, page_size=1000)
    conn.commit()
    return len(rows)


# ═════════════════════════════════════════════════════════════════════════════
# 数据域: A股日线 → daily_quote
# ═════════════════════════════════════════════════════════════════════════════

DAILY_QUOTE_INSERT_SQL = """
    INSERT INTO daily_quote (ts_code, trade_year, trade_date, pre_close, open, high, low, close, change, pct_chg, vol, amount)
    VALUES %s
    ON CONFLICT (ts_code, trade_year, trade_date) DO NOTHING
"""


def fetch_a_share(conn, pro, target: date, dry_run: bool = False) -> int:
    print("\n── A股日线 (tushare pro.daily → daily_quote) ──")
    sql = "SELECT MAX(trade_date) FROM daily_quote WHERE ts_code NOT LIKE '%.HK'"
    max_d = get_max_date(conn, sql)
    dates = missing_dates_for(conn, sql, target)
    if not dates:
        print(f"  ✅ 已是最新 (MAX={max_d}, 目标={target})")
        return 0
    print(f"  PG MAX={max_d} → 待补 {len(dates)} 个交易日: {dates[0]} ~ {dates[-1]}")
    if dry_run:
        return 0

    total = 0
    for i, d in enumerate(dates):
        try:
            df = pro.daily(trade_date=d)
        except Exception as e:
            log(f"A股 {d} 拉取失败: {e}", "WARN")
            continue
        if df is None or df.empty:
            log(f"A股 {d} 无行情（可能非交易日或未出数）", "WARN")
            continue

        rows = []
        for r in df.to_dict("records"):
            o, h, l, c = (_clean_float(r.get(x)) for x in ("open", "high", "low", "close"))
            # 零值过滤：任一价非正即脏（停牌/无效）
            if not (o and o > 0 and h and h > 0 and l and l > 0 and c and c > 0):
                continue
            rows.append((
                r["ts_code"], int(d[:4]), f"{d[:4]}-{d[4:6]}-{d[6:8]}",
                _clean_float(r.get("pre_close")), o, h, l, c,
                _clean_float(r.get("change")), _clean_float(r.get("pct_chg")),
                _clean_float(r.get("vol")), _clean_float(r.get("amount")),
            ))
        n = upsert_rows(conn, DAILY_QUOTE_INSERT_SQL, rows)
        total += n
        print(f"  [{i+1}/{len(dates)}] {d}: +{n} 行")
        time.sleep(0.4)  # tushare 频控

    log(f"A股日线完成: {total} 行")
    return total


# ═════════════════════════════════════════════════════════════════════════════
# 数据域: 港股日线 → daily_quote（akshare 按标的）
# ═════════════════════════════════════════════════════════════════════════════

def fetch_hk(conn, target: date, dry_run: bool = False) -> int:
    print("\n── 港股日线 (akshare stock_hk_daily → daily_quote) ──")
    import pandas as pd

    # 落后标的：last_date < target - 容忍天数
    cutoff = target - timedelta(days=HK_LAG_TOLERANCE_DAYS)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT ts_code, MAX(trade_date) AS last_date
            FROM daily_quote
            WHERE ts_code LIKE %s
            GROUP BY ts_code
        """, ('%.HK',))
        all_hk = cur.fetchall()
    lagging = [(ts, ld) for ts, ld in all_hk if ld and ld < cutoff]
    fresh = len(all_hk) - len(lagging)
    print(f"  港股共 {len(all_hk)} 只 | 新鲜 {fresh} | 落后(< {cutoff}) {len(lagging)}")
    if not lagging:
        return 0
    if dry_run:
        for ts, ld in lagging[:10]:
            print(f"  [dry-run] {ts}: last={ld}")
        if len(lagging) > 10:
            print(f"  ... 等共 {len(lagging)} 只")
        return 0

    import akshare as ak
    total = 0
    failed = 0
    for i, (ts_code, last_date) in enumerate(lagging):
        symbol = ts_code.split(".")[0]
        try:
            df = ak.stock_hk_daily(symbol=symbol, adjust="")
            if df is None or df.empty:
                failed += 1
                continue
            # 防御性数值清洗：akshare 偶发返回脏值（如 '3.90' 混入 float64 列
            # 抛 Invalid value 异常导致整只标的失败），coerce 后仅丢脏行
            for col in ("open", "high", "low", "close", "volume", "amount"):
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            # date 列可能是 datetime.date（object dtype），统一转 Timestamp
            df["date"] = pd.to_datetime(df["date"])
            # 兼容列名（volume/vol；amount 可能缺失）
            vol_col = "volume" if "volume" in df.columns else ("vol" if "vol" in df.columns else None)
            if vol_col and vol_col != "vol":
                df = df.rename(columns={vol_col: "vol"})
            if "amount" not in df.columns:
                df["amount"] = None
            df["pre_close"] = df["close"].shift(1)

            # 写 last_date - 90 天窗口之后的行（近期断档自愈 + 增量）
            window_start = pd.Timestamp(last_date) - pd.Timedelta(days=HK_BACKFILL_WINDOW_DAYS)
            new_df = df[df["date"] > window_start]
            if new_df.empty:
                continue

            # 窗口首行的 pre_close 若为 NaN，用 PG 已有最后收盘价补
            # （Decimal 必须转 float——新版 pandas 禁止 Decimal 赋值 float64 列，
            #   报 Invalid value for dtype，此前 23 只港股失败的根因）
            pg_last_close = None
            with conn.cursor() as cur:
                cur.execute("SELECT close FROM daily_quote WHERE ts_code = %s ORDER BY trade_date DESC LIMIT 1",
                            (ts_code,))
                row = cur.fetchone()
                pg_last_close = float(row[0]) if row else None
            if pg_last_close is not None and pd.isna(new_df.iloc[0]["pre_close"]):
                new_df = new_df.copy()
                new_df.iloc[0, new_df.columns.get_loc("pre_close")] = pg_last_close

            rows = []
            for r in new_df.to_dict("records"):
                o, h, l, c = (_clean_float(r.get(x)) for x in ("open", "high", "low", "close"))
                if not (o and o > 0 and c and c > 0 and h and h > 0 and l and l > 0):
                    continue
                d = r["date"]
                d_str = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10]
                pre = _clean_float(r.get("pre_close"))
                chg = (c - pre) if (pre and pre > 0) else None
                pct = ((c / pre - 1) * 100) if (pre and pre > 0) else None
                rows.append((
                    ts_code, int(d_str[:4]), d_str, pre, o, h, l, c, chg, pct,
                    _clean_float(r.get("vol")), _clean_float(r.get("amount")),
                ))
            n = upsert_rows(conn, DAILY_QUOTE_INSERT_SQL, rows)
            total += n
            if (i + 1) % 100 == 0 or i == len(lagging) - 1:
                print(f"  [{i+1}/{len(lagging)}] 累计 +{total} 行 (失败 {failed})")
            time.sleep(0.25)  # akshare 礼貌限速
        except Exception as e:
            failed += 1
            if failed <= 5 or (i + 1) % 100 == 0:
                log(f"港股 {ts_code} 失败: {str(e)[:100]}", "WARN")

    log(f"港股日线完成: {total} 行, 失败 {failed} 只")
    return total


# ═════════════════════════════════════════════════════════════════════════════
# 数据域: ETF 日线 → etf_quote
# ═════════════════════════════════════════════════════════════════════════════

def fetch_etf(conn, pro, target: date, dry_run: bool = False) -> int:
    print("\n── ETF日线 (tushare pro.fund_daily → etf_quote) ──")
    sql = "SELECT MAX(trade_date) FROM etf_quote"
    max_d = get_max_date(conn, sql)
    dates = missing_dates_for(conn, sql, target)
    if not dates:
        print(f"  ✅ 已是最新 (MAX={max_d}, 目标={target})")
        return 0
    print(f"  PG MAX={max_d} → 待补 {len(dates)} 个交易日: {dates[0]} ~ {dates[-1]}")
    if dry_run:
        return 0

    insert_sql = """
        INSERT INTO etf_quote (code, trade_date, trade_year, pre_close, open, high, low, close, change, pct_chg, vol, amount)
        VALUES %s
        ON CONFLICT (code, trade_date) DO NOTHING
    """
    total = 0
    for i, d in enumerate(dates):
        try:
            df = pro.fund_daily(trade_date=d)
        except Exception as e:
            log(f"ETF {d} 拉取失败: {e}", "WARN")
            continue
        if df is None or df.empty:
            log(f"ETF {d} 无行情", "WARN")
            continue

        rows = []
        for r in df.to_dict("records"):
            o, h, l, c = (_clean_float(r.get(x)) for x in ("open", "high", "low", "close"))
            if not (o and o > 0 and h and h > 0 and l and l > 0 and c and c > 0):
                continue
            rows.append((
                r["ts_code"], f"{d[:4]}-{d[4:6]}-{d[6:8]}", int(d[:4]),
                _clean_float(r.get("pre_close")), o, h, l, c,
                _clean_float(r.get("change")), _clean_float(r.get("pct_chg")),
                _clean_float(r.get("vol")), _clean_float(r.get("amount")),
            ))
        n = upsert_rows(conn, insert_sql, rows)
        total += n
        print(f"  [{i+1}/{len(dates)}] {d}: +{n} 行")
        time.sleep(0.4)

    log(f"ETF日线完成: {total} 行")
    return total


# ═════════════════════════════════════════════════════════════════════════════
# 数据域: 指数 → index_daily（A股 tushare + 港股指数 akshare sina）
# ═════════════════════════════════════════════════════════════════════════════

INDEX_INSERT_SQL = """
    INSERT INTO index_daily (symbol, name, trade_date, open, high, low, close, pre_close, change, pct_chg, volume, amount)
    VALUES %s
    ON CONFLICT (symbol, trade_date) DO NOTHING
"""


def fetch_index(conn, pro, target: date, dry_run: bool = False) -> int:
    print("\n── 指数日线 (A股 tushare + 港股指数 akshare → index_daily) ──")
    total = 0

    # ── A股指数：逐交易日 ──
    a_in = ",".join(f"'{s}'" for s in A_INDEX_SYMBOLS)
    sql = f"SELECT MAX(trade_date) FROM index_daily WHERE symbol IN ({a_in})"
    max_d = get_max_date(conn, sql)
    dates = missing_dates_for(conn, sql, target)
    if not dates:
        print(f"  A股指数 ✅ 已是最新 (MAX={max_d})")
    else:
        print(f"  A股指数 PG MAX={max_d} → 待补 {len(dates)} 个交易日")
        if not dry_run:
            for i, d in enumerate(dates):
                try:
                    df = pro.index_daily(trade_date=d)
                except Exception as e:
                    log(f"A股指数 {d} 拉取失败: {e}", "WARN")
                    continue
                if df is None or df.empty:
                    continue
                df = df[df["ts_code"].isin(A_INDEX_SYMBOLS)]
                rows = []
                for r in df.to_dict("records"):
                    o, h, l, c = (_clean_float(r.get(x)) for x in ("open", "high", "low", "close"))
                    if not (o and o > 0 and c and c > 0):
                        continue
                    rows.append((
                        r["ts_code"], A_INDEX_NAMES.get(r["ts_code"], r["ts_code"]),
                        f"{d[:4]}-{d[4:6]}-{d[6:8]}", o, h, l, c,
                        _clean_float(r.get("pre_close")),
                        _clean_float(r.get("change")), _clean_float(r.get("pct_chg")),
                        _clean_float(r.get("vol")), _clean_float(r.get("amount")),
                    ))
                n = upsert_rows(conn, INDEX_INSERT_SQL, rows)
                total += n
                print(f"  [{i+1}/{len(dates)}] A股指数 {d}: +{n} 行")
                time.sleep(0.4)

    # ── 港股指数：akshare sina 按标的 ──
    if dry_run:
        for sym in HK_INDEX_SYMBOLS:
            with conn.cursor() as cur:
                cur.execute("SELECT MAX(trade_date) FROM index_daily WHERE symbol = %s", (sym,))
                row = cur.fetchone()
            print(f"  [dry-run] {sym}: last={row[0] if row else None}")
        return total

    import akshare as ak
    import pandas as pd
    for sym in HK_INDEX_SYMBOLS:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT MAX(trade_date) FROM index_daily WHERE symbol = %s", (sym,))
                row = cur.fetchone()
            last_d = row[0] if row else None
            if last_d and last_d >= target - timedelta(days=HK_LAG_TOLERANCE_DAYS):
                print(f"  港股指数 {sym} ✅ 新鲜 (last={last_d})")
                continue

            df = ak.stock_hk_index_daily_sina(symbol=sym)
            if df is None or df.empty:
                log(f"港股指数 {sym} 无数据", "WARN")
                continue
            df["date"] = pd.to_datetime(df["date"])
            df["pre_close"] = df["close"].shift(1)
            window_start = pd.Timestamp(last_d) - pd.Timedelta(days=HK_BACKFILL_WINDOW_DAYS) if last_d else df["date"].min()
            new_df = df[df["date"] > window_start]
            if new_df.empty:
                continue

            pg_last_close = None
            if last_d:
                with conn.cursor() as cur:
                    cur.execute("SELECT close FROM index_daily WHERE symbol = %s ORDER BY trade_date DESC LIMIT 1", (sym,))
                    row = cur.fetchone()
                    pg_last_close = row[0] if row else None
            if pg_last_close is not None and pd.isna(new_df.iloc[0]["pre_close"]):
                new_df = new_df.copy()
                new_df.iloc[0, new_df.columns.get_loc("pre_close")] = pg_last_close

            rows = []
            for r in new_df.to_dict("records"):
                d = r["date"]
                d_str = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10]
                o, h, l, c = (_clean_float(r.get(x)) for x in ("open", "high", "low", "close"))
                if not (o and o > 0 and c and c > 0):
                    continue
                pre = _clean_float(r.get("pre_close"))
                chg = (c - pre) if (pre and pre > 0) else None
                pct = ((c / pre - 1) * 100) if (pre and pre > 0) else None
                rows.append((
                    sym, HK_INDEX_NAMES.get(sym, sym), d_str, o, h, l, c, pre, chg, pct,
                    _clean_float(r.get("volume")), _clean_float(r.get("amount")),
                ))
            n = upsert_rows(conn, INDEX_INSERT_SQL, rows)
            total += n
            print(f"  港股指数 {sym}: +{n} 行 (last={last_d})")
            time.sleep(0.5)
        except Exception as e:
            log(f"港股指数 {sym} 失败: {str(e)[:120]}", "WARN")

    log(f"指数日线完成: {total} 行")
    return total


# ═════════════════════════════════════════════════════════════════════════════
# 导出 CSV（复用 scripts/export.py，M4 增强按标的导出）
# ═════════════════════════════════════════════════════════════════════════════

def run_export() -> bool:
    export_script = REPO / "scripts" / "export.py"
    if not export_script.exists():
        log("scripts/export.py 不存在，跳过导出", "ERROR")
        return False
    try:
        r = subprocess.run([sys.executable, str(export_script)],
                           capture_output=True, text=True, cwd=REPO, timeout=1800)
        if r.returncode != 0:
            log(f"export.py 失败 (exit={r.returncode}): {r.stderr[-500:]}", "ERROR")
            return False
        log("export.py 导出完成")
        return True
    except subprocess.TimeoutExpired:
        log("export.py 超时 (30分钟)", "ERROR")
        return False


# ═════════════════════════════════════════════════════════════════════════════
# 应急通道: 腾讯接口直写 daily/*.csv（PG 不可用时手动使用）
# ═════════════════════════════════════════════════════════════════════════════

def fetch_from_tencent(dry_run: bool = False) -> int:
    """腾讯接口 → daily/*.csv。仅作应急，不进日常 pipeline。

    去重修复（P0-2）：CSV 第二列是 datetime，判断 line.split(",")[1] == today。
    """
    import requests

    log("【应急通道】腾讯接口直写 daily/ CSV...")
    if dry_run:
        log("[dry-run] 跳过", "WARN")
        return 0

    if not DAILY_DIR.exists():
        log("daily/ 目录不存在", "ERROR")
        return 0

    ts_codes = [f.stem for f in sorted(DAILY_DIR.glob("*.csv"))
                if f.stem.endswith((".SH", ".SZ"))]

    code_map = {}
    for ts_code in ts_codes:
        symbol = ts_code.split(".")[0]
        tx_code = f"sh{symbol}" if ts_code.endswith(".SH") else f"sz{symbol}"
        code_map[tx_code] = ts_code

    today = date.today().strftime("%Y-%m-%d")
    url = "https://web.sqt.gtimg.cn/q="
    headers = {"User-Agent": "Mozilla/5.0"}
    batch_size = 500
    tx_codes = list(code_map.keys())
    total = len(tx_codes)
    new_rows = 0

    for i in range(0, total, batch_size):
        batch = tx_codes[i:i + batch_size]
        batch_url = url + ",".join(batch)
        try:
            resp = requests.get(batch_url, headers=headers, timeout=60,
                                proxies={"http": None, "https": None})
            resp.encoding = "gbk"
            lines = resp.text.strip().split(";")

            for line in lines:
                if '="' not in line:
                    continue
                parts = line.split('="')
                tx_code = parts[0].replace("v_", "").strip()
                data = parts[1].rstrip('"').split("~")
                if len(data) < 40:
                    continue
                ts_code = code_map.get(tx_code)
                if not ts_code:
                    continue
                try:
                    price = float(data[3]) if data[3] else 0
                    pre_close = float(data[4]) if data[4] else 0
                    open_price = float(data[5]) if data[5] else pre_close
                    high = float(data[33]) if data[33] else price
                    low = float(data[34]) if data[34] else price
                    vol = float(data[6]) * 100 if data[6] else 0
                    amount = float(data[7]) * 10000 if data[7] else 0
                    if high == 0:
                        high = price
                    if low == 0:
                        low = price
                except (ValueError, IndexError):
                    continue

                # 零值过滤：任一价非正即脏
                if price <= 0 or open_price <= 0 or high <= 0 or low <= 0:
                    continue

                csv_path = DAILY_DIR / f"{ts_code}.csv"
                # 去重：CSV 第二列是 datetime（修复原 startswith(today) 永假 bug）
                dup = False
                if csv_path.exists():
                    try:
                        with open(csv_path) as _f:
                            for _line in _f:
                                if _line.split(",")[1:2] == [today]:
                                    dup = True
                                    break
                    except Exception:
                        pass
                if dup:
                    continue

                new_line = {
                    "symbol": ts_code, "datetime": today,
                    "open": open_price, "high": high, "low": low,
                    "close": price, "volume": vol, "amount": amount,
                }
                existed = csv_path.exists()
                with open(csv_path, "a", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=list(new_line.keys()))
                    if not existed:
                        writer.writeheader()
                    writer.writerow(new_line)
                new_rows += 1
        except Exception as e:
            log(f"批次 [{i+1}-{min(i+batch_size, total)}] 失败: {e}", "WARN")
            continue
        time.sleep(0.05)

    log(f"腾讯应急通道: 写入 {new_rows} 行 ({len(ts_codes)} 只标的)")
    return new_rows


# ═════════════════════════════════════════════════════════════════════════════
# Git 操作
# ═════════════════════════════════════════════════════════════════════════════

def git_status() -> tuple[bool, list[str]]:
    try:
        r = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, cwd=REPO, timeout=10)
        lines = [l[3:] for l in r.stdout.strip().split("\n") if l]
        return len(lines) > 0, lines
    except Exception as e:
        log(f"git status 失败: {e}", "ERROR")
        return False, []


def git_pull() -> bool:
    for i in range(MAX_RETRIES):
        try:
            r = subprocess.run(["git", "pull", "--rebase", GITHUB_REMOTE, GITHUB_BRANCH],
                               capture_output=True, text=True, cwd=REPO, timeout=30)
            if r.returncode == 0:
                return True
            log(f"git pull 失败 (尝试 {i+1}/{MAX_RETRIES}): {r.stderr.strip()}", "WARN")
            time.sleep(2)
        except subprocess.TimeoutExpired:
            log(f"git pull 超时 (尝试 {i+1}/{MAX_RETRIES})", "WARN")
            time.sleep(2)
    return False


def git_commit_and_push(dry_run: bool = False, no_push: bool = False) -> bool:
    has_changes, files = git_status()
    if not has_changes:
        log("无变更，跳过提交")
        return True

    stats = {}
    for f in files:
        ext = Path(f).suffix
        stats[ext] = stats.get(ext, 0) + 1
    stats_str = ", ".join(f"{v} {k}" for k, v in sorted(stats.items()))
    log(f"变更: {len(files)} 个文件 ({stats_str})")

    if dry_run:
        log("[dry-run] 跳过 git 操作")
        return True

    today_str = date.today().strftime("%Y-%m-%d")
    try:
        subprocess.run(["git", "add", "-A", "data/", "daily/", "fundamental/", "meta/", "macro/", "index/"],
                       capture_output=True, text=True, cwd=REPO, timeout=60)
        subprocess.run(["git", "add", "scripts/", "validate.py", "mcp_server.py",
                        "fetch_and_backup.py", "fetch_macro.py", "backfill_financial.py", "pipeline.py"],
                       capture_output=True, text=True, cwd=REPO, timeout=10)
    except Exception as e:
        log(f"git add 失败: {e}", "ERROR")
        return False

    commit_msg = f"data: {today_str} auto-update\n\n{len(files)} files changed"
    try:
        r = subprocess.run(["git", "commit", "-m", commit_msg],
                           capture_output=True, text=True, cwd=REPO, timeout=60)
        if r.returncode != 0:
            log(f"git commit 失败: {r.stderr}", "ERROR")
            return False
    except Exception as e:
        log(f"git commit 失败: {e}", "ERROR")
        return False

    if no_push:
        log("--no-push 模式，跳过推送")
        return True

    for i in range(MAX_RETRIES):
        try:
            r = subprocess.run(["git", "push", GITHUB_REMOTE, GITHUB_BRANCH],
                               capture_output=True, text=True, cwd=REPO, timeout=120)
            if r.returncode == 0:
                log(f"git push 成功")
                return True
            log(f"git push 失败 (尝试 {i+1}/{MAX_RETRIES}): {r.stderr.strip()}", "WARN")
            time.sleep(3)
        except subprocess.TimeoutExpired:
            log(f"git push 超时 (尝试 {i+1}/{MAX_RETRIES})", "WARN")
            time.sleep(3)
    return False


# ═════════════════════════════════════════════════════════════════════════════
# 主流程
# ═════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="stock_data 数据拉取 & GitHub 备份 (PG 单向流)")
    parser.add_argument("--only", default="all", help=f"只拉指定数据域，逗号分隔 ({','.join(ALL_DOMAINS)})")
    parser.add_argument("--dry-run", action="store_true", help="只算增量区间，不调 API 不写库")
    parser.add_argument("--skip-export", action="store_true", help="只写 PG，不导出 CSV")
    parser.add_argument("--skip-git", action="store_true", help="不做 git 提交推送")
    parser.add_argument("--no-push", action="store_true", help="提交但不推送")
    parser.add_argument("--cron", action="store_true", help="定时任务模式（静默输出）")
    parser.add_argument("--tencent", action="store_true", help="应急通道：腾讯源直写 daily/ CSV（不进日常流程）")
    args = parser.parse_args()

    load_env()
    CRON["on"] = args.cron

    log("=== stock_data fetch & backup (v2) ===")

    # ── 应急通道 ──
    if args.tencent:
        n = fetch_from_tencent(args.dry_run)
        log(f"应急通道完成: {n} 行")
        return

    # ── 域选择 ──
    if args.only == "all":
        domains = list(ALL_DOMAINS)
    else:
        domains = [d.strip() for d in args.only.split(",") if d.strip()]
        bad = [d for d in domains if d not in ALL_DOMAINS]
        if bad:
            log(f"未知数据域: {bad} (可选: {ALL_DOMAINS})", "ERROR")
            sys.exit(2)

    log(f"模式: {'DRY-RUN ' if args.dry_run else ''}数据域: {domains}")

    # ── Step 0: 同步远程 ──
    if not args.dry_run and not args.skip_git:
        git_pull()

    # ── Step 1: PG 连接 ──
    try:
        conn = get_pg_conn()
    except Exception as e:
        log(f"PostgreSQL 不可用: {e}（应急可用 --tencent 通道）", "ERROR")
        sys.exit(1)

    target = get_latest_trade_date(conn)
    if not target:
        log("trade_cal 无数据，无法确定目标交易日", "ERROR")
        sys.exit(1)
    log(f"目标交易日: {target}")

    # ── Step 2: 拉数据（只写 PG）──
    total = 0
    pro = None
    if any(d in domains for d in ("a", "etf", "index")):
        pro = get_tushare_pro()

    if "a" in domains:
        if pro:
            total += fetch_a_share(conn, pro, target, args.dry_run)
        else:
            log("跳过 A股日线（无 token）", "WARN")

    if "hk" in domains:
        total += fetch_hk(conn, target, args.dry_run)

    if "etf" in domains:
        if pro:
            total += fetch_etf(conn, pro, target, args.dry_run)
        else:
            log("跳过 ETF 日线（无 token）", "WARN")

    if "index" in domains:
        if pro:
            total += fetch_index(conn, pro, target, args.dry_run)
        else:
            log("跳过指数日线（无 token）", "WARN")

    log(f"数据拉取完成: 共写入 {total} 行")

    # ── Step 3: 导出 CSV ──
    if args.skip_export or args.dry_run:
        log("[导出] 跳过" + (" (dry-run)" if args.dry_run else " (--skip-export)"))
    else:
        if not run_export():
            log("导出失败，数据已在 PG，可手动重跑 scripts/export.py", "ERROR")

    # ── Step 4: Git 提交 ──
    if args.skip_git or args.dry_run:
        log("[git] 跳过")
    else:
        if not git_commit_and_push(no_push=args.no_push):
            log("git 推送失败", "ERROR")
            sys.exit(1)

    log("✅ 完成" + (" [dry-run]" if args.dry_run else ""))


if __name__ == "__main__":
    main()

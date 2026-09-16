#!/usr/bin/env python3
"""策略回放共享库
backtest_report / alpha_monitor / decision_snapshot 共用:
单因子缓存加载 · strategy_config 解析(支持 @win=92-98,topn=30 覆盖后缀) · 截面打分 · 选股
"""
import os
import sys

import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import factor_eval as fe  # noqa: E402  (get_conn / loaders / 常量)


def load_factor(conn, name: str, start, end) -> pd.DataFrame:
    """单因子宽表 (parquet 缓存 + PG 增量补齐, 与 fe.load_factors 同语义, 只加载需要的因子)"""
    f = os.path.join(fe.CACHE_DIR, f"{name}.parquet")
    lo, hi = pd.Timestamp(start), pd.Timestamp(end)
    if os.path.exists(f):
        wide = pd.read_parquet(f)
        cur = conn.cursor()
        cur.execute("SELECT MAX(trade_date) FROM factor_value WHERE factor_name = %s", (name,))
        pg_max = cur.fetchone()[0]
        cur.close()
        if pg_max is not None and pd.Timestamp(pg_max) > wide.index.max():
            df = pd.read_sql(
                "SELECT trade_date, ts_code, value FROM factor_value "
                f"WHERE factor_name = '{name}' AND trade_date > '{wide.index.max().date()}'", conn)
            if len(df):
                inc = fe._pivot_unique(df, "trade_date", "ts_code", "value")
                inc.index = pd.to_datetime(inc.index)
                wide = pd.concat([wide, inc]).sort_index().sort_index(axis=1)
                wide.to_parquet(f)
    else:
        df = pd.read_sql(
            f"SELECT trade_date, ts_code, value FROM factor_value WHERE factor_name = '{name}'", conn)
        wide = fe._pivot_unique(df, "trade_date", "ts_code", "value")
        wide.index = pd.to_datetime(wide.index)
        wide.to_parquet(f)
    return wide.loc[(wide.index >= lo) & (wide.index <= hi)].sort_index(axis=1)


def load_strategies(conn, specs: list) -> list:
    """specs: ['prod_6f_eq', 'prod_6f_eq@win=92-98,topn=30'] → 策略配置列表 (覆盖后缀仅改内存, 不写库)"""
    out = []
    for sp in specs:
        sid, overrides = sp, {}
        if "@" in sp:
            sid, ov = sp.split("@", 1)
            for kv in ov.split(","):
                k, v = kv.split("=", 1)
                overrides[k.strip()] = v.strip()
        cur = conn.cursor()
        cur.execute("SELECT strategy_id, name, config FROM strategy_config WHERE strategy_id = %s", (sid,))
        row = cur.fetchone()
        cur.close()
        if not row:
            raise SystemExit(f"strategy_config 不存在: {sid}")
        cfg = dict(row[2])
        if "topn" in overrides:
            cfg["top_n"] = int(overrides["topn"])
        if "win" in overrides:
            cfg["window"] = [float(x) for x in overrides["win"].split("-")]
        label = row[1] + (f" ({ov})" if overrides else "")
        out.append({"id": sp, "sid": sid, "name": row[1], "label": label, "cfg": cfg})
    return out


def score_cross(fwides: dict, weights: dict, t, uni: set, list_dates: dict, min_cross: int = 50):
    """t 日截面打分: 股票池过滤 → 逐因子 z-score → 加权合成。
    fwides: {因子名: 宽表 DataFrame}; weights: {因子名: 权重 float}
    返回 (score: Series, zdf: DataFrame[各因子z×权重]); 截面不足返回 (None, None)"""
    zs = {}
    for name, w in weights.items():
        fw = fwides[name]
        row = fw.loc[t].dropna() if t in fw.index else pd.Series(dtype=float)
        keep = [c for c in row.index if c in uni and list_dates.get(c) is not None
                and t > pd.Timestamp(list_dates[c]) + pd.Timedelta(days=120)]
        row = row[keep]
        if len(row) < min_cross:
            return None, None
        sd = row.std()
        zs[name] = ((row - row.mean()) / (sd if sd and sd > 0 else 1.0)) * w
    zdf = pd.DataFrame(zs)
    score = zdf.sum(axis=1, min_count=len(zs) // 2)
    return score, zdf


def select_stocks(score: pd.Series, top_n: int, window=None):
    """选股。window=[lo,hi] 百分位窗口 (如 [92,98]: 只在 P92~P98 里选, 跳过最极端前 2%)。
    返回 (入选代码列表, 降序排名全表)"""
    ranked = score.dropna().sort_values(ascending=False)
    if window:
        pct = score.rank(pct=True)
        eligible = pct[(pct >= window[0] / 100) & (pct <= window[1] / 100)].index
        ranked = ranked[ranked.index.isin(eligible)]
    return list(ranked.head(top_n).index), ranked


def load_recent_returns(conn, ts_codes: list, days: int = 60) -> pd.DataFrame:
    """近 N 交易日日收益宽表 (index=交易日, columns=ts_code), 供协方差估计。"""
    if not ts_codes:
        return pd.DataFrame()
    import numpy as np  # noqa: F401
    ph = ",".join(["%s"] * len(ts_codes))
    cur = conn.cursor()
    cur.execute(f"SELECT trade_date, ts_code, pct_chg FROM daily_quote "
                f"WHERE ts_code IN ({ph}) AND trade_date >= (CURRENT_DATE - %s) "
                f"ORDER BY trade_date", (*ts_codes, days + 30))
    df = pd.DataFrame(cur.fetchall(), columns=["trade_date", "ts_code", "pct_chg"])
    cur.close()
    if df.empty:
        return pd.DataFrame()
    df["pct_chg"] = pd.to_numeric(df["pct_chg"], errors="coerce") / 100.0
    wide = df.pivot(index="trade_date", columns="ts_code", values="pct_chg").sort_index()
    return wide.tail(days)


def renormalize_with_cap(w: pd.Series, cap: float = 0.10) -> pd.Series:
    """剔除低权重持仓后的再归一化, 同时保持单票上限 (water-filling 投影)。
    简单 w/w.sum() 会因剔除放大剩余权重导致轻微超 cap, 此处迭代压回。"""
    import numpy as np
    w = w.astype(float).copy()
    if w.sum() <= 0:
        return w
    for _ in range(50):
        over = w > cap
        if not over.any():
            break
        excess = float((w[over] - cap).sum())
        w[over] = cap
        free = ~over
        fs = float(w[free].sum())
        if fs > 0:
            w[free] += excess * w[free] / fs
        else:
            break
    w = w / w.sum()
    # 投影后仍可能因浮点轻微超 cap → 最后硬截一次再归一 (偏差 <1e-9)
    w = w.clip(upper=cap)
    return w / w.sum()


def load_industry_map(conn) -> dict:
    """ts_code → 申万行业 (stocks.industry), 空/NaN → '其他'。仅 A 股。"""
    cur = conn.cursor()
    cur.execute("SELECT ts_code, industry FROM stocks WHERE ts_code NOT LIKE '%.HK'")
    out = {}
    for ts_code, ind in cur.fetchall():
        ind = (str(ind).strip() if ind is not None else "")
        out[ts_code] = ind if ind and ind.lower() != "nan" else "其他"
    cur.close()
    return out


def apply_industry_cap(w: pd.Series, industry_map: dict, cap: float = 0.15,
                       single_cap: float | None = None) -> pd.Series:
    """联合约束投影: 单一行业权重之和 ≤ cap, 同时单票 ≤ single_cap (若给)。
    交替投影 (对凸的 cap 约束收敛): 反复 [单票 water-filling → 行业 water-filling]。
    注意: 顺序施加 [先行业后单票] 会互相破坏 (单票 cap 的多余权重回流行业),
    所以这里必须联合投影。single_cap 默认 None = 不额外约束单票。"""
    import numpy as np
    w = w.astype(float).copy()
    if w.sum() <= 0:
        return w
    ind = pd.Series([industry_map.get(c, "其他") for c in w.index], index=w.index)

    def cap_elements(s, cap_):
        """元素级 water-filling: 超 cap_ 的元素压到 cap_, 多余按比例分给未超元素。"""
        s = s.astype(float).copy()
        for _ in range(50):
            over = s > cap_ + 1e-12
            if not over.any():
                break
            excess = float((s[over] - cap_).sum())
            s[over] = cap_
            free = ~over
            fs = float(s[free].sum())
            if fs <= 0:
                break
            s[free] += excess * s[free] / fs
        return s

    def cap_industries(s):
        """行业级 water-filling: 超 cap 行业按比例缩到 cap, 多余分给未超行业。"""
        s = s.astype(float).copy()
        for _ in range(50):
            ind_w = s.groupby(ind).sum()
            over = ind_w[ind_w > cap + 1e-12]
            if over.empty:
                break
            excess_total = 0.0
            for ind_name in over.index:
                mask = ind == ind_name
                tot = float(s[mask].sum())
                if tot <= 0:
                    continue
                excess_total += tot - cap
                s[mask] = s[mask] * (cap / tot)
            ind_w = s.groupby(ind).sum()
            free = ind_w[ind_w <= cap + 1e-12]
            free_total = float(free.sum())
            if free_total <= 0:
                break
            free_mask = ind.isin(free.index)
            s[free_mask] = s[free_mask] * (1.0 + excess_total / free_total)
        return s

    for _ in range(200):
        prev = w.copy()
        if single_cap is not None:
            w = cap_elements(w, single_cap)
        w = cap_industries(w)
        ok_single = (single_cap is None) or bool((w <= single_cap + 1e-9).all())
        ok_ind = bool((w.groupby(ind).sum() <= cap + 1e-9).all())
        if ok_single and ok_ind:
            break
        if float(np.abs(w - prev).max()) < 1e-12:
            break
    s = float(w.sum())
    return w / s if s > 0 else w


def optimize_weights(hist: pd.DataFrame, scheme: str, cap: float = 0.10) -> pd.Series:
    """风险端加权 (在排序选出的持仓内), 前提=排序可靠/幅度不可靠 → 不做 MVO 收益项。
    hist: (dates × stocks) 历史日收益; scheme: equal/inv_vol/min_var/risk_parity。
    cap: 单票权重上限 (min_var 用, None=无上限)。返回权重 Series (和=1, 非负, 单票≤cap)。"""
    import numpy as np
    n = hist.shape[1]
    cols = hist.columns.tolist()
    if scheme == "equal" or n == 0:
        return pd.Series(np.ones(n) / n, index=cols)
    rets = hist.fillna(0.0).to_numpy(dtype=np.float64)
    sig = rets.std(axis=0)
    if scheme == "inv_vol":
        iv = np.where(sig > 1e-8, 1.0 / sig, 0.0)
        w = iv / iv.sum() if iv.sum() > 0 else np.ones(n) / n
        return pd.Series(w, index=cols)
    try:
        from sklearn.covariance import LedoitWolf
        cov = LedoitWolf().fit(rets).covariance_
    except Exception:
        cov = np.cov(rets, rowvar=False) + 1e-6 * np.eye(n)
    if scheme == "min_var":
        from scipy.optimize import minimize
        def obj(w):
            return float(w @ cov @ w)
        cons = [{"type": "eq", "fun": lambda w: w.sum() - 1.0}]
        bnds = [(0.0, cap)] * n
        res = minimize(obj, np.ones(n) / n, method="SLSQP", bounds=bnds,
                       constraints=cons, options={"maxiter": 300, "ftol": 1e-12})
        w = res.x if res.success else np.ones(n) / n
        w = np.maximum(w, 0.0)
        w = w / w.sum() if w.sum() > 0 else np.ones(n) / n
        return pd.Series(w, index=cols)
    if scheme == "risk_parity":
        w = np.ones(n) / n
        for _ in range(50):
            marg = cov @ w
            sig_p = float(np.sqrt(w @ marg))
            if sig_p <= 1e-12:
                break
            rc = w * marg / sig_p
            target = sig_p / n
            scale = np.sqrt(np.where(rc > 1e-12, target / rc, 1.0))
            w = w * scale
            w = np.maximum(w, 0.0)
            w = w / w.sum()
        return pd.Series(w, index=cols)
    return pd.Series(np.ones(n) / n, index=cols)

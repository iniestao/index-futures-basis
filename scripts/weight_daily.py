# -*- coding: utf-8 -*-
"""
每日指数权重（流通市值近似）

思路：中证官方只公布**月末**权重（权重文件里 日期 恒为月末）。月内静态权重会在调仓/
除权/停牌期产生偏差，且月内个股相对涨跌完全不被反映。本模块把月末官方权重当作
**锚点**，在锚点之间用个股「流通市值」的相对变动把权重漂移到每日：

    w_i(t) = w_i(A) · FFMC_i(t)/FFMC_i(A)  /  Σ_j [ w_j(A) · FFMC_j(t)/FFMC_j(A) ]  × Σ_j w_j(A)

其中 A = ≤t 最近的中证月末权重日，FFMC = 自由流通市值。
归一化分母含**全部成分股**（而非仅事件股票），保证单位与官方文件一致（百分点）。

为什么锚定而不是直接按流通市值重算：中证权重 = Σ(自由流通市值 × 加权比例因子)，
因子按分级靠档（≤15%→15%…100%）且仅在定期调整时更新；把官方权重当锚点，
因子差异被锚点吸收，本模块只需刻画**期内相对变动**，精度显著高于纯自算。

FFMC 代理：raw_close(t) × 送转累积因子(t)
  - raw_close 取新浪**不复权**收盘价（data_raw/stock_prices/close_YYYY.csv）
  - 送转（送股/转股）使股本按 1+送转比例/10 增加、不复权价同步除权，市值不变，
    故用分红表「送转股份-送转总比例」做联动修正，消除除权跳空
  - 现金分红不修正：派现是真实现金流出，流通市值（及指数权重）应当同步下降
  - 停牌：用 ≤t 最近可得价格（forward fill），等价于价格不动
  - 解禁/增发/配股造成的流通股本变动未纳入（数量级小且逐月被锚点重置），见 METHODOLOGY 局限
"""
import os, glob, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import RAW

import numpy as np
import pandas as pd

PRICE_DIR = os.path.join(RAW, "stock_prices")
DIV_DIR = os.path.join(RAW, "dividends")


_PX_CACHE = {}       # years -> 合并后的价格面板（跨品种复用，省去重复读盘）
_SHARE_CACHE = {}    # code -> 送转事件（跨品种复用）


# ---------- 价格面板 ----------
def load_price_panel(dates, codes):
    """
    读取并按 dates 对齐的**不复权收盘价**面板（index=date 'YYYY-MM-DD'，columns=codes）。
    返回 None 表示无任何价格数据（此时调用方应回退到月末静态权重）。
    """
    if not dates:
        return None
    years = sorted({int(d[:4]) for d in dates})
    key = tuple(range(min(years), max(years) + 1))
    full = _PX_CACHE.get(key)
    if full is None:
        frames = []
        for y in key:
            fp = os.path.join(PRICE_DIR, f"close_{y}.csv")
            if not os.path.exists(fp):
                continue
            df = pd.read_csv(fp, index_col=0, dtype={0: str})
            df.index = df.index.astype(str)
            frames.append(df)
        if not frames:
            return None
        full = pd.concat(frames, axis=0).sort_index()
        full = full[~full.index.duplicated(keep="last")]
        _PX_CACHE[key] = full
    keep = [c for c in codes if c in full.columns]
    if not keep:
        return None
    px = full[keep]
    px = px.reindex(sorted(set(px.index) | set(dates))).ffill()
    return px.reindex(dates)


# ---------- 送转累积因子 ----------
def load_share_events(codes):
    """{code: [(除权除息日 'YYYY-MM-DD', 因子 1+送转比例/10)]}，只取已实施且有除权日的记录"""
    out = {}
    cols = ["送转股份-送转总比例", "除权除息日", "方案进度"]
    for code in codes:
        if code in _SHARE_CACHE:
            if _SHARE_CACHE[code]:
                out[code] = _SHARE_CACHE[code]
            continue
        fp = os.path.join(DIV_DIR, f"{code}.csv")
        if not os.path.exists(fp) or os.path.getsize(fp) < 50:
            _SHARE_CACHE[code] = []
            continue
        try:
            df = pd.read_csv(fp, usecols=lambda c: c in cols)
        except Exception:
            _SHARE_CACHE[code] = []
            continue
        if "送转股份-送转总比例" not in df.columns or "除权除息日" not in df.columns:
            _SHARE_CACHE[code] = []
            continue
        ratio = pd.to_numeric(df["送转股份-送转总比例"], errors="coerce")
        ex = pd.to_datetime(df["除权除息日"], errors="coerce")
        m = df["方案进度"].astype(str) if "方案进度" in df.columns else pd.Series("", index=df.index)
        ok = ratio.notna() & (ratio > 0) & ex.notna() & m.str.contains("实施", na=False)
        ev = sorted((d.strftime("%Y-%m-%d"), 1.0 + r / 10.0) for r, d in zip(ratio[ok], ex[ok]))
        _SHARE_CACHE[code] = ev
        if ev:
            out[code] = ev
    return out


def apply_share_events(px, dates, events):
    """把送转累积因子乘到不复权价上，得到流通市值代理（比例意义）"""
    adj = px.astype("float32").copy()
    idx = np.array(dates)
    for code, evs in events.items():
        if code not in adj.columns:
            continue
        col = adj[code].to_numpy(dtype="float32").copy()
        mult = np.ones(len(idx), dtype="float32")
        for ex, f in evs:
            mult[idx >= ex] *= np.float32(f)
        adj[code] = col * mult
    return adj


# ---------- 每日权重 ----------
def daily_weight_matrix(dates, timeline, snapshot, extra_codes):
    """
    返回 (W, axis)：
      axis = 全部历史成分 ∪ 当前快照 ∪ extra_codes（排序后）
      W    = (len(dates), len(axis)) float32，单位与中证权重文件一致（百分点，行和≈100）
    无价格数据时返回 (None, None)，调用方回退月末静态权重。
    """
    axis = set(extra_codes)
    for _, wmap in timeline:
        axis |= set(wmap)
    axis |= set(snapshot)
    axis = sorted(axis)
    if not axis or not dates:
        return None, None

    px = load_price_panel(dates, axis)
    if px is None:
        return None, None
    px = px.reindex(columns=axis)
    # 注意：不得剔除"无价格数据"的成分股 —— 它们必须留在归一化分母里（漂移记 1.0，
    # 等价于权重不动）。若剔除，剩余个股权重会被整体放大，反而引入系统性偏差。
    cov = float(px.notna().any(axis=0).mean())
    min_cov = float(os.environ.get("MIN_PRICE_COV", "0.5"))
    if cov < min_cov:
        print(f"  [WARN] price coverage {cov*100:.1f}% < {min_cov*100:.0f}%; skip daily weights", flush=True)
        return None, None

    adj = apply_share_events(px, dates, load_share_events(axis))
    A = adj.to_numpy(dtype="float32")          # (n_d, n_a)
    A[~np.isfinite(A) | (A <= 0)] = np.nan

    n_d, n_a = A.shape
    W = np.zeros((n_d, n_a), dtype="float32")
    dts = np.array(dates)

    # 每个交易日 -> 锚点（≤t 最近的中证月末文件；t 早于最早文件时用最早一期，但基准价取 t 当日，避免前视）
    keys = np.array([d.replace("-", "") for d in dates])
    ann = np.full(n_d, -1, dtype=int)
    j = -1
    for i in range(n_d):
        while j + 1 < len(timeline) and timeline[j + 1][0] <= keys[i]:
            j += 1
        ann[i] = j
    if (ann < 0).any():
        ann[ann < 0] = 0          # 早于最早权重文件：借用第 1 期权重（基准价仍取当日，故期内漂移为 0）

    for j in range(len(timeline)):
        rows = np.where(ann == j)[0]
        if rows.size == 0:
            continue
        m_end, wmap = timeline[j]
        w0 = np.array([wmap.get(c, 0.0) for c in axis], dtype="float64")
        if w0.sum() <= 0:
            continue
        anchor_str = f"{m_end[:4]}-{m_end[4:6]}-{m_end[6:]}"
        pos = int(np.searchsorted(dts, anchor_str, side="right")) - 1
        for i in rows:
            base_pos = min(pos, i) if pos >= 0 else i     # 锚点晚于 t 时用当日价 → 漂移为 0（无前视）
            base = A[base_pos]
            cur = A[i]
            ratio = np.where(np.isfinite(cur) & np.isfinite(base), cur / base, 1.0)
            ratio = np.where(np.isfinite(ratio) & (ratio > 0), ratio, 1.0)
            num = np.where(w0 > 0, w0 * ratio, 0.0)
            s = num.sum()
            if s > 0:
                W[i] = (num / s * w0.sum()).astype("float32")
    return W, axis

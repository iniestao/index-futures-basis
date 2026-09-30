# -*- coding: utf-8 -*-
"""
调整基差计算引擎 V2（三口径并列 + 月度历史权重）
B_adj = B + DPV = F − (S − DPV)，DPV 点数 = Σ w_i(t) × y_i × S_t
权重：用户提供的月度历史权重文件（weights/{idx}.SH_YYYYMMDD.csv），缺失月回退当前快照
三口径并列输出（用户自主选择）：
  _y 固定股息率  _d 固定分红  _p 固定派息率（均为信息集内 3 年均值外推，无前视）
"""
import io, os, sys, glob, math, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import (RAW, OUT, PRODUCTS, START, END, month_range, third_friday,
                    env_setup, INDEX_MEMBERS)
from weight_daily import daily_weight_matrix
env_setup()
import numpy as np
import pandas as pd

DIV_DIR = os.path.join(RAW, "dividends")
FUT_CSV = os.path.join(RAW, "futures", "cffex_daily_all.csv")
EPS_Q_CSV = os.path.join(RAW, "eps_quarterly.csv")


# ---------- 季报 EPS（用于 pq 口径） ----------
def load_eps_quarterly():
    """
    季报累计 EPS → {code: [(notice_ord, report_year, report_month, eps_cum), ...]}
    仅保留「公告日 ≤ 使用时刻」的记录，保证无前视。
    """
    if not os.path.exists(EPS_Q_CSV):
        return {}
    try:
        df = pd.read_csv(EPS_Q_CSV, dtype={"stock_code": str})
    except Exception:
        return {}
    need = {"stock_code", "report_date", "eps_cum", "notice_date"}
    if not need.issubset(df.columns):
        return {}
    df["notice_date"] = pd.to_datetime(df["notice_date"], errors="coerce")
    df["report_date"] = pd.to_datetime(df["report_date"], errors="coerce")
    df["eps_cum"] = pd.to_numeric(df["eps_cum"], errors="coerce")
    df = df[df["notice_date"].notna() & df["report_date"].notna() & df["eps_cum"].notna()]
    out = {}
    for c, g in df.groupby("stock_code", sort=False):
        g = g.sort_values(["report_date", "notice_date"])
        out[c] = [(r.notice_date, r.report_date.year, r.report_date.month, float(r.eps_cum))
                  for r in g.itertuples()]
    return out


def _ttm(avail, year, mth, cum_now):
    """TTM run-rate：上年全年 + 本期累计 − 上年同期累计；上年同期缺失时退化为累计年化"""
    if mth == 12:
        return cum_now
    ann_prev = [r for r in avail if r[1] == year - 1 and r[2] == 12]
    base_prev = [r for r in avail if r[1] == year - 1 and r[2] == mth]
    if ann_prev and base_prev:
        return cum_now + ann_prev[-1][3] - base_prev[-1][3]
    return cum_now * 12.0 / mth if mth else np.nan


def eps_est_asof(qrows, asof, target_year):
    """
    信息集 ≤ asof 时，对 target_year 财年 EPS 的估计（季报外推，无前视）：
      1) 目标年度年报已披露 → 用实际值
      2) 目标年度有季报（Q1/H1/Q3）→ TTM 外推：上年全年 + 本期累计 − 上年同期累计
      3) 目标年度尚无披露 → 用最近可得报告期的 TTM run-rate（比只用上年年报更及时）
    """
    if not qrows or asof is None or pd.isna(asof):
        return np.nan
    avail = [r for r in qrows if r[0] <= asof]
    if not avail:
        return np.nan
    ann_t = [r for r in avail if r[1] == target_year and r[2] == 12]
    if ann_t:
        return ann_t[-1][3]
    cur = [r for r in avail if r[1] == target_year]
    if cur:
        last = max(cur, key=lambda r: (r[2], r[0]))
        return _ttm(avail, target_year, last[2], last[3])
    latest = max(avail, key=lambda r: (r[1], r[2]))
    return _ttm(avail, latest[1], latest[2], latest[3])

# ---------- 合约规则 ----------
def roll_contract(today_ym, months_ahead):
    y, m = divmod(today_ym, 100)
    m += months_ahead
    y += (m - 1) // 12
    return y * 100 + (m - 1) % 12 + 1

def four_contracts_for_day(date: dt.date):
    this_month = date.year * 100 + date.month
    tf = third_friday(this_month)
    cur = this_month if date <= tf else roll_contract(this_month, 1)
    nxt = roll_contract(cur, 1)
    qs = []
    q = cur
    while len(qs) < 2:
        y, m = divmod(roll_contract(q, 1), 100)
        q = y * 100 + m
        if m in (3, 6, 9, 12):
            qs.append(q)
    return [cur, nxt] + qs

# ---------- 权重时间线 ----------
def repair_weights(wmap, deficit, omitted_code=None):
    """
    修复权重文件的整行缺失。

    背景：Wind 导出的中证月末权重文件把**代码最小的那只成分股**整行丢了
    （实测 139/141 期行数比指数成分数少 1，IF 全期缺 000001.SZ，IH 2015 年缺口达 4.5% 权重），
    导致该期 DPV 系统性低估。缺口 = 100 − Σ(文件权重) 即缺失成分的权重。

    修复策略：
      1) 若能确定被省略的代码（在完整期出现、且所有缺行期都不出现）→ 原样补回并赋缺口权重；
      2) 否则按缺口把文件内权重归一到 100 —— 等价于「给缺失成分按指数平均股息率插补」，
         在缺失成分股息率≈指数平均时严格正确，误差远小于整体遗漏。
    """
    if deficit <= 0.05:
        return wmap, None
    if omitted_code and 0.05 <= deficit <= 3.0:
        wmap = dict(wmap)
        wmap[omitted_code] = deficit
        return wmap, f"补回{omitted_code}"
    k = 100.0 / (100.0 - deficit)
    return {c: w * k for c, w in wmap.items()}, f"归一×{k:.5f}"


def load_weight_timeline(index_code, repair=True):
    """返回 sorted [(month_end 'YYYY-MM-DD', {code6: w_pct})] + 快照 fallback"""
    tl, nrow, raw = [], {}, {}
    w_dir = os.path.join(RAW, "weights")
    for fp in glob.glob(os.path.join(w_dir, f"{index_code}.SH_*.csv")):
        base = os.path.basename(fp)
        month_end = base.replace(f"{index_code}.SH_", "").replace(".csv", "")
        try:
            df = pd.read_csv(fp)
            if "wind_code" not in df.columns or "i_weight" not in df.columns:
                continue
            df["_code"] = df["wind_code"].astype(str).str[:6]
            df["i_weight"] = pd.to_numeric(df["i_weight"], errors="coerce")
            df = df[df["i_weight"].notna() & (df["i_weight"] > 0)]
            wmap = dict(zip(df["_code"], df["i_weight"]))
            raw[month_end] = wmap
            nrow[month_end] = len(df)
            tl.append((month_end, wmap))
        except Exception:
            continue
    tl.sort(key=lambda x: x[0])
    if not repair or not tl:
        return tl

    exp = INDEX_MEMBERS.get(index_code)
    if not exp:
        return tl
    full_sets = [set(w) for m, w in tl if nrow[m] >= exp]
    short = [m for m, _ in tl if nrow[m] < exp]
    # 被整表省略的代码：完整期都含、且所有缺行期都不含
    cand = set(full_sets[0]).intersection(*full_sets) if full_sets else set()
    for m in short:
        cand -= set(raw[m])
    omitted = sorted(cand)[0] if len(cand) == 1 else None

    fixed, fix_n, ins_n = [], 0, 0
    for m, wmap in tl:
        deficit = 100.0 - sum(wmap.values())
        if nrow[m] < exp and deficit > 0.05:
            newmap, how = repair_weights(wmap, deficit, omitted)
            if how:
                fix_n += 1
                if how.startswith("补回"):
                    ins_n += 1
            fixed.append((m, newmap))
        else:
            fixed.append((m, wmap))
    if fix_n:
        print(f"  [权重修复] {index_code}: {fix_n}/{len(tl)} 期整行缺失已修复"
              f"（补回代码 {ins_n} 期{('/ ' + omitted) if omitted else '，其余按缺口归一'}"
              f"）；平均缺口 {100.0 - sum(sum(w.values()) for _, w in tl) / len(tl):.3f}", flush=True)
    return fixed

def load_snapshot_weights(index_code):
    fp = os.path.join(RAW, "weights", f"{index_code}_weights.csv")
    if os.path.exists(fp):
        df = pd.read_csv(fp, dtype={"stock_code": str})
        return dict(zip(df["stock_code"], df["weight_pct"]))
    return {}

def get_weight_vec(t_str, timeline, snapshot, codes):
    """t 时刻权重向量：用 ≤t 最近月末文件；无任何月度文件则当前快照；个股缺失记 0（视为非成分）"""
    if timeline:
        chosen = None
        for m_end, wmap in timeline:
            if m_end <= t_str.replace("-", ""):
                chosen = wmap
            else:
                break
        if chosen is None:
            chosen = timeline[0][1]  # t 早于最早月末文件：用最早一期
        return np.array([chosen.get(c, 0.0) for c in codes])
    return np.array([snapshot.get(c, 0.0) for c in codes])

# ---------- 事件表 ----------
def load_events(product, index_code):
    # 事件候选池 = 月度历史权重文件成分并集 ∪ 当前快照成分。
    # 修复：原先只用当前快照 {product}_weights.csv，历史成分（后被调出的股票）
    # 的分红事件被整体漏掉，导致历史 DPV 系统性低估、调整基差偏差。
    # 注：已退市股票东财F10接口无分红数据（返回空），属数据源边界，无法免费补齐。
    members = set()
    for _, wmap in load_weight_timeline(index_code):
        members |= set(wmap)
    snap_fp = os.path.join(RAW, "weights", f"{product}_weights.csv")
    if os.path.exists(snap_fp):
        snap_df = pd.read_csv(snap_fp, dtype={"stock_code": str})
        members |= set(snap_df["stock_code"])
    uni = pd.read_csv(os.path.join(RAW, "universe_all.csv"), dtype={"stock_code": str})
    rows = []
    for code in sorted(members & set(uni["stock_code"])):
        fp = os.path.join(DIV_DIR, f"{code}.csv")
        if not os.path.exists(fp) or os.path.getsize(fp) < 50:
            continue
        try:
            df = pd.read_csv(fp)
        except Exception:
            continue
        df["report_year"] = pd.to_datetime(df["报告期"], errors="coerce").dt.year
        df = df[df["report_year"].between(2013, dt.date.today().year)]
        pay = pd.to_numeric(df["现金分红-现金分红比例"], errors="coerce")
        df = df[(pay.notna()) & (pay > 0)]
        ann = pd.to_datetime(df["预案公告日"], errors="coerce")
        ex = pd.to_datetime(df["除权除息日"], errors="coerce")
        yr = pd.to_numeric(df["现金分红-股息率"], errors="coerce")
        eps = pd.to_numeric(df["每股收益"], errors="coerce")
        dps = pay / 10.0
        for i in df.index[ann.notna()]:
            rows.append((code, ann.loc[i], ex.loc[i],
                         float(yr.loc[i]) if np.isfinite(yr.loc[i]) else np.nan,
                         float(dps.loc[i]), float(eps.loc[i]) if np.isfinite(eps.loc[i]) else np.nan))
    ev = pd.DataFrame(rows, columns=["code", "ann", "ex", "yield_dec", "dps", "eps"])
    ev = ev.sort_values(["code", "ann"]).reset_index(drop=True)

    # ---- 分红类型标签（中期 vs 年报）----
    # A 股年报分红除息集中在 5-8 月，其余（9 月~次年 4 月）为中期/特别分红。
    # 用「除息日」判定；除息日为空（已公告未实施）的事件按「预案公告日」推断：
    #   7-12 月公告的多为中期/特别分红（当年），1-6 月公告的多为上年年报分红。
    # is_mid=1 表中期/特别，0 表年报。候选生成与股息率校准都按此类型分别建模。
    ex_dt = pd.to_datetime(ev["ex"], errors="coerce")
    ann_dt = pd.to_datetime(ev["ann"], errors="coerce")
    m_ex = ex_dt.dt.month
    m_ann = ann_dt.dt.month
    ev["is_mid"] = np.where(
        ex_dt.notna(),
        ~m_ex.between(5, 8),          # 有除息日：5-8 月=年报(0)，其余=中期(1)
        ~m_ann.between(1, 6),         # 无除息日：7-12 月公告=中期(1)，1-6 月=年报(0)
    ).astype(int)

    # ---- 四种预测收益率（均为信息集内无前视）----
    qeps = load_eps_quarterly()
    col_v0, col_y, col_d, col_p, col_pq, col_epsq = [], [], [], [], [], []
    for c, g in ev.groupby("code", sort=False):
        qrows = qeps.get(c, [])
        y_arr = g["yield_dec"].to_numpy(float)
        d_arr = g["dps"].to_numpy(float)
        e_arr = g["eps"].to_numpy(float)
        p_arr = np.where((e_arr > 0) & np.isfinite(e_arr), d_arr / e_arr, np.nan)
        for i in range(len(g)):
            hist = [j for j in range(i) if pd.notna(g["ann"].iloc[j]) and g["ann"].iloc[j] < g["ann"].iloc[i]]
            prev_i = hist[-1] if hist else None
            v0 = y_arr[prev_i] if prev_i is not None and np.isfinite(y_arr[prev_i]) else \
                 (y_arr[i] if np.isfinite(y_arr[i]) else 0.0)
            v0 = v0 if np.isfinite(v0) else 0.0
            col_v0.append(v0)
            # 参考价 P_ref = d_prev / y_prev（把金额/派息率口径换算回收益率）
            P_ref_ok = prev_i is not None and np.isfinite(d_arr[prev_i]) and d_arr[prev_i] > 0 \
                       and np.isfinite(y_arr[prev_i]) and y_arr[prev_i] > 0
            y_fix = y_dfix = y_pfix = y_pqfix = np.nan
            eps_target = np.nan
            if hist:
                win = hist[-3:]
                y3 = y_arr[win]; d3 = d_arr[win]; p3 = p_arr[win]
                y3v = y3[np.isfinite(y3)]; d3v = d3[np.isfinite(d3)]
                if len(y3v) >= 1 and np.nanmean(y3v) > 0:
                    y_fix = float(np.nanmean(y3v))                      # 固定股息率
                if len(d3v) >= 1 and np.nanmean(d3v) > 0 and P_ref_ok:
                    y_dfix = float(np.nanmean(d3v) * y_arr[prev_i] / d_arr[prev_i])  # 固定分红
                p3v = p3[np.isfinite(p3)]
                if len(p3v) >= 1 and np.nanmean(p3v) > 0 and P_ref_ok and prev_i is not None \
                   and np.isfinite(e_arr[prev_i]) and e_arr[prev_i] > 0:
                    y_pfix = float(np.nanmean(p3v) * e_arr[prev_i] * y_arr[prev_i] / d_arr[prev_i])  # 固定派息率
                    # pq：派息率 × 季报外推 EPS（目标财年 = 本事件所属财年 + 1，即下一期分红依据的财年）
                    # 事件所属财年由**公告日**推导：A 股年报分红在次年 1-6 月公告（属上一财年），
                    # 7-12 月公告的多为中期/特别分红（属当年）。
                    # 注意：早期版本这里读 ev 中并不存在的 report_year 列，KeyError 被
                    # `except Exception` 静默吞掉 → target_year 恒为 None → eps_est_q 全为 NaN
                    # → y_fix_pq 恒等于 y_fix_p，第 4 口径实际从未生效（2026-09-14 定位）。
                    ann_i = pd.to_datetime(g["ann"].iloc[i], errors="coerce")
                    if pd.notna(ann_i):
                        ev_year = ann_i.year - 1 if ann_i.month <= 6 else ann_i.year
                        eps_target = eps_est_asof(qrows, ann_i, ev_year + 1)
                    e_use = eps_target if np.isfinite(eps_target) and eps_target > 0 else e_arr[prev_i]
                    if np.isfinite(e_use) and e_use > 0:
                        y_pqfix = float(np.nanmean(p3v) * e_use * y_arr[prev_i] / d_arr[prev_i])
            col_y.append(y_fix if np.isfinite(y_fix) else v0)
            col_d.append(y_dfix if np.isfinite(y_dfix) else v0)
            col_p.append(y_pfix if np.isfinite(y_pfix) else v0)
            col_pq.append(y_pqfix if np.isfinite(y_pqfix) else v0)
            col_epsq.append(eps_target if np.isfinite(eps_target) else np.nan)
    ev["yield_true"] = ev["yield_dec"]  # 真值列（NaN 行在覆盖率统计中自然处理）
    ev["y_pred_v0"] = col_v0       # 上年递推（对照）
    ev["y_fix_y"] = col_y          # 固定股息率
    ev["y_fix_d"] = col_d          # 固定分红
    ev["y_fix_p"] = col_p          # 固定派息率（上年年报 EPS）
    ev["y_fix_pq"] = col_pq        # 固定派息率 × 季报外推 EPS（TTM）
    ev["eps_est_q"] = col_epsq     # 季报外推 EPS（诊断用）
    return ev

def build_est_ex(ev):
    out = []
    for c, g in ev.groupby("code", sort=False):
        med_iv = (pd.to_datetime(g["ex"]) - pd.to_datetime(g["ann"])).dt.days.median()
        prev_est = None
        for _, r in g.sort_values("ann").iterrows():
            if pd.notna(r["ex"]):
                est = r["ex"]
            else:
                if pd.notna(r["ann"]) and med_iv == med_iv:
                    est = r["ann"] + pd.Timedelta(days=float(med_iv))
                elif prev_est is not None:
                    est = prev_est + pd.Timedelta(days=365)
                else:
                    est = pd.NaT
            out.append(est)
            prev_est = est
    ev["est_ex"] = pd.Series(out, index=ev.index)
    return ev


def add_prediction_candidates(ev, ahead_days=400, back_days=30, per_stock_max=2):
    """
    为「尚未公告的未来分红」合成预测候选事件 —— 实现文档 §3 状态机中
    「候选 → 未公告 → 预测引擎」分支。

    缺口（2026-09-16 定位）：事件池只含已公告行，而四口径仅对 ann > t 的
    事件分化（pred_sel）。t 逼近数据末端时 ann > t 的事件必然趋空
    （2026-08-31 中报披露截止后 announced_ratio 恒为 1.0、四口径全同），
    当下 DPV 系统性漏算窗口内未公告分红 → basis_adj 高估。
    （历史 t 不受影响：那时的"未来公告"如今都已在池里。）

    中期分红显式建模（2026-09-30 重构）：
    之前把「历史所有 est_ex」各自 +365k 周年外推，未区分分红类型，导致
    ①年报除息日被机械映射到次年 1-2 月（分红真空期，凭空造出候选）；
    ②同一类型被 +365/+730/+1095 外推多期，候选数量膨胀 60%；
    ③已公告的中期分红与「下一期同类」候选对同一股票重复计息（34 只重叠）。
    重构后：
      1. 按 is_mid 分组，年报历史只外推年报候选、中期历史只外推中期候选；
      2. 每类型只外推「最近一期」的下一期（+365 一次），而非三期；
      3. 强判重：该类型已有「已公告的未来事件」（ann 已出、est_ex 在未来）
         落窗，则同类候选不再生成——已公告事件已占据该类型的下一期分红。
    候选四口径股息率继承该股「同类型历史真实事件」的中位数（_type_predict_yields，
    见下），ann = est_ex − 预案→除息中位间隔（未来日期，天然落 pred_sel）。
    """
    today = pd.Timestamp(dt.date.today())
    lo, hi = today - pd.Timedelta(days=back_days), today + pd.Timedelta(days=ahead_days)
    med_iv = (pd.to_datetime(ev["ex"]) - pd.to_datetime(ev["ann"])).dt.days
    cols = list(ev.columns)
    new_rows = []
    for c, g in ev.groupby("code", sort=False):
        g_real = g[g["ex"].notna()].copy()
        if g_real.empty:
            continue
        _ex = pd.to_datetime(g_real["ex"])
        g_real["_type"] = _ex.map(_div_type)
        ref = g_real.loc[_ex.idxmax()]
        miv = med_iv.loc[g.index].median()
        miv = float(miv) if pd.notna(miv) and miv > 0 else 60.0

        # 已公告的未来事件（ann 非空、est_ex 已估计到未来）：按类型登记，
        # 用于强判重——该类型若已有已公告事件落窗，则不再外推同类候选。
        announced_future = g[g["ann"].notna() & pd.to_datetime(g["est_ex"], errors="coerce").notna()]
        af_types = set()
        for _, r in announced_future.iterrows():
            exd = pd.to_datetime(r["est_ex"], errors="coerce")
            if pd.notna(exd) and lo <= exd <= hi:
                af_types.add(int(r["is_mid"]) if pd.notna(r["is_mid"]) else int(_div_type(exd) == "interim"))

        n = 0
        # 按分红类型分别外推：年报（is_mid=0）与中期（is_mid=1）各只外推最近一期
        for is_mid, type_name in ((0, "annual"), (1, "interim")):
            if n >= per_stock_max:
                break
            if is_mid in af_types:
                # 该类型已有已公告未来事件占据窗口，不再外推（强判重）
                continue
            type_ex = _ex[_ex.map(_div_type) == type_name]
            if type_ex.empty:
                continue
            base = type_ex.max()           # 该类型最近一次真实除息日
            cand = base + pd.Timedelta(days=365)   # 下一期同类型
            if not (lo <= cand <= hi):
                continue
            yp = _type_predict_yields(g_real, type_name, ref)
            if yp is None:
                continue
            row = {col: np.nan for col in cols}
            row.update(code=c, ann=cand - pd.Timedelta(days=miv), ex=pd.NaT,
                       est_ex=cand, candidate=1, is_mid=is_mid,
                       y_pred_v0=yp["y_fix_y"], y_fix_y=yp["y_fix_y"],
                       y_fix_d=yp["y_fix_d"], y_fix_p=yp["y_fix_p"],
                       y_fix_pq=yp["y_fix_pq"], eps_est_q=np.nan)
            new_rows.append(row)
            n += 1
    if not new_rows:
        return ev
    out = pd.concat([ev, pd.DataFrame(new_rows)], ignore_index=True)
    out["_is_candidate"] = out.get("candidate", pd.Series(0, index=out.index)).fillna(0).astype(int)
    return out.drop(columns=["candidate"], errors="ignore")


def _div_type(d):
    """分红类型：A 股年报分红除息集中在 5-8 月，其余（9 月~次年 4 月）为中期/特别分红。"""
    return "annual" if 5 <= d.month <= 8 else "interim"


def _type_predict_yields(real_g, div_type, ref):
    """
    基于「同分红类型的历史真实除息事件」计算候选的四口径预测股息率。

    背景（2026-09-30）：候选原继承「最新真实事件的前三次全样本均值」，该均值
    把 5-7 月年报大分红的高股息率与 9 月~次年 3 月中期分红的低股息率混在一起，
    导致淡季窗口（9 月~次年 3 月，全是中期/特别分红）的候选股息率系统性虚高
    50~73% → 中小盘（IC/IM）远月 DPV 高估 33~42%。改按「分红类型」分开建模：
      annual（除息 5-8 月）候选继承历史 annual 事件，interim（其余）继承 interim。
    淡季窗口候选天然全为 interim，股息率不再被年报大分红污染。

    取**中位数**而非均值（对稀疏的延后年报/特别分红离群值更鲁棒）。四口径：
      y   = median(yield_dec of 同类型)
      d   = median(dps of 同类型) × (y_ref/d_ref)
      p   = median(dps/eps of 同类型) × eps_ref × (y_ref/d_ref)
      pq  = 降级为 p（候选无公告日，无法推季报外推 EPS 目标财年）
    参考价基准 (y_ref,d_ref,eps_ref) 取该股「最近真实事件」。

    无同类型历史（该股从无此类分红）返回 None → 调用方跳过该候选（真空期过滤）。
    """
    g = real_g
    t = g["_type"]
    y = g["yield_dec"].to_numpy(float)
    d = g["dps"].to_numpy(float)
    e = g["eps"].to_numpy(float)
    p = np.where((e > 0) & np.isfinite(e), d / e, np.nan)
    win = np.where(t == div_type)[0]
    if win.size == 0:
        return None
    yv = y[win]; dv = d[win]; pv = p[win]
    yv = yv[np.isfinite(yv)]; dv = dv[np.isfinite(dv)]; pv = pv[np.isfinite(pv)]
    if yv.size == 0:
        return None
    y_fix = float(np.nanmedian(yv))
    # 参考价基准（最近真实事件）
    y_ref = float(ref["yield_dec"]) if np.isfinite(ref.get("yield_dec", np.nan)) else np.nan
    d_ref = float(ref["dps"]) if np.isfinite(ref.get("dps", np.nan)) else np.nan
    e_ref = float(ref["eps"]) if np.isfinite(ref.get("eps", np.nan)) else np.nan
    P_ref_ok = np.isfinite(d_ref) and d_ref > 0 and np.isfinite(y_ref) and y_ref > 0
    out = {"y_fix_y": y_fix}
    if dv.size >= 1 and np.nanmedian(dv) > 0 and P_ref_ok:
        out["y_fix_d"] = float(np.nanmedian(dv) * y_ref / d_ref)
    else:
        out["y_fix_d"] = y_fix
    if pv.size >= 1 and np.nanmedian(pv) > 0 and P_ref_ok and np.isfinite(e_ref) and e_ref > 0:
        out["y_fix_p"] = float(np.nanmedian(pv) * e_ref * y_ref / d_ref)
    else:
        out["y_fix_p"] = y_fix
    out["y_fix_pq"] = out["y_fix_p"]   # 候选无公告日，pq 降级为 p
    return out


def build_events(product, index_code):
    """统一入口：事件池 → est_ex → 未公告候选。main 与 compute_backtest 共用。"""
    return add_prediction_candidates(build_est_ex(load_events(product, index_code)))

# ---------- 主流程 ----------
# 面板固定列序：pd.DataFrame(recs) 的列序取决于首个 dict 的键序，而
# 「窗口内无事件」分支与正常分支的键序不同（calibre 位置不一致），曾导致
# IF/IH 与 IC/IM 的 panel 列顺序不一致（2026-09-16 用户发现）。输出前统一重排。
PANEL_COLUMNS = ["date", "product", "role", "contract", "expire", "spot", "future",
                 "basis_raw", "calibre", "dpv_pts", "basis_adj", "annualized_rate",
                 "ann_rate_raw", "announced_ratio"]


def main(end_date=None, out_suffix=""):
    fut = pd.read_csv(FUT_CSV, header=None,
                      names=["date", "symbol", "open", "high", "low", "close",
                             "settle", "pre_settle", "volume", "open_interest"],
                      dtype={"date": str, "symbol": str}, encoding="utf-8-sig", skiprows=1)
    fut = fut[fut["date"].astype(str).str.match(r"^\d{8}$", na=False)]
    fut = fut[fut["symbol"].astype(str).str.match(r"^(IF|IH|IC|IM)\d{4}$", na=False)]
    fut["date"] = pd.to_datetime(fut["date"].astype(str), format="%Y%m%d", errors="coerce").dt.strftime("%Y-%m-%d")
    fut["close"] = pd.to_numeric(fut["close"], errors="coerce")

    all_panels = []
    for prod, cfg in PRODUCTS.items():
        print(f"===== {prod} =====", flush=True)
        sidx = pd.read_csv(os.path.join(RAW, "index", f"{prod}_{cfg['sina_idx']}.csv"))
        sidx["date"] = pd.to_datetime(sidx["date"]).dt.strftime("%Y-%m-%d")
        sidx = sidx[(sidx["date"] >= START) & (sidx["date"] <= (end_date or END))].reset_index(drop=True)
        dates = sidx["date"].tolist()
        closes = sidx["close"].to_numpy(dtype=float)

        ev = build_events(prod, cfg["index"])
        if len(ev) == 0:
            print(f"[WARN] no events for {prod}")
            continue
        codes = ev["code"].unique().tolist()
        code_pos = {c: i for i, c in enumerate(codes)}
        ev_code_idx = ev["code"].map(code_pos).to_numpy(np.int64)

        dser0 = dt.date.fromisoformat(dates[0])
        e_ann_f = np.array([np.nan if pd.isna(x) else (x.date() - dser0).days
                            for x in pd.to_datetime(ev["ann"])], dtype=float)
        e_ex_f = np.array([np.nan if pd.isna(x) else (x.date() - dser0).days
                           for x in pd.to_datetime(ev["est_ex"])], dtype=float)
        Y_TRUE = ev["yield_true"].to_numpy(float)
        YP = {k: ev[k].to_numpy(float) for k in ("y_pred_v0", "y_fix_y", "y_fix_d", "y_fix_p", "y_fix_pq")}
        order = np.argsort(e_ex_f, kind="stable")
        E_EX, E_ANN, E_CODE = e_ex_f[order], e_ann_f[order], ev_code_idx[order]
        YT = Y_TRUE[order]
        YPx = {k: v[order] for k, v in YP.items()}

        fut_prod = fut[fut["symbol"].str.startswith(prod)]
        price_lookup = {(r.date, r.symbol): r.close for r in fut_prod.itertuples()}
        symbols_avail = set(fut_prod["symbol"])

        timeline = load_weight_timeline(cfg["index"])
        snapshot = load_snapshot_weights(cfg["index"])
        n_m = len(timeline)
        print(f"  weight months={n_m}, snapshot fallback={'yes' if n_m == 0 else 'no'}", flush=True)

        # 每日权重（流通市值近似）：锚定月末官方权重，期内按个股流通市值相对变动漂移
        W_daily, AXPOS_E = None, None
        if os.environ.get("STATIC_WEIGHTS", "").lower() not in ("1", "true", "yes"):
            try:
                W_daily, w_axis = daily_weight_matrix(dates, timeline, snapshot, codes)
            except Exception as e:
                W_daily = None
                print(f"  [WARN] daily weights unavailable ({type(e).__name__}: {e})", flush=True)
            if W_daily is not None:
                ax_pos = {c: i for i, c in enumerate(w_axis)}
                AXPOS_E = np.array([ax_pos.get(c, -1) for c in codes], dtype=int)[E_CODE]
                print(f"  daily weights ON (axis={len(w_axis)}, 覆盖事件权重 "
                      f"{100.0 * (AXPOS_E >= 0).mean():.1f}%)", flush=True)
        if W_daily is None:
            print("  daily weights OFF -> month-end static weights", flush=True)

        recs = []
        for ti, dstr in enumerate(dates):
            dd = dt.date.fromisoformat(dstr)
            tr = float((dd - dser0).days)
            S = closes[ti]
            if W_daily is not None:
                row = W_daily[ti]
                w_by_event = np.where(AXPOS_E >= 0, row[np.clip(AXPOS_E, 0, None)], 0.0)
            else:
                w_vec = get_weight_vec(dstr, timeline, snapshot, codes)
                w_by_event = w_vec[E_CODE]     # 事件对齐权重（当月非成分=0，天然剔除）
            contracts = four_contracts_for_day(dd)
            for role_i, ym in enumerate(contracts):
                role = ["current", "next", "q1", "q2"][role_i]
                sym = f"{prod}{ym % 10000:04d}"
                T_day = third_friday(ym)
                if T_day < dd:
                    continue
                Fv = price_lookup.get((dstr, sym), np.nan)
                T_rel = (T_day - dser0).days
                sel_mask = (E_EX > tr) & (E_EX <= T_rel)
                if not sel_mask.any():
                    for k in ("y", "d", "p", "pq"):
                        recs.append(dict(date=dstr, product=prod, role=role, contract=sym,
                                         expire=T_day.isoformat(), spot=S, future=Fv,
                                         basis_raw=Fv - S, dpv_pts=0.0, basis_adj=Fv - S,
                                         annualized_rate=np.nan, ann_rate_raw=np.nan,
                                         announced_ratio=np.nan, calibre=k))
                    continue
                true_sel = sel_mask & (E_ANN <= tr)
                pred_sel = sel_mask & ~true_sel
                true_part = np.nansum(w_by_event[true_sel] * YT[true_sel] / 100.0)
                cover = np.nan
                out_row_base = dict(date=dstr, product=prod, role=role, contract=sym,
                                    expire=T_day.isoformat(), spot=S, future=Fv,
                                    basis_raw=Fv - S)
                for k, ycol in (("y", "y_fix_y"), ("d", "y_fix_d"), ("p", "y_fix_p"), ("pq", "y_fix_pq")):
                    pred_part = np.nansum(w_by_event[pred_sel] * YPx[ycol][pred_sel] / 100.0)
                    tot = true_part + pred_part
                    dpv_pts = tot * S
                    cov = true_part / tot if tot > 0 else np.nan
                    years = max((T_day - dd).days, 0) / 365.0
                    B_adj = (Fv - S) + dpv_pts
                    ann_rate = (B_adj / S / years * 100) if years > 0 and np.isfinite(Fv) else np.nan
                    ann_rate_raw = ((Fv - S) / S / years * 100) if years > 0 and np.isfinite(Fv) else np.nan
                    recs.append(dict(**out_row_base, calibre=k, dpv_pts=dpv_pts,
                                     basis_adj=(Fv - S) + dpv_pts, annualized_rate=ann_rate,
                                     ann_rate_raw=ann_rate_raw,
                                     announced_ratio=cov))
        panel = pd.DataFrame(recs).reindex(columns=PANEL_COLUMNS)
        all_panels.append(panel)
        suffix = f"_{out_suffix}" if out_suffix else ""
        panel.to_csv(os.path.join(OUT, f"{prod}_panel{suffix}.csv"), index=False, encoding="utf-8-sig")
        print(f"[OK] {prod} panel rows={len(panel)}", flush=True)

    full = pd.concat(all_panels, ignore_index=True)
    full.to_csv(os.path.join(OUT, f"adjusted_basis_panel_all{('_' + out_suffix) if out_suffix else ''}.csv"),
                index=False, encoding="utf-8-sig")
    print("TOTAL rows:", len(full))

if __name__ == "__main__":
    end_arg = sys.argv[1] if len(sys.argv) > 1 else None
    sfx = sys.argv[2] if len(sys.argv) > 2 else ""
    main(end_date=end_arg, out_suffix=sfx)

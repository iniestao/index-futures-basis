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
from config import RAW, OUT, PRODUCTS, START, END, month_range, third_friday, env_setup
env_setup()
import numpy as np
import pandas as pd

DIV_DIR = os.path.join(RAW, "dividends")
FUT_CSV = os.path.join(RAW, "futures", "cffex_daily_all.csv")

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
def load_weight_timeline(index_code):
    """返回 sorted [(month_end 'YYYY-MM-DD', {code6: w_pct})] + 快照 fallback"""
    tl = []
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
            tl.append((month_end, dict(zip(df["_code"], df["i_weight"]))))
        except Exception:
            continue
    tl.sort(key=lambda x: x[0])
    return tl

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
def load_events(product):
    weights_df = pd.read_csv(os.path.join(RAW, "weights", f"{product}_weights.csv"),
                             dtype={"stock_code": str})
    members = set(weights_df["stock_code"])
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

    # ---- 四种预测收益率（均为信息集内无前视）----
    col_v0, col_y, col_d, col_p = [], [], [], []
    for c, g in ev.groupby("code", sort=False):
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
            y_fix = y_dfix = y_pfix = np.nan
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
            col_y.append(y_fix if np.isfinite(y_fix) else v0)
            col_d.append(y_dfix if np.isfinite(y_dfix) else v0)
            col_p.append(y_pfix if np.isfinite(y_pfix) else v0)
    ev["yield_true"] = ev["yield_dec"].fillna(ev["yield_dec"].groupby(ev["code"]).transform(
        lambda s: s.shift(1)))
    ev["yield_true"] = ev["yield_dec"]  # 真值列（NaN 行在覆盖率统计中自然处理）
    ev["y_pred_v0"] = col_v0       # 上年递推（对照）
    ev["y_fix_y"] = col_y          # 固定股息率
    ev["y_fix_d"] = col_d          # 固定分红
    ev["y_fix_p"] = col_p          # 固定派息率
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

# ---------- 主流程 ----------
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

        ev = build_est_ex(load_events(prod))
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
        YP = {k: ev[k].to_numpy(float) for k in ("y_pred_v0", "y_fix_y", "y_fix_d", "y_fix_p")}
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

        recs = []
        for ti, dstr in enumerate(dates):
            dd = dt.date.fromisoformat(dstr)
            tr = float((dd - dser0).days)
            S = closes[ti]
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
                    for k in ("y", "d", "p"):
                        recs.append(dict(date=dstr, product=prod, role=role, contract=sym,
                                         expire=T_day.isoformat(), spot=S, future=Fv,
                                         basis_raw=Fv - S, dpv_pts=0.0, basis_adj=Fv - S,
                                         annualized_rate=np.nan, announced_ratio=np.nan, calibre=k))
                    continue
                true_sel = sel_mask & (E_ANN <= tr)
                pred_sel = sel_mask & ~true_sel
                true_part = np.nansum(w_by_event[true_sel] * YT[true_sel] / 100.0)
                cover = np.nan
                out_row_base = dict(date=dstr, product=prod, role=role, contract=sym,
                                    expire=T_day.isoformat(), spot=S, future=Fv,
                                    basis_raw=Fv - S)
                for k, ycol in (("y", "y_fix_y"), ("d", "y_fix_d"), ("p", "y_fix_p")):
                    pred_part = np.nansum(w_by_event[pred_sel] * YPx[ycol][pred_sel] / 100.0)
                    tot = true_part + pred_part
                    dpv_pts = tot * S
                    cov = true_part / tot if tot > 0 else np.nan
                    years = max((T_day - dd).days, 0) / 365.0
                    B_adj = (Fv - S) + dpv_pts
                    ann_rate = (B_adj / S / years * 100) if years > 0 and np.isfinite(Fv) else np.nan
                    recs.append(dict(**out_row_base, calibre=k, dpv_pts=dpv_pts,
                                     basis_adj=(Fv - S) + dpv_pts, annualized_rate=ann_rate,
                                     announced_ratio=cov))
        panel = pd.DataFrame(recs)
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

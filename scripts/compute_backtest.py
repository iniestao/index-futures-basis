# -*- coding: utf-8 -*-
"""2025 样本外回测：三口径（固定股息率/固定分红/固定派息率）并列对比"""
import os, sys, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import RAW, OUT, PRODUCTS, START, END, third_friday, env_setup
from compute_adjusted_basis import four_contracts_for_day, load_events, build_est_ex, load_weight_timeline, load_snapshot_weights, get_weight_vec
env_setup()
import numpy as np
import pandas as pd

FUT_CSV = os.path.join(RAW, "futures", "cffex_daily_all.csv")
CALIBRES = ("y_fix_y", "y_fix_d", "y_fix_p")
NAMES = {"y_fix_y": "固定股息率", "y_fix_d": "固定分红", "y_fix_p": "固定派息率"}

def main():
    fut = pd.read_csv(FUT_CSV, header=None,
                      names=["date","symbol","open","high","low","close","settle","pre_settle","volume","open_interest"],
                      dtype={"date": str, "symbol": str})
    fut = fut[fut["symbol"] != "__FAIL__"]
    fut["date"] = pd.to_datetime(fut["date"].astype(str), format="%Y%m%d", errors="coerce").dt.strftime("%Y-%m-%d")
    fut["close"] = pd.to_numeric(fut["close"], errors="coerce")
    rows = []
    for prod, cfg in PRODUCTS.items():
        sidx = pd.read_csv(os.path.join(RAW, "index", f"{prod}_{cfg['sina_idx']}.csv"))
        sidx["date"] = pd.to_datetime(sidx["date"]).dt.strftime("%Y-%m-%d")
        sidx = sidx[(sidx["date"] >= "2024-06-01") & (sidx["date"] <= "2025-12-31")].reset_index(drop=True)
        dates = sidx["date"].tolist()
        closes = sidx["close"].to_numpy(float)
        ev = build_est_ex(load_events(prod))
        dser0 = dt.date.fromisoformat(dates[0])
        e_ann = np.array([np.nan if pd.isna(x) else (x.date() - dser0).days for x in pd.to_datetime(ev["ann"])], float)
        e_ex = np.array([np.nan if pd.isna(x) else (x.date() - dser0).days for x in pd.to_datetime(ev["est_ex"])], float)
        y_true = ev["yield_true"].to_numpy(float)
        YP = {k: ev[k].to_numpy(float) for k in CALIBRES}
        order = np.argsort(e_ex, kind="stable")
        E_EX, E_ANN, YT = e_ex[order], e_ann[order], y_true[order]
        YPx = {k: v[order] for k, v in YP.items()}
        timeline = load_weight_timeline(cfg["index"])
        snapshot = load_snapshot_weights(cfg["index"])
        ds = pd.Series(dates)
        month_ends = sorted({d for d in dates if d == max(ds[ds.str[:7] == d[:7]])})
        S_arr = dict(zip(dates, closes))
        for tstr in month_ends:
            tr = float((dt.date.fromisoformat(tstr) - dser0).days)
            S = float(S_arr[tstr])
            dd = dt.date.fromisoformat(tstr)
            w_vec = get_weight_vec(tstr, timeline, snapshot, ev["code"].unique().tolist())
            code_pos = {c: i for i, c in enumerate(ev["code"].unique())}
            w_by_event = w_vec[[code_pos[c] for c in ev["code"]]][order]
            for ym in four_contracts_for_day(dd):
                T_day = third_friday(ym)
                T_rel = (T_day - dser0).days
                if T_rel <= tr:
                    continue
                m_pred = (E_EX > tr) & (E_EX <= T_rel)
                if not m_pred.any():
                    continue
                true_sel = m_pred & (E_ANN <= tr)
                pred_sel = m_pred & ~true_sel
                true_pts = (w_by_event[m_pred] * YT[m_pred] / 100.0).sum() * S
                base = (w_by_event[true_sel] * YT[true_sel] / 100.0).sum() * S
                rec = dict(product=prod, view_date=tstr, contract=f"{prod}{ym}",
                           expire=T_day.isoformat(), dpv_truth_pts=round(true_pts, 2),
                           announced_coverage=round(true_sel.sum() / max(m_pred.sum(), 1), 3) if m_pred.sum() else np.nan)
                for k in CALIBRES:
                    fcst = base + np.nan_to_num((w_by_event[pred_sel] * YPx[k][pred_sel] / 100.0)).sum() * S
                    rec[NAMES[k] + "_pts"] = round(fcst, 2)
                    rec[NAMES[k] + "_err"] = round(fcst - true_pts, 2)
                rows.append(rec)
    bt = pd.DataFrame(rows)
    out_path = os.path.join(OUT, "backtest_2025_calibres.csv")
    bt.to_csv(out_path, index=False, encoding="utf-8-sig")
    print("[OK] backtest rows:", len(bt), "->", out_path)

if __name__ == "__main__":
    main()

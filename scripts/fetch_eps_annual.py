# -*- coding: utf-8 -*-
"""抓取全市场年报 EPS（stock_yjbb_em，2013-2025 年报），供 v2 盈利驱动预测"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import RAW, env_setup
env_setup()
import pandas as pd
import akshare as ak

OUT_PATH = os.path.join(RAW, "eps_annual.csv")

def main():
    rows = []
    for y in range(2013, 2026):
        date = f"{y}1231"
        for attempt in range(3):
            try:
                df = ak.stock_yjbb_em(date=date)
                if df is None or len(df) == 0:
                    break
                df["_code"] = df["股票代码"].astype(str).str.zfill(6)
                sub = df[["_code", "每股收益"]].copy()
                sub.columns = ["stock_code", "eps"]
                sub["report_year"] = y
                rows.append(sub)
                print(f"[OK] {date}: {len(sub)}", flush=True)
                break
            except Exception as e:
                print(f"  retry {date} #{attempt}: {type(e).__name__} {str(e)[:60]}", flush=True)
                time.sleep(3)
        time.sleep(0.5)
    all_df = pd.concat(rows, ignore_index=True)
    all_df["eps"] = pd.to_numeric(all_df["eps"], errors="coerce")
    all_df = all_df[all_df["eps"].notna()]
    all_df.to_csv(OUT_PATH, index=False, encoding="utf-8-sig")
    print(f"[DONE] rows={len(all_df)} -> {OUT_PATH}")

if __name__ == "__main__":
    main()

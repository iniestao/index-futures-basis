# -*- coding: utf-8 -*-
"""抓取四指数成分与权重（中证官网 closeweight.xls），并派生成分股清单"""
import io, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import PROJ, RAW, PRODUCTS, env_setup
env_setup()
import requests
import pandas as pd

BASE = ("https://oss-ch.csindex.com.cn/static/html/csindex/"
        "public/uploads/file/autofile/closeweight/")

def fetch_one(code):
    url = BASE + f"{code}closeweight.xls"
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=40)
            if r.status_code == 200 and len(r.content) > 5000:
                return r.content
        except Exception as e:
            print(f"  retry {code} #{attempt}: {e}")
        time.sleep(3)
    return None

def main():
    universe_rows = []
    for prod, cfg in PRODUCTS.items():
        idx = cfg["index"]
        raw = fetch_one(idx)
        if raw is None:
            print(f"[FAIL] {prod} {idx} closeweight")
            continue
        df = pd.read_excel(io.BytesIO(raw))
        # 列：日期Date / 指数代码 / ... / 成份券代码Constituent Code / 成份券名称Constituent Name / 权重(%)
        wcol = [c for c in df.columns if "权重" in str(c)][0]
        ccol = [c for c in df.columns if "成份券代码" in str(c) or "成分券代码" in str(c)][0]
        ncol = [c for c in df.columns if "成份券名称" in str(c) or "成分券名称" in str(c)][0]
        dcol = [c for c in df.columns if "日期" in str(c)][0]
        df["_code"] = df[ccol].astype(str).str.zfill(6)
        out = pd.DataFrame({
            "product": prod,
            "index_code": idx,
            "weight_date": pd.to_datetime(df[dcol].astype(str), format="%Y%m%d", errors="coerce").dt.date.astype(str),
            "stock_code": df["_code"],
            "stock_name": df[ncol].astype(str),
            "weight_pct": pd.to_numeric(df[wcol], errors="coerce"),
        })
        out = out.dropna(subset=["weight_pct"])
        out.to_csv(os.path.join(RAW, "weights", f"{prod}_weights.csv"), index=False, encoding="utf-8-sig")
        print(f"[OK] {prod} {idx}: rows={len(out)}, weight_date={out['weight_date'].iloc[0]}, sum={out['weight_pct'].sum():.2f}")
        for _, r in out.iterrows():
            universe_rows.append((r["stock_code"], prod))

    uni = pd.DataFrame(universe_rows, columns=["stock_code", "in_products"])
    uni_grouped = uni.groupby("stock_code")["in_products"].apply(lambda s: "|".join(sorted(s))).reset_index()
    uni_grouped.to_csv(os.path.join(RAW, "universe_all.csv"), index=False, encoding="utf-8-sig")
    print(f"[OK] universe: {len(uni_grouped)} unique stocks -> data_raw/universe_all.csv")

if __name__ == "__main__":
    main()

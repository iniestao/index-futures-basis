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

        # ---- 月末权重落库：把中证最新已发布月末数据按历史月度文件格式归档 ----
        # closeweight.xls 为最近已发布月末权重（滞后约1个月）。若无该月末文件则落库，
        # 使月度历史序列随时间自动滚动补全，避免历史权重断档。
        wd = str(out["weight_date"].iloc[0]).replace("-", "")
        arch = os.path.join(RAW, "weights", f"{idx}.SH_{wd}.csv")
        if not os.path.exists(arch):
            arch_df = pd.DataFrame({
                "wind_code": [c + (".SH" if str(c).startswith("6") else ".SZ")
                              for c in out["stock_code"]],
                "i_weight": out["weight_pct"].to_numpy(),
            })
            arch_df.to_csv(arch, index=False, encoding="utf-8-sig")
            print(f"[OK] archived monthly weights -> weights/{idx}.SH_{wd}.csv ({len(arch_df)} rows)")
        else:
            print(f"[skip] weights/{idx}.SH_{wd}.csv already exists")

        for _, r in out.iterrows():
            universe_rows.append((r["stock_code"], prod))

    # 历史月度权重文件（静态）的成分并集也纳入 universe：
    # 历史成分（后被调出/退市前）的分红事件参与历史 DPV 计算，必须有分红数据
    import glob as _glob
    for fp in _glob.glob(os.path.join(RAW, "weights", "*.SH_*.csv")):
        try:
            hw = pd.read_csv(fp)
            if "wind_code" in hw.columns:
                for c in hw["wind_code"].astype(str).str[:6].unique():
                    universe_rows.append((c, "hist"))
        except Exception:
            continue

    uni = pd.DataFrame(universe_rows, columns=["stock_code", "in_products"])
    uni_grouped = uni.groupby("stock_code")["in_products"].apply(lambda s: "|".join(sorted(s))).reset_index()
    uni_grouped.to_csv(os.path.join(RAW, "universe_all.csv"), index=False, encoding="utf-8-sig")
    print(f"[OK] universe: {len(uni_grouped)} unique stocks -> data_raw/universe_all.csv")

if __name__ == "__main__":
    main()

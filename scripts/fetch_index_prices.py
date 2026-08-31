# -*- coding: utf-8 -*-
"""四指数日线（新浪源），2015-01-01 起"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import RAW, PRODUCTS, START, env_setup
env_setup()
import pandas as pd
import akshare as ak

def main():
    for prod, cfg in PRODUCTS.items():
        out_path = os.path.join(RAW, "index", f"{prod}_{cfg['sina_idx']}.csv")
        # 每次全量重抓（单指数十年日线仅数百KB，覆盖式写入保证增量更新）
        for attempt in range(3):
            try:
                df = ak.stock_zh_index_daily(symbol=cfg["sina_idx"])
                df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
                df = df[df["date"] >= "2014-12-01"][["date", "open", "high", "low", "close"]]
                df.to_csv(out_path, index=False, encoding="utf-8-sig")
                print(f"[OK] {prod} {cfg['sina_idx']}: rows={len(df)}, {df['date'].iloc[0]}~{df['date'].iloc[-1]}")
                break
            except Exception as e:
                print(f"  retry {prod} #{attempt}: {type(e).__name__} {str(e)[:80]}")
                time.sleep(5)
        time.sleep(1)

if __name__ == "__main__":
    main()

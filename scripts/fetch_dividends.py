# -*- coding: utf-8 -*-
"""逐股抓取分红明细（东财F10 stock_fhps_detail_em），断点续传，多线程"""
import io, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import RAW, env_setup
env_setup()
import pandas as pd
import akshare as ak
from concurrent.futures import ThreadPoolExecutor, as_completed

DIV_DIR = os.path.join(RAW, "dividends")
FAIL_LOG = os.path.join(DIV_DIR, "_failed.txt")

def fetch_one(code):
    out_path = os.path.join(DIV_DIR, f"{code}.csv")
    stale = (not os.path.exists(out_path)) or (time.time() - os.path.getmtime(out_path) > 20*3600)
    if not stale and os.path.getsize(out_path) > 50:
        return code, "skip"
    last_err = ""
    for attempt in range(3):
        try:
            df = ak.stock_fhps_detail_em(symbol=code)
            if df is None or len(df) == 0:
                df = pd.DataFrame()
            df.to_csv(out_path, index=False, encoding="utf-8-sig")
            return code, f"ok{len(df)}"
        except Exception as e:
            last_err = f"{type(e).__name__}:{str(e)[:80]}"
            time.sleep(2 + attempt * 3)
    return code, f"FAIL {last_err}"

def main():
    uni = pd.read_csv(os.path.join(RAW, "universe_all.csv"), dtype={"stock_code": str})
    codes = uni["stock_code"].str.zfill(6).tolist()
    print(f"total {len(codes)} stocks")
    fails = []
    done = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(fetch_one, c): c for c in codes}
        for fut in as_completed(futures):
            code, status = fut.result()
            done += 1
            if status.startswith("FAIL"):
                fails.append(code)
                with open(FAIL_LOG, "a", encoding="utf-8") as f:
                    f.write(f"{code}\t{status}\n")
            if done % 100 == 0:
                print(f"[{done}/{len(codes)}] fails={len(fails)}", flush=True)
    print(f"DONE. total={done} fail={len(fails)}")

if __name__ == "__main__":
    main()

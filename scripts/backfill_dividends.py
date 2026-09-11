# -*- coding: utf-8 -*-
"""分红文件补抓：单线程限速，只抓缺失文件（universe_all 中没有对应 dividends/{code}.csv 的股票）
用于修复"历史成分不在 universe 导致分红缺失"问题后的数据补齐。
已退市股票东财接口返回空 -> 写入空 CSV（避免反复重抓），空文件不影响计算。
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import RAW, env_setup
env_setup()
# 如本地网络需代理，运行前自行 export HTTPS_PROXY/HTTP_PROXY（勿在代码中硬编码）
import pandas as pd
import akshare as ak

DIV_DIR = os.path.join(RAW, "dividends")

def main():
    uni = pd.read_csv(os.path.join(RAW, "universe_all.csv"), dtype={"stock_code": str})
    codes = [c for c in uni["stock_code"].str.zfill(6).tolist()
             if not os.path.exists(os.path.join(DIV_DIR, f"{c}.csv"))]
    print(f"缺失分红文件: {len(codes)} 只", flush=True)
    ok = empty = fail = 0
    for n, code in enumerate(codes, 1):
        out_path = os.path.join(DIV_DIR, f"{code}.csv")
        for attempt in range(5):
            try:
                df = ak.stock_fhps_detail_em(symbol=code)
                if df is None or len(df) == 0:
                    pd.DataFrame().to_csv(out_path, index=False, encoding="utf-8-sig")
                    empty += 1
                else:
                    df.to_csv(out_path, index=False, encoding="utf-8-sig")
                    ok += 1
                break
            except Exception as e:
                if attempt == 4:
                    fail += 1
                    with open(os.path.join(DIV_DIR, "_backfill_failed.txt"), "a", encoding="utf-8") as f:
                        f.write(f"{code}\t{type(e).__name__}:{str(e)[:60]}\n")
                else:
                    # 代理时通时断：失败后等待更久再试（探测网络窗口）
                    time.sleep(10 + attempt * 15)
        time.sleep(0.6)
        if n % 50 == 0:
            print(f"[{n}/{len(codes)}] ok={ok} empty={empty} fail={fail}", flush=True)
    print(f"DONE ok={ok} empty={empty} fail={fail}", flush=True)

if __name__ == "__main__":
    main()

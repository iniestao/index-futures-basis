# -*- coding: utf-8 -*-
"""逐股抓取分红明细（东财F10 stock_fhps_detail_em），断点续传，多线程。

刷新策略（内容驱动轮转，不依赖文件 mtime —— git checkout 会重置 mtime，
导致 CI 环境下所有文件被误判为"新鲜"而永不更新）：
  - 文件缺失 / 过小 -> 直接抓
  - 否则读 CSV 内"预案公告日"与"最新公告日期"的最大值作为最后活跃时间：
      * 45 天内有公告活动 -> 每 2 天轮转重抓一次（活跃公司，捕捉新预案/实施）
      * 超过 45 天无活动或无日期信息 -> 每 10 天轮转重抓一次（兜底捕捉"突然恢复分红"的公司）
  - 轮转桶：crc32(code) % freq == 当日 dayofyear % freq 时才抓，保证每日增量可控且确定
"""
import io, os, sys, time, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import RAW, env_setup
env_setup()
import pandas as pd
import akshare as ak
from concurrent.futures import ThreadPoolExecutor, as_completed

DIV_DIR = os.path.join(RAW, "dividends")
FAIL_LOG = os.path.join(DIV_DIR, "_failed.txt")

def need_refresh(code, out_path):
    """返回 (是否需要重抓, 轮转频率)"""
    if (not os.path.exists(out_path)) or os.path.getsize(out_path) <= 50:
        return True, 0
    try:
        df = pd.read_csv(out_path, encoding="utf-8-sig")
    except Exception:
        return True, 0
    last = pd.NaT
    for col in ("预案公告日", "最新公告日期"):
        if col in df.columns:
            s = pd.to_datetime(df[col], errors="coerce").max()
            if pd.notna(s) and (pd.isna(last) or s > last):
                last = s
    today = pd.Timestamp.now().normalize()
    if pd.isna(last):
        freq = 10
    elif (today - last).days <= 45:
        freq = 2   # 近期有公告活动，高频轮转
    else:
        freq = 10  # 长期无活动，低频兜底轮转
    bucket = zlib.crc32(code.encode()) % freq
    return bucket == (today.dayofyear % freq), freq

def fetch_one(code):
    out_path = os.path.join(DIV_DIR, f"{code}.csv")
    refresh, freq = need_refresh(code, out_path)
    if not refresh:
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
    # 预统计当日计划抓取量
    plan = [c for c in codes if need_refresh(c, os.path.join(DIV_DIR, f"{c}.csv"))[0]]
    print(f"total {len(codes)} stocks, to fetch today: {len(plan)}")
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

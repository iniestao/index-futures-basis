# -*- coding: utf-8 -*-
"""
季报 EPS 抓取（东财业绩报表 RPT_LICO_FN_CPD，含披露日）

用途：为「固定派息率 + 季报外推 EPS」口径提供输入，替代只锚定上年年报 EPS 的做法。
产出：data_raw/eps_quarterly.csv
      stock_code, report_date(报告期), eps_cum(报告期累计每股收益), notice_date(最新公告日期)

增量策略：历史季度只补缺失；最近 2 个季度每次都重抓（公告日期/数据会随披露滚动更新）。
文件规模控制：只保留 universe_all.csv 内的股票（其余股票对 DPV 无贡献）。
"""
import os, sys, time, datetime as dt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import RAW, env_setup
env_setup()
import pandas as pd

OUT_CSV = os.path.join(RAW, "eps_quarterly.csv")
COLUMNS = ["stock_code", "report_date", "eps_cum", "notice_date"]
FIRST_QUARTER = (2015, 3)          # 首个报告期 2015-03-31
REFRESH_LAST_N = 2                 # 最近 N 个报告期每次重抓


def quarter_ends(first=(2015, 3)):
    """生成从 first 到「最近一个已过报告期」的季度末列表"""
    today = dt.date.today()
    out = []
    y, m = first
    while (y, m) <= (today.year, today.month):
        q_end = dt.date(y, m, [31, 30, 30, 31][m // 3 - 1])
        if q_end <= today:
            out.append(q_end)
        m += 3
        if m > 12:
            y, m = y + 1, 3
    return out


def load_universe():
    fp = os.path.join(RAW, "universe_all.csv")
    if not os.path.exists(fp):
        return None
    df = pd.read_csv(fp, dtype={"stock_code": str})
    return set(df["stock_code"])


def fetch_quarter(q_end, uni):
    import akshare as ak
    ymd = q_end.strftime("%Y%m%d")
    for attempt in range(3):
        try:
            df = ak.stock_yjbb_em(date=ymd)
            if df is None or len(df) == 0:
                return pd.DataFrame(columns=COLUMNS)
            df = df.rename(columns={"股票代码": "stock_code", "每股收益": "eps_cum",
                                    "最新公告日期": "notice_date"})
            df = df[["stock_code", "eps_cum", "notice_date"]].copy()
            df["stock_code"] = df["stock_code"].astype(str).str.zfill(6)
            df["report_date"] = q_end.isoformat()
            df["eps_cum"] = pd.to_numeric(df["eps_cum"], errors="coerce")
            df["notice_date"] = pd.to_datetime(df["notice_date"], errors="coerce").dt.strftime("%Y-%m-%d")
            df = df[df["eps_cum"].notna()]
            if uni is not None:
                df = df[df["stock_code"].isin(uni)]
            return df[COLUMNS]
        except Exception as e:
            if attempt == 2:
                print(f"[WARN] {ymd} failed: {type(e).__name__}: {str(e)[:80]}", flush=True)
                return None
            time.sleep(4)


def main():
    uni = load_universe()
    if uni is not None:
        print(f"universe filter: {len(uni)} codes", flush=True)

    cols = quarter_ends()
    old = pd.DataFrame(columns=COLUMNS)
    if os.path.exists(OUT_CSV):
        try:
            old = pd.read_csv(OUT_CSV, dtype={"stock_code": str})
            old["stock_code"] = old["stock_code"].astype(str).str.zfill(6)
        except Exception:
            old = pd.DataFrame(columns=COLUMNS)
    have = set(old["report_date"].unique()) if len(old) else set()
    refresh = {q.isoformat() for q in cols[-REFRESH_LAST_N:]}
    todo = [q for q in cols if q.isoformat() not in have or q.isoformat() in refresh]
    print(f"季度总数={len(cols)} 待抓={len(todo)} (刷新最近{REFRESH_LAST_N}期 + 缺失期)", flush=True)

    frames = [old]
    ok = fail = 0
    for q in todo:
        df = fetch_quarter(q, uni)
        if df is None:
            fail += 1
            continue
        # 该报告期已有数据 → 用新结果替换（公告日期会滚动更新）
        frames = [f for f in frames if not (len(f) and (f["report_date"] == q.isoformat()).any())]
        frames.append(df)
        ok += 1
        print(f"  [OK] {q} rows={len(df)}", flush=True)
        time.sleep(1.0)

    if ok == 0:
        print("no new quarter fetched; file unchanged")
    merged = pd.concat(frames, ignore_index=True)
    merged = merged.drop_duplicates(subset=["stock_code", "report_date"], keep="last")
    merged = merged.sort_values(["stock_code", "report_date"]).reset_index(drop=True)
    merged.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print(f"[DONE] eps_quarterly rows={len(merged)} quarters={merged['report_date'].nunique()} "
          f"codes={merged['stock_code'].nunique()} (fetched={ok}, failed={fail})", flush=True)


if __name__ == "__main__":
    main()

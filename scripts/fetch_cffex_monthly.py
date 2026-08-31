# -*- coding: utf-8 -*-
"""按月中金所 zip 直连并发抓取（每 zip 含整月全部交易日），合并写回缓存"""
import io, os, sys, zipfile, time, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import RAW, START, END, env_setup
env_setup()
import requests
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed

FUT_DIR = os.path.join(RAW, "futures")
CACHE_CSV = os.path.join(RAW, "futures", "cffex_daily_all.csv")
CACHE_COLS = ["date", "symbol", "open", "high", "low", "close",
              "settle", "pre_settle", "volume", "open_interest"]
COLS = ["合约代码", "开盘价", "最高价", "最低价", "收盘价", "结算价", "前结算价", "成交量", "持仓量"]
URL = "http://www.cffex.com.cn/sj/historysj/{ym}/zip/{ym}.zip"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}

def months_needed():
    idx = pd.read_csv(os.path.join(RAW, "index", "IF_sh000300.csv"))
    days = sorted(d.replace("-", "") for d in idx[(idx["date"] >= START) & (idx["date"] <= END)]["date"])
    return sorted({d[:6] for d in days}), set(days)

def fetch_month(ym):
    try:
        r = requests.get(URL.format(ym=ym), headers=HEADERS, timeout=60)
        if r.status_code != 200 or len(r.content) < 2000:
            return ym, None, f"http {r.status_code}"
        rows = []
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            for name in zf.namelist():
                base = name.split("/")[-1]
                if not base.endswith(".csv"):
                    continue
                day = base.split("_")[0]
                if len(day) != 8 or not day.isdigit():
                    continue
                data = zf.read(name).decode("gb2312", errors="ignore")
                df = pd.read_csv(io.StringIO(data))
                if "合约代码" not in df.columns:
                    continue
                df = df[~df["合约代码"].astype(str).str.contains("小计|合计|IO|MO|HO|示例", na=False)]
                # 兼容新老两套列名体系
                ALIAS = {
                    "open": ["开盘价", "今开盘"],
                    "high": ["最高价"],
                    "low": ["最低价"],
                    "close": ["收盘价", "今收盘"],
                    "settle": ["结算价", "今结算"],
                    "pre_settle": ["前结算", "昨结算"],
                    "volume": ["成交量"],
                    "open_interest": ["持仓量"],
                }
                colmap = {}
                for target, aliases in ALIAS.items():
                    for c in df.columns:
                        cs = str(c).strip()
                        if cs in aliases and cs in df.columns:
                            colmap[c] = target
                            break
                sub = df[list(colmap.keys())].rename(columns=colmap)
                for c in ("open","high","low","close","settle","pre_settle","volume","open_interest"):
                    if c not in sub.columns:
                        sub[c] = None
                code_stripped = sub["合约代码"] if "合约代码" in sub.columns else df["合约代码"]
                code_stripped = code_stripped.astype(str).str.strip()
                keep_varieties = code_stripped.str.match(r"^(IF|IH|IC|IM)\d{4}$")
                sub = sub[keep_varieties.values].copy()
                sub.insert(0, "symbol", code_stripped[keep_varieties.values].values)
                sub.insert(0, "date", day)
                rows.append(sub[["date","symbol","open","high","low","close","settle","pre_settle","volume","open_interest"]])
        if rows:
            out = pd.concat(rows, ignore_index=True)
            return ym, out, f"{len(out)} rows"
        return ym, None, "empty"
    except Exception as e:
        return ym, None, f"EXC {type(e).__name__}:{str(e)[:60]}"

def load_cache():
    """读缓存：兼容带表头/带BOM/无表头三种历史格式，只留合法数据行"""
    if not os.path.exists(CACHE_CSV):
        return pd.DataFrame(columns=CACHE_COLS)
    df = pd.read_csv(CACHE_CSV, header=None, names=CACHE_COLS,
                     dtype={"date": str, "symbol": str}, encoding="utf-8-sig", skiprows=1)
    df = df[df["date"].astype(str).str.match(r"^\d{8}$", na=False)]
    df = df[df["symbol"].astype(str).str.match(r"^(IF|IH|IC|IM)\d{4}$", na=False)]
    return df


def main():
    yms, all_days = months_needed()
    # 断点：哪些天已在缓存
    old_df = load_cache()
    have_days = set(old_df["date"].unique())
    missing_months = [ym for ym in yms if any(d.startswith(ym) and d not in have_days for d in all_days)]
    # 当月与上一月永远强制重下（zip 内容随交易日滚动更新）
    today = dt.date.today()
    cur_ym = today.strftime("%Y%m")
    prev_ym = (today.replace(day=1) - dt.timedelta(days=1)).strftime("%Y%m")
    missing_months = sorted(set(missing_months) | {cur_ym, prev_ym})
    print(f"months total={len(yms)}, missing+forced={len(missing_months)}")

    new_frames = []
    fails = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(fetch_month, ym): ym for ym in missing_months}
        done_n = 0
        for fut in as_completed(futures):
            ym, df, msg = fut.result()
            done_n += 1
            if df is not None:
                new_frames.append(df)
            else:
                fails.append((ym, msg))
            if done_n % 20 == 0:
                print(f"[{done_n}/{len(missing_months)}]", flush=True)
    print(f"downloaded={len(new_frames)} fail={len(fails)}: {fails[:8]}")

    old_df = load_cache()

    merged_cols = CACHE_COLS
    full = pd.concat(new_frames, ignore_index=True) if new_frames else pd.DataFrame(columns=merged_cols)
    # 统一列名到英文（老缓存已英文）
    full_en = full.rename(columns=dict(zip(["开盘价","最高价","最低价","收盘价","结算价","前结算价","成交量","持仓量"],
                                           ["open","high","low","close","settle","pre_settle","volume","open_interest"])))
    both = pd.concat([old_df, full_en], ignore_index=True)
    both = both.drop_duplicates(subset=["date", "symbol"], keep="last")
    both = both.sort_values(["date", "symbol"])
    both.to_csv(CACHE_CSV, index=False, encoding="utf-8")
    print(f"[OK] total rows={len(both)}, dates={both['date'].nunique()}, span={both['date'].min()}~{both['date'].max()}")

if __name__ == "__main__":
    main()

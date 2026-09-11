# -*- coding: utf-8 -*-
"""
个股日线抓取（不复权收盘价）—— 服务于「流通市值近似」的每日指数权重。

数据源：新浪财经日 K（CN_MarketData.getKLineData），单次可返回最近约 3000 个交易日（≈12 年）。
        该接口为**不复权**价：送转除权日存在价格跳空，由引擎侧用分红表
        「送转股份-送转总比例」做联动修正（见 scripts/weight_daily.py），此处不做复权。
存储：data_raw/stock_prices/close_YYYY.csv —— (日期行 × 代码列) 宽表，值=收盘价。
      宽表按年切分，每日只重写当年文件且只追加 1 行，git 增量约 30KB/日，仓库体积增长可控。
抓取策略：
      - 无任何存储记录的代码（新纳入 universe / 历史成分）→ 全量 3000 根
      - 当前成分（四指数最新月末文件 ∪ 快照）→ 增量 45 根（自愈最近缺口）
      - --sweep 全量 universe 45 根（周期性兜底，捕捉重新纳入的成分）
"""
import os, sys, json, time, argparse, threading
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import RAW, PRODUCTS, env_setup

env_setup()
import pandas as pd
import requests
import glob

PRICE_DIR = os.path.join(RAW, "stock_prices")
W_DIR = os.path.join(RAW, "weights")
FULL_BARS = 3000        # 全量回溯（覆盖 2014 年至今）
TAIL_BARS = 45          # 增量窗口（约两个月交易日）
WORKERS = int(os.environ.get("PRICE_WORKERS", "8"))
DELAY = float(os.environ.get("PRICE_DELAY", "0"))   # 每请求额外等待（本地代理限流时调大）
FLUSH_EVERY = 400      # 每抓满 N 只即落盘（中断/超时不丢进度）
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Referer": "https://finance.sina.com.cn",
}
SINA_URL = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen={n}")

_print_lock = threading.Lock()


def sina_symbol(code):
    code = str(code).zfill(6)
    if code[0] in ("6", "9"):
        return "sh" + code
    if code[0] in ("0", "2", "3"):
        return "sz" + code
    return None  # 北交所等非本指数成分，跳过


def year_path(y):
    return os.path.join(PRICE_DIR, f"close_{y}.csv")


def load_year(y):
    fp = year_path(y)
    if not os.path.exists(fp):
        return pd.DataFrame()
    df = pd.read_csv(fp, index_col=0, dtype={0: str})
    df.index = df.index.astype(str)
    return df


def stored_codes():
    """所有年份面板里已有的代码（只读表头，便宜）"""
    have = set()
    if not os.path.isdir(PRICE_DIR):
        return have
    for fp in sorted(glob.glob(os.path.join(PRICE_DIR, "close_*.csv"))):
        try:
            df = pd.read_csv(fp, nrows=0)
            have |= {c for c in df.columns if c != df.columns[0]}
        except Exception:
            continue
    return have


def current_constituents():
    """当前成分：四指数最新月末权重文件 ∪ 当前快照"""
    codes = set()
    for prod, cfg in PRODUCTS.items():
        files = sorted(glob.glob(os.path.join(W_DIR, f"{cfg['index']}.SH_*.csv")))
        if files:
            try:
                df = pd.read_csv(files[-1])
                if "wind_code" in df.columns:
                    codes |= set(df["wind_code"].astype(str).str[:6])
            except Exception:
                pass
        snap = os.path.join(W_DIR, f"{prod}_weights.csv")
        if os.path.exists(snap):
            try:
                df = pd.read_csv(snap, dtype={"stock_code": str})
                codes |= set(df["stock_code"])
            except Exception:
                pass
    return codes


def fetch_tencent(sess, code, start_date="2015-01-01", batches=5):
    """
    腾讯日 K 兜底（新浪不可用时）。腾讯单次最多 800 根，按日期区间往前翻页。
    返回 [(day, close)]，不复权（param 末位留空）。
    """
    sym = sina_symbol(code)
    if sym is None:
        return []
    import datetime as dt
    out = {}
    end = dt.date.today()
    for _ in range(batches):
        url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param="
               f"{sym},day,{start_date},{end.isoformat()},800,")
        try:
            r = sess.get(url, headers=UA, timeout=20)
            node = (r.json().get("data") or {}).get(sym) or {}
        except Exception:
            break
        rows = node.get("day") or node.get("qfqday") or []
        if not rows:
            break
        for row in rows:
            try:
                out[str(row[0])[:10]] = round(float(row[2]), 2)   # [日期, 开, 收, 高, 低, 量]
            except Exception:
                continue
        first = str(rows[0][0])[:10]
        if first <= start_date:
            break
        end = dt.date.fromisoformat(first) - dt.timedelta(days=1)
        if end.isoformat() <= start_date:
            break
    return sorted(out.items())


def fetch_one(sess, code, n):
    """返回 (code, [(day, close)]) ；失败返回 (code, None)。新浪为主、腾讯兜底"""
    sym = sina_symbol(code)
    if sym is None:
        return code, []
    url = SINA_URL.format(sym=sym, n=n)
    for att in range(3):
        try:
            if DELAY:
                time.sleep(DELAY)
            r = sess.get(url, headers=UA, timeout=20)
            txt = (r.text or "").strip()
            if not txt or txt in ("null", "[]", "null;"):
                break
            data = json.loads(txt)
            out = []
            for d in data:
                c = d.get("close")
                if c in (None, "", "0", "0.000"):
                    continue
                try:
                    out.append((str(d["day"])[:10], round(float(c), 2)))
                except Exception:
                    continue
            if out:
                return code, out
            break
        except Exception:
            time.sleep(1.0 + att)
    # 兜底：腾讯
    rows = fetch_tencent(sess, code)
    if rows:
        return code, rows
    # 两个源都无数据（退市/新股未上市/源不可达）：返回空 —— 该代码不会落库，
    # 下次运行仍在「无存储记录」集合中被重试，天然自愈；整体覆盖情况由质量护栏报告
    return code, []


def upsert(new_by_code):
    """把 {code: [(day, close)]} 合并进按年宽表（按年分组后读改写）"""
    per_year = {}   # year -> {code: Series(day -> close)}
    for code, rows in new_by_code.items():
        if not rows:
            continue
        s = pd.Series(dict(rows), dtype=float)
        s = s[~s.index.duplicated(keep="last")].sort_index()
        for y, sub in s.groupby(s.index.str[:4], sort=True):
            per_year.setdefault(int(y), {})[code] = sub
    os.makedirs(PRICE_DIR, exist_ok=True)
    stat = {}
    for y in sorted(per_year):
        old = load_year(y)
        new = pd.DataFrame(per_year[y])
        new.index.name = "date"
        if len(old):
            merged = new.combine_first(old)
        else:
            merged = new
        merged = merged.sort_index()
        merged.to_csv(year_path(y), encoding="utf-8")
        stat[y] = (len(merged), merged.shape[1])
    return stat


def run(targets, tag):
    """targets: list[(code, bars)]；分批落盘，中断也不丢已抓数据"""
    if not targets:
        print(f"[{tag}] nothing to fetch", flush=True)
        return 0, 0
    sess = requests.Session()
    ok_rows, failed = {}, []
    done = 0
    t0 = time.time()
    stat_all = {}
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def flush():
        nonlocal ok_rows
        if not ok_rows:
            return
        stat = upsert(ok_rows)
        for y, v in stat.items():
            stat_all[y] = v
        ok_rows = {}

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(fetch_one, sess, c, n): c for c, n in targets}
        for fu in as_completed(futs):
            code, rows = fu.result()
            done += 1
            if rows is None:
                failed.append(code)
            elif rows:
                ok_rows[code] = rows
            if done % FLUSH_EVERY == 0:
                flush()
                with _print_lock:
                    print(f"[{tag}] {done}/{len(targets)} fail={len(failed)} {time.time() - t0:.0f}s", flush=True)
    flush()
    print(f"[{tag}] fail={len(failed)}/{len(targets)} | " +
          ", ".join(f"{y}:{r}x{c}" for y, (r, c) in sorted(stat_all.items())), flush=True)
    if failed:
        fp = os.path.join(PRICE_DIR, "_failed.txt")
        with open(fp, "a", encoding="utf-8") as f:
            f.write("\n".join(f"{c}\t{tag}\t{time.strftime('%Y-%m-%d %H:%M')}" for c in failed) + "\n")
        print(f"[{tag}] failed codes -> {fp}", flush=True)
    return len(ok_rows), len(failed)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep", action="store_true", help="全量 universe 增量刷新（周期性兜底）")
    ap.add_argument("--full", action="store_true", help="强制全量回溯所有 universe 代码")
    ap.add_argument("--limit", type=int, default=0, help="只抓前 N 个（调试用）")
    ap.add_argument("--codes", type=str, default="", help="逗号分隔的指定代码（调试用）")
    args = ap.parse_args()

    uni = pd.read_csv(os.path.join(RAW, "universe_all.csv"), dtype={"stock_code": str})
    all_codes = [c for c in uni["stock_code"].tolist() if sina_symbol(c)]
    have = stored_codes()
    cur = current_constituents()

    if args.codes:
        targets = [(c.strip(), FULL_BARS) for c in args.codes.split(",") if c.strip()]
    else:
        full = [c for c in all_codes if c not in have]
        daily = [c for c in sorted(cur) if c in have and sina_symbol(c)]
        if args.sweep:
            extra = [c for c in all_codes if c in have and c not in set(daily)]
            daily = daily + extra
        if args.full:
            full = all_codes
        targets = [(c, FULL_BARS) for c in full] + [(c, TAIL_BARS) for c in daily]
    if args.limit:
        targets = targets[: args.limit]

    print(f"universe={len(all_codes)} stored={len(have)} current={len(cur)} "
          f"-> full={sum(1 for _, n in targets if n == FULL_BARS)} "
          f"tail={sum(1 for _, n in targets if n == TAIL_BARS)}", flush=True)
    run(targets, "prices")
    print("DONE", flush=True)


if __name__ == "__main__":
    main()

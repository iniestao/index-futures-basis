# -*- coding: utf-8 -*-
"""
个股日线抓取（不复权收盘价）—— 服务于「流通市值近似」的每日指数权重。

数据源：新浪财经日 K（CN_MarketData.getKLineData），单次可返回最近约 3000 个交易日（≈12 年）。
        该接口为**不复权**价：送转除权日存在价格跳空，由引擎侧用分红表
        「送转股份-送转总比例」做联动修正（见 scripts/weight_daily.py），此处不做复权。
存储：data_raw/stock_prices/close_YYYY.csv —— (日期行 × 代码列) 宽表，值=收盘价。
      宽表按年切分，每日只重写当年文件且只追加 1 行，git 增量约 30KB/日，仓库体积增长可控。
抓取策略（队列按「当日价值 / 抓取成本」分四层，详见 main() 内注释）：
      - 当前成分 × 当日缺价        → 增量 45 根（当日价格是看板硬需求，最先做完）
      - 当前成分 × 无存储记录      → 全量 3000 根（决定每日权重的覆盖率）
      - 当前成分 × 当日已有价      → 增量 45 根（自愈该股近期其他缺口）
      - 历史成分 × 无存储记录      → 全量 3000 根（只影响历史精度，最后做）
      - --sweep 追加全量 universe 45 根（周期性兜底，捕捉重新纳入的成分）
      单轮运行必然被限流/预算截断，故需两层防饥饿：
      · 组内轮转 —— 「当日缺价」组带游标（output/price_cursor.json），本轮消费多少只
        偏移就前进多少，下轮从断点接着做；否则每次都从固定队头开始，被截断的永远是
        同一批队尾代码（2026-09-14 实测：前 801 只与实际有价集合重合 99.9%）。
      · 组间配额 —— 该组单轮最多做 GAP_MAX 只（默认 600）。它盘中可达 1500+ 只，
        不封顶就会吃光整轮预算，让「当前成分缺记录」组（决定 IC/IM 权重覆盖率）
        永远轮不到（2026-09-15 实测连续三轮权重覆盖率纹丝不动）。
      · 另设全进程限速 RATE（默认 1 req/s）：新浪按突发窗口限流，突发速率只会换来
        最长 300s 的全局冷却（吞吐归零），不如压低速率、靠长时间稳定跑量。
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
WORKERS = int(os.environ.get("PRICE_WORKERS", "4"))   # 并发过高会触发新浪限流（曾整段失败）
DELAY = float(os.environ.get("PRICE_DELAY", "0.1"))   # 每请求额外等待，压住突发速率
FLUSH_EVERY = 400      # 每抓满 N 只即落盘（中断/超时不丢进度）
# 抓取阶段的总时间预算（秒；0=不限）。这是**硬保护**：必须在 job 超时前主动收尾，
# 否则 GitHub 会在 timeout-minutes 处直接 cancel 整个 job，连已抓数据都提交不了
# （2026-09-14 即如此：退避遇上持续限流 → 跑满 90 分钟被取消，远端无任何产出）。
# 预算用尽时停止取新任务、落盘已完成部分，剩余代码下次运行继续（无落库记录者天然进队列）。
BUDGET = float(os.environ.get("PRICE_BUDGET", "3600"))

# ---- 限流自适应退避 ----
# 实测：新浪按「突发窗口」限流，被限时返回 456 或非 JSON 响应（也可能直接返回空）。
# 若不退避而是继续猛打，限流窗口会持续吞掉整段队列（2026-09-13 云端运行即如此：
# 000 段与 600 段成功，中间的 001/002/003/300/301 整段失败，共丢 1624 只）。
# 策略：连续空/被限 → 全局冷却（指数增长，成功即重置），而不是各线程独立重试。
LIMIT_STATUS = (403, 429, 456, 500, 502, 503, 504)
COOLDOWN_BASE = float(os.environ.get("PRICE_COOLDOWN", "15"))   # 首次冷却秒数
COOLDOWN_MAX = float(os.environ.get("PRICE_COOLDOWN_MAX", "300"))
# 收尾判据：**看"有没有产出"，不看"冷却占了多少时间"**。
# 旧判据是 cooled ≥ ratio × elapsed，在持续限流下会在约 50 分钟就中止 ——
# 2026-09-15 实测三次手动运行各 52/52/57 分钟（时间预算 4 小时，只用了 22%），
# 单轮产出仅 583/572/440 条。而每次命中限流后的自动重试其实都能成功，只是慢；
# 把剩余 3 小时全扔了。改为「连续 STALL_LIMIT 秒没有任何成功抓取」才收尾：
# 既能拦住真螺旋（确实零产出），又让限流下仍在推进的长任务跑满预算。
# （public 仓库 Actions 分钟数免费，不必为省额度提前收尾。）
STALL_LIMIT = float(os.environ.get("PRICE_STALL_LIMIT", "1800"))
# 全进程最大请求速率（req/s，0=不限）。新浪按突发窗口限流，4 线程 + 0.1s 间隔的
# 突发速率会持续命中限流，而每次命中触发最长 300s 的全局冷却（期间吞吐为 0）。
# 与其"快一阵、停五分钟"，不如把速率压到阈值以下、靠长时间稳定跑量：
# 1 req/s × 4 小时 = 14400 次请求，远超全量一轮所需（约 2600）。
RATE = float(os.environ.get("PRICE_RATE", "1.0"))
# 「当日缺价」组单轮最多消费多少只 —— 防止它（盘中可达 1500+ 只）吃光整轮预算，
# 让后面的「当前成分缺记录」组（决定 IC/IM 权重覆盖率）永远轮不到。
GAP_MAX = int(os.environ.get("PRICE_GAP_MAX", "600"))
# 参照日的最小填充比例：只有当某日的非空数 ≥ 该比例 × 近期中位数，才承认它是
# "已有数据的交易日"。否则个别股票先拿到的新日期会把参照日顶到**数据源其实还没有**
# 的那一天，使「当日缺价」组膨胀到 1500+ 只、整轮预算被徒劳请求吃光
# （2026-09-15 盘中运行实测：参照日跳到 9-15 后，连续两轮约 1000 次请求几乎零效果）。
MIN_DAY_FILL = float(os.environ.get("PRICE_MIN_DAY_FILL", "0.5"))
EMPTY_STREAK_TRIGGER = int(os.environ.get("PRICE_EMPTY_STREAK", "20"))  # 连续空到这个数即判定为被限流
ATTEMPTS = int(os.environ.get("PRICE_ATTEMPTS", "4"))
# 新浪被限流时每个失败代码都会走腾讯兜底，请求量会放大 2~3 倍、把限流拖得更久。
# 给兜底设每轮次数上限，超限即视为本轮拿不到。
TENCENT_MAX = int(os.environ.get("PRICE_TENCENT_MAX", "200"))

# 队列游标（入库，随数据一起提交）。记录「当日增量」队列上次的起点位置。
# 为什么需要它：单轮运行必然会在限流/预算处被截断，而队列若每次都从固定队头开始，
# 被截断的永远是同一批队尾代码 —— 2026-09-14 实测：daily 队列 1564 只，
# 「升序前 801 只」与实际拿到当日价的代码重合 800/801（99.9%），
# 队尾 763 只（几乎全部 601/603/605/688 段）连续三轮运行零进展，
# 且排在后面的回补队列一次都没轮到（价格面板覆盖连续三轮卡在 2398 只不动）。
CURSOR_FP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "output", "price_cursor.json")

_throttle = {"until": 0.0, "strikes": 0, "empty_streak": 0, "cooled": 0.0,
             "tencent": 0, "last_ok": 0.0}
_deadline = 0.0        # run() 开始时设定为 t0 + BUDGET
_run_t0 = 0.0          # 本轮开始时刻
_rate_lock = threading.Lock()
_rate_next = [0.0]     # 下一个允许发起请求的时刻（全进程限速用）
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Referer": "https://finance.sina.com.cn",
}
SINA_URL = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen={n}")

_print_lock = threading.Lock()


def _wait_gate():
    """在全局冷却结束前挂起本线程（限流时所有线程一起让路）；已超预算则立即返回"""
    while True:
        if _over_budget():
            return
        remain = _throttle["until"] - time.time()
        if remain <= 0:
            return
        time.sleep(min(5.0, remain))


def _rate_gate():
    """全进程令牌间隔：把发起请求的速率压到 RATE req/s 以内（0=不限）"""
    if RATE <= 0:
        return
    while True:
        with _rate_lock:
            now = time.time()
            if now >= _rate_next[0]:
                _rate_next[0] = now + 1.0 / RATE
                return
            wait = _rate_next[0] - now
        time.sleep(min(1.0, wait))


def _over_budget():
    """
    本轮是否该收尾？
      1) 时间预算用尽 → 必须在 job 超时前主动收尾（否则连已抓数据都提交不了）；
      2) 产出停滞 → 连续 STALL_LIMIT 秒没有任何成功抓取，才算限流把本轮压死。
    判据 2 刻意用"产出"而不是"冷却占已用时长比例"：后者在持续限流下会在约 50 分钟
    就中止，而时间预算有 4 小时（2026-09-15 实测三次运行各 52/52/57 分钟，浪费 78%）。
    """
    now = time.time()
    if _deadline and now >= _deadline:
        return True
    if STALL_LIMIT > 0 and _throttle["last_ok"] > 0:
        return now - _throttle["last_ok"] >= STALL_LIMIT
    return False


def _strike(reason):
    """
    命中限流：拉长全局冷却（指数退避，上限 COOLDOWN_MAX），并重置空计数。
    注意两点防螺旋：
      1) 冷却窗口内的重复命中不叠加 —— 4 个线程会同时撞上限流，
         若各自 +=1 则 strikes 一次跳 4 级、冷却瞬间顶到 COOLDOWN_MAX，
         之后每轮只放行几个请求，90 分钟也推进不了多少（2026-09-14 的实况）。
      2) 收尾由 _over_budget 的"产出停滞"判据决定（连续 STALL_LIMIT 秒零成功），
         不用"冷却占比"——后者会把限流下仍稳定推进的长任务在 50 分钟就砍掉。
    """
    with _print_lock:
        now = time.time()
        if now < _throttle["until"]:
            return                                   # 已在冷却中：本次命中并入当前窗口
        _throttle["empty_streak"] = 0
        _throttle["strikes"] += 1
        cd = min(COOLDOWN_MAX, COOLDOWN_BASE * (2 ** min(_throttle["strikes"] - 1, 5)))
        _throttle["until"] = max(_throttle["until"], now + cd)
        _throttle["cooled"] += cd
        print(f"  [throttle] {reason} -> cool down {cd:.0f}s "
              f"(strike {_throttle['strikes']}, total {_throttle['cooled']:.0f}s)", flush=True)


def _strike_ok():
    """成功一次即认为限流已解除，冷却计数归零"""
    with _print_lock:
        if _throttle["strikes"] or _throttle["empty_streak"]:
            _throttle["strikes"] = 0
            _throttle["empty_streak"] = 0


def _note_empty():
    """空响应可能只是「该股无行情」，但连续大量空说明是被限流 —— 触发冷却"""
    with _print_lock:
        _throttle["empty_streak"] += 1
        hit = _throttle["empty_streak"] >= EMPTY_STREAK_TRIGGER
    if hit:
        _strike(f"{EMPTY_STREAK_TRIGGER} consecutive empty responses")


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


def last_day_priced():
    """
    返回 (参照交易日, 该日有价的代码集合)。
    用途：把「当日缺价」的代码排到队列最前 —— 当日价格是看板的硬需求。

    参照日**不能盲取面板最后一行**：只要有个别股票先拿到新日期的数据，末行就成了
    那一天，而数据源此时往往还没有当日收盘价（盘中运行尤甚）。那样「当日缺价」组会
    膨胀到 1500+ 只且**抓不缩**（每只都返回成功，但都没有那一行），整轮预算被徒劳
    请求吃光、后面各组永远轮不到 —— 2026-09-15 盘中连续两轮约 1000 次请求几乎零效果。
    故要求该日非空数 ≥ MIN_DAY_FILL × 近期中位数，才承认它是"已有数据的交易日"。
    兜底：面板很短或全都不达标时退回末行（判定只会退化为"全都缺"，不会出错）。
    """
    import datetime as dt
    fp = year_path(dt.date.today().year)
    if not os.path.exists(fp):
        return None, set()
    try:
        df = pd.read_csv(fp, index_col=0, dtype={0: str})
    except Exception:
        return None, set()
    if df.empty:
        return None, set()
    df.index = df.index.astype(str)
    counts = df.notna().sum(axis=1)
    med = float(counts.tail(20).median())
    need = MIN_DAY_FILL * med if med > 0 else 0.0
    for pos in range(len(counts) - 1, -1, -1):
        if float(counts.iloc[pos]) >= need:
            row = df.iloc[pos]
            return str(df.index[pos])[:10], set(df.columns[row.notna()])
    return str(df.index[-1])[:10], set(df.columns[df.iloc[-1].notna()])


def _rotate(lst, k):
    """按 k 位置轮转队列：把上次处理过的部分挪到队尾，避免队尾永久饥饿"""
    if not lst:
        return lst
    k %= len(lst)
    return lst[k:] + lst[:k]


def _load_cursor():
    try:
        with open(CURSOR_FP, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cursor(d):
    try:
        os.makedirs(os.path.dirname(CURSOR_FP), exist_ok=True)
        with open(CURSOR_FP, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  [cursor] write failed: {e}", flush=True)


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
            _rate_gate()
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
    """
    返回 (code, rows, reason)：
      rows   = [(day, close)]，失败为空列表
      reason = ok / empty（两源都无此代码，多为退市）/ rate_limited / error
    新浪为主、腾讯兜底。命中限流时触发全局冷却，避免持续猛打。
    """
    sym = sina_symbol(code)
    if sym is None:
        return code, [], "empty"
    if _over_budget():
        return code, [], "skipped"
    url = SINA_URL.format(sym=sym, n=n)
    limited = False
    for att in range(ATTEMPTS):
        _wait_gate()
        if _over_budget():
            return code, [], "skipped"
        try:
            if DELAY:
                time.sleep(DELAY)
            _rate_gate()
            r = sess.get(url, headers=UA, timeout=20)
            if r.status_code in LIMIT_STATUS:
                limited = True
                _strike(f"sina HTTP {r.status_code}")
                continue
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
                _strike_ok()
                return code, out, "ok"
            break
        except json.JSONDecodeError:
            # 返回了非 JSON（限流页/HTML 错误页）
            limited = True
            _strike("non-JSON response")
        except Exception:
            time.sleep(1.0 + att)
    # 兜底：腾讯。限流期新浪失败的代码会大量涌向这里，每只多 1~5 个请求，
    # 会把请求量放大 2~3 倍、让限流拖得更久 —— 故设每轮次数上限。
    use_tencent = False
    if not _over_budget():
        with _print_lock:
            if _throttle["tencent"] < TENCENT_MAX:
                _throttle["tencent"] += 1
                use_tencent = True
    if use_tencent:
        rows = fetch_tencent(sess, code)
        if rows:
            _strike_ok()
            return code, rows, "ok"
    if limited:
        return code, [], "rate_limited"
    # 两个源都无此代码（退市/未上市）：不落库，下次运行仍在「无存储记录」集合中被重试
    _note_empty()
    return code, [], "empty"


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
    """
    targets: list[(code, bars)]；分批落盘，中断也不丢已抓数据。
    返回 (Counter({reason: n}), done)：done 是本轮实际取用（含 skipped 之外）的任务数，
    由 main() 用来推进队列游标。同时写出**入库的**状态文件（原先的 _failed.txt 被
    .gitignore 排除，云端失败原因完全不可见；改写到 output/ 下随数据一起提交）。
    """
    from collections import Counter
    if not targets:
        print(f"[{tag}] nothing to fetch", flush=True)
        return Counter()
    sess = requests.Session()
    ok_rows, failed = {}, {}
    done = 0
    n_ok = 0
    t0 = time.time()
    global _deadline, _run_t0
    _run_t0 = t0
    _deadline = t0 + BUDGET if BUDGET > 0 else 0.0
    _throttle["last_ok"] = t0        # 产出停滞判据的基准时刻
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
            try:
                code, rows, reason = fu.result()
            except Exception as e:                     # 编程错误必须可见，不得静默丢
                code = futs[fu]
                rows, reason = [], f"error:{type(e).__name__}"
            done += 1
            if rows:
                ok_rows[code] = rows
                n_ok += 1
                _throttle["last_ok"] = time.time()    # 有产出 → 重置停滞计时
            elif reason == "skipped":
                pass                     # 本轮时间预算用尽，未轮到：不算失败、不落清单，下次继续
            else:
                # 空结果原先被 `elif rows:` 静默吞掉（既不计数也不落失败清单）—— 已修正
                failed[code] = reason
            if done % FLUSH_EVERY == 0:
                flush()
                print(f"[{tag}] {done}/{len(targets)} ok={n_ok} fail={len(failed)} "
                      f"{time.time() - t0:.0f}s", flush=True)
            if _over_budget():
                el = time.time() - t0
                print(f"[{tag}] stop early at {done}/{len(targets)} "
                      f"({el:.0f}s elapsed, cooled={_throttle['cooled']:.0f}s "
                      f"= {_throttle['cooled']/max(el,1)*100:.0f}% of elapsed)", flush=True)
                break
    flush()
    n_skip = len(targets) - done
    reasons = Counter(failed.values())
    print(f"[{tag}] {len(targets)} targets -> ok={n_ok} fail={len(failed)} skipped={n_skip} "
          f"({dict(reasons)}) in {time.time() - t0:.0f}s | " +
          ", ".join(f"{y}:{r}x{c}" for y, (r, c) in sorted(stat_all.items())), flush=True)
    if n_skip:
        print(f"[{tag}] {n_skip} not fetched this round (budget) -> will retry next run; "
              f"cooldown total {_throttle['cooled']:.0f}s, tencent fallbacks {_throttle['tencent']}",
              flush=True)
    # 无论成功与否都写状态文件（全成功时清空上一轮遗漏的失败清单，避免陈旧记录误导）
    _write_status(tag, targets, n_ok, failed, reasons)
    return reasons, done


def _write_status(tag, targets, n_ok, failed, reasons):
    """把失败原因写入 output/（随提交入库，云端运行结果可直接核查）"""
    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")
    os.makedirs(out_dir, exist_ok=True)
    fp = os.path.join(out_dir, f"price_fetch_status_{tag}.csv")
    import csv
    with open(fp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["code", "reason", "run_at"])
        now = time.strftime("%Y-%m-%d %H:%M")
        for c, r in sorted(failed.items()):
            w.writerow([c, r, now])
    print(f"[{tag}] status -> {fp} | targets={len(targets)} ok={n_ok} "
          f"fail={len(failed)} reasons={dict(reasons)}", flush=True)


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
        daily, last_day, n_gap, k = [], None, 0, 0
    else:
        full = [c for c in all_codes if c not in have]
        daily = [c for c in sorted(cur) if c in have and sina_symbol(c)]
        if args.sweep:
            extra = [c for c in all_codes if c in have and c not in set(daily)]
            daily = daily + extra
        if args.full:
            full = all_codes
        full_cur = [c for c in full if c in cur]
        full_hist = [c for c in full if c not in cur]

        # 队列顺序按「对当日产品的价值 / 抓取成本」排序，但要两层一起看：
        #
        # 第一层 价值优先（决定"先做什么"）：
        #   ① 当前成分 × 当日缺价（增量 45 根）：当日价格是看板的硬需求，必须最先做完。
        #   ② 当前成分 × 无落库记录（3000 根）：决定每日权重的覆盖率；且无记录必然也缺当日价。
        #   ③ 当前成分 × 当日已有价（增量 45 根）：自愈该股近期其他缺口，对权重覆盖率有直接帮助。
        #   ④ 历史成分 × 无落库记录（3000 根）：只影响历史精度（其权重多为 0），最后做。
        # 注：早期版本把"全部当前成分"当作一个 45 根的大组（不分当日是否有价），
        # 在限流下光这一组就用光预算，回补队列三轮一次都没轮到（价格面板覆盖卡死在 2398 只）。
        #
        # 第二层 队内配额（决定"这一组最多占多少"）—— 限流下单轮产出可能只有 500 条，
        # 而「当日缺价」组盘中可达 1500+ 只，若不给它封顶，它必然吃光整轮预算、
        # 让后面各组永远轮不到（2026-09-15 实测：价格面板覆盖与 IC/IM 权重覆盖率
        # 连续三轮纹丝不动，全因队首组吃光预算）。故 gap 组单轮最多做 GAP_MAX 只。
        #
        # 第三层 组内轮转（决定"从哪个开始"）—— 轮转只作用于 gap 组，成员每轮重新判定，
        # k 只是组内偏移，于是游标前进量与"实际消费条数"精确对应。
        # 至于 full_* 两组，抓成功即进入 stored_codes、下轮自动退出队列，天然收敛，不需要游标。
        # 不轮转会怎样：单轮必然被截断，若每次都从固定队头开始，被截断的永远是同一批队尾
        # 代码（2026-09-14 实测重合度 800/801 = 99.9%），队尾 763 只连续三轮零进展。
        last_day, has_last = last_day_priced()
        curs = _load_cursor()
        k = int(curs.get("daily", 0) or 0)
        # 游标放在「当日缺价」组**内部**（而不是未过滤的 daily 上）：该组成员每轮
        # 重新判定，k 只是组内偏移，于是"本轮消费多少只"与"游标前进多少"精确对应。
        gap_all = [c for c in daily if c not in has_last]
        rest = [c for c in daily if c in has_last]
        gap = _rotate(gap_all, k)
        if GAP_MAX > 0:
            gap = gap[:GAP_MAX]          # 单轮配额：别让这一组吃光预算、饿死后面各组
        n_gap = len(gap)
        targets = ([(c, TAIL_BARS) for c in gap]
                   + [(c, FULL_BARS) for c in full_cur]
                   + [(c, TAIL_BARS) for c in rest]
                   + [(c, FULL_BARS) for c in full_hist])
    if args.limit:
        targets = targets[: args.limit]

    n_full = sum(1 for _, n in targets if n == FULL_BARS)
    n_tail = sum(1 for _, n in targets if n == TAIL_BARS)
    est = (n_full * 1.6 + n_tail * 0.9) / max(WORKERS, 1)      # 无节流下的粗略耗时（秒）
    print(f"universe={len(all_codes)} stored={len(have)} current={len(cur)} "
          f"-> full={n_full} tail={n_tail} | workers={WORKERS} delay={DELAY}s "
          f"rate={RATE}/s budget={BUDGET:.0f}s est={est/60:.0f}min (throttle-free)", flush=True)
    if not args.codes:
        print(f"  queue: 当日({last_day})缺价={n_gap}/{len(gap_all)} | 当前成分缺记录={len(full_cur)} "
              f"| 当日有价自愈={len(rest)} | 历史成分缺记录={len(full_hist)} "
              f"| gap cursor={k}", flush=True)
    reasons, done = run(targets, "prices")
    if not args.codes and daily:
        # 游标按"本轮实际消费掉的 gap 条数"前进。gap 组在队首且按顺序消费，
        # 故 min(done, n_gap) 是精确值（旧版把偏移放在未过滤的 daily 上，只能用近似）。
        # 整组（gap_all）都被消费过则归零 —— 下一轮多半已是一批新的缺价代码。
        used = min(done, n_gap)
        new_k = (k + used) % len(gap_all) if gap_all and used < len(gap_all) else 0
        _save_cursor({"daily": new_k, "daily_len": len(daily), "gap": n_gap,
                      "gap_all": len(gap_all), "last_day": last_day or "",
                      "done": done, "run_at": time.strftime("%Y-%m-%d %H:%M")})
        print(f"[cursor] gap {k} -> {new_k} (used={used}/{n_gap}, done={done}/{len(targets)})",
              flush=True)
    print("DONE", flush=True)


if __name__ == "__main__":
    main()

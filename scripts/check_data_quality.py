# -*- coding: utf-8 -*-
"""
数据质量护栏（sanity check）

输出 output/data_quality_report.csv，检查三件事：
  1) 隐含股息率：DPV 折算的年化股息率是否落在各品种合理带内（缺失/污染会立刻暴露）
  2) 分红文件覆盖率：事件池中真正有分红文件的股票占比（补抓进度）
  3) 已公告覆盖率：DPV 中真值部分占比（预测占比）
异常会打印 GitHub Actions 注解（::warning::），并在报告中标 WARN；始终以 0 退出，不阻断流水线。
用法：python scripts/check_data_quality.py
"""
import os, sys, glob, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import RAW, OUT, PRODUCTS, env_setup, INDEX_MEMBERS
env_setup()
import numpy as np
import pandas as pd

# 各品种隐含股息率合理带（%）：低于下界多半是分红缺失，高于上界多半是数据污染
BANDS = {
    "IF": (0.6, 5.0),
    "IH": (0.8, 6.0),
    "IC": (0.4, 4.0),
    "IM": (0.2, 3.5),
}
HARD_HI = 12.0          # 单日绝对上限（超出即视为异常，仅统计剩余期限≥LONG_REMAIN_DAYS 的行）
MIN_REMAIN_DAYS = 55    # 剩余期限过短的合约年化会爆炸，不入水平统计
LONG_REMAIN_DAYS = 120  # 极值判定只用长剩余期限的行（避开分红季近月合约的年化放大）


def div_file_coverage(product, index_code):
    """事件池（月度权重并集 ∪ 当前快照）中有分红文件的占比"""
    members = set()
    for fp in glob.glob(os.path.join(RAW, "weights", f"{index_code}.SH_*.csv")):
        try:
            df = pd.read_csv(fp)
            if "wind_code" in df.columns:
                members |= set(df["wind_code"].astype(str).str[:6])
        except Exception:
            continue
    snap = os.path.join(RAW, "weights", f"{product}_weights.csv")
    if os.path.exists(snap):
        members |= set(pd.read_csv(snap, dtype={"stock_code": str})["stock_code"])
    uni_fp = os.path.join(RAW, "universe_all.csv")
    if os.path.exists(uni_fp):
        members &= set(pd.read_csv(uni_fp, dtype={"stock_code": str})["stock_code"])
    if not members:
        return np.nan, 0, 0
    have = 0
    for c in members:
        fp = os.path.join(RAW, "dividends", f"{c}.csv")
        if os.path.exists(fp) and os.path.getsize(fp) > 50:
            have += 1
    return have / len(members), have, len(members)


EXPECT_N = INDEX_MEMBERS
MIN_PRICE_COV = 0.3     # 价格计数覆盖率灾难性下限（与 weight_daily.MIN_PRICE_COV 一致）
MIN_W_COV = 0.9         # 每日权重生效所需的**权重**覆盖率下限（与 weight_daily.MIN_W_COV 一致）


def structure_checks():
    """结构检查：月末权重文件完整性 + 个股价格面板覆盖（每日权重的前提）"""
    rows, warns = [], []

    rows.append(dict(item="月末权重文件", value="", status="", note="整行缺失由引擎自动修复（补回或按缺口归一）"))
    for prod, cfg in PRODUCTS.items():
        idx = cfg["index"]
        files = sorted(glob.glob(os.path.join(RAW, "weights", f"{idx}.SH_*.csv")))
        exp = EXPECT_N.get(idx, 0)
        short, max_def = 0, 0.0
        for fp in files:
            try:
                df = pd.read_csv(fp)
                w = pd.to_numeric(df["i_weight"], errors="coerce")
            except Exception:
                continue
            if exp and len(df) < exp:
                short += 1
                max_def = max(max_def, 100.0 - w.sum())
        note = ""
        if short:
            note = (f"行数少于成分数（最多缺 {max_def:.2f} 权重），引擎已自动修复；"
                    f"未修复则该期 DPV 会低估同等比例")
        rows.append(dict(item=f"  {prod} {idx}", value=f"{len(files)} 期 / 整行缺失 {short} 期",
                         status="WARN" if short else "OK", note=note))
        if short:
            warns.append(f"{prod} 月末权重文件 {short}/{len(files)} 期整行缺失（最多 {max_def:.2f} 权重），已自动修复")

    # 个股价格面板（每日权重数据源）
    pfiles = sorted(glob.glob(os.path.join(RAW, "stock_prices", "close_*.csv")))
    if not pfiles:
        rows.append(dict(item="个股价格面板", value="缺失", status="WARN",
                         note="data_raw/stock_prices 为空 → 每日权重自动回退为月末静态权重"))
        warns.append("个股价格面板缺失，每日权重未生效")
    else:
        codes, last = set(), ""
        for fp in pfiles:
            try:
                head = pd.read_csv(fp, nrows=1, dtype={0: str})
                codes |= {c for c in head.columns if c != head.columns[0]}
            except Exception:
                continue
        for fp in reversed(pfiles):
            try:
                tail = pd.read_csv(fp, usecols=[0])
                if len(tail):
                    last = str(tail.iloc[-1, 0])
                    break
            except Exception:
                continue
        uni_fp = os.path.join(RAW, "universe_all.csv")
        uni = set(pd.read_csv(uni_fp, dtype={"stock_code": str})["stock_code"]) if os.path.exists(uni_fp) else set()
        cov = len(codes & uni) / len(uni) if uni else np.nan
        st = "OK" if np.isfinite(cov) and cov >= MIN_PRICE_COV else "WARN"
        note = f"最新交易日 {last}；计数覆盖率（含已退市历史成分，后者天然抓不到，故不可能到 100%）"
        if st == "WARN":
            warns.append(f"个股价格覆盖 {cov*100:.1f}% 低于下限 {MIN_PRICE_COV*100:.0f}%")
        rows.append(dict(item="个股价格面板", value=f"{len(codes & uni)}/{len(uni)} 只 ({cov*100:.1f}%)",
                         status=st, note=note))

        # ---- 最新交易日的填充度：当日增量是否真的跑完 ----
        # 只检查「面板里有没有这只票」不够：当日增量若被昂贵的全量回补饿死（2026-09-14 实况：
        # 回补 446 只后中止，当日增量 1839 只一只没跑），面板仍在、覆盖率仍好看，
        # 但最新一天只有少数代码有价 —— 当日权重会静默退化为 forward fill 的上一日价格。
        # 故与**前一交易日**对比：两者应基本持平，骤降说明当日增量没跑完。
        cur_all = set()
        for prod in PRODUCTS:
            fp = os.path.join(RAW, "weights", f"{prod}_weights.csv")
            if os.path.exists(fp):
                try:
                    cur_all |= set(pd.read_csv(fp, dtype={"stock_code": str})["stock_code"])
                except Exception:
                    continue
        if cur_all:
            try:
                dfp = pd.read_csv(pfiles[-1], dtype={0: str})
                dfp = dfp.set_index(dfp.columns[0])
                cols = [c for c in dfp.columns if c in cur_all]
                if len(dfp) >= 2 and cols:
                    d_last = str(dfp.index[-1])
                    d_prev = str(dfp.index[-2])
                    n_last = int(dfp.iloc[-1][cols].notna().sum())
                    n_prev = int(dfp.iloc[-2][cols].notna().sum())
                    r = n_last / max(n_prev, 1)
                    st2 = "OK" if r >= 0.9 else "WARN"
                    n2 = (f"{d_last} 有价 {n_last}/{len(cols)} 只，前一交易日({d_prev}) {n_prev} 只 "
                          f"({r*100:.0f}%)")
                    if st2 == "WARN":
                        n2 += ("；当日增量被限流截断 → 当日权重会退化为上一日价格。"
                               "队列已按「当日缺口优先」排序并带游标轮转，下次运行会自动接着补齐")
                        warns.append(f"最新交易日价格覆盖骤降（{n_last}/{n_prev}），当日增量可能被截断")
                    rows.append(dict(item="最新交易日价格填充（当前成分）", value=n2.split("；")[0],
                                     status=st2, note=n2))
            except Exception as e:
                rows.append(dict(item="最新交易日价格填充（当前成分）", value="检查失败",
                                 status="WARN", note=f"{type(e).__name__}: {e}"))

        # ---- 抓取队列游标：当日增量组下一轮从哪继续（用于判断是否在"原地打转"）----
        try:
            _cfp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "output", "price_cursor.json")
            with open(_cfp, encoding="utf-8") as _f:
                cs = json.load(_f)
            rows.append(dict(
                item="抓取队列游标（当日缺价组）",
                value=(f"组内偏移 {cs.get('daily', 0)}"
                       f"｜当日缺口 {cs.get('gap', 0)} 只"
                       f"（未封顶时全组 {cs.get('gap_all', cs.get('gap', 0))} 只）"),
                status="OK",
                note=(f"最后运行 {cs.get('run_at', '')}｜参照日 {cs.get('last_day', '')}｜"
                      f"本轮实际取用 {cs.get('done', 0)} 只；下一轮从该偏移接着做（组内取模轮转）——"
                      f"若连续多轮偏移不动且缺口不减，说明限流把单轮产出压到了 0。"
                      f"注意游标只在'当日缺价'组内轮转，该组单轮最多做 PRICE_GAP_MAX 只，"
                      f"以免吃光预算饿死后面的回补组")) )
        except Exception:
            pass

        # ---- 每品种：每日权重是否真正生效（这是最该看的指标）----
        # 判据用**权重覆盖率**：缺的可能是权重很大的成分（2026-09-13 云端即如此，
        # IC 按只数 61% 过线、按权重却缺 40.7/100，输出的只是半更新权重）。
        rows.append(dict(item="每日权重启用状态", value="", status="", note="按品种分别判定（权重覆盖率为准）"))
        for prod in PRODUCTS:
            snap_fp = os.path.join(RAW, "weights", f"{prod}_weights.csv")
            if not os.path.exists(snap_fp):
                continue
            snap = pd.read_csv(snap_fp, dtype={"stock_code": str, "index_code": str})
            if "weight_pct" not in snap.columns:
                continue
            snap["_w"] = pd.to_numeric(snap["weight_pct"], errors="coerce")
            snap = snap.dropna(subset=["_w"])
            idx = str(snap["index_code"].iloc[0]) if "index_code" in snap.columns else ""
            axis = set(snap["stock_code"])
            for fp in glob.glob(os.path.join(RAW, "weights", f"{idx}.SH_*.csv")):
                try:
                    d = pd.read_csv(fp, dtype=str)
                    col = "wind_code" if "wind_code" in d.columns else d.columns[0]
                    axis |= {str(x).split(".")[0] for x in d[col].dropna()}
                except Exception:
                    continue
            n_axis = len(axis)
            cnt_cov = len(axis & codes) / n_axis if n_axis else np.nan
            tot_w = float(snap["_w"].sum())
            w_cov = float(snap.loc[snap["stock_code"].isin(codes), "_w"].sum() / tot_w) if tot_w else np.nan
            ok = np.isfinite(w_cov) and w_cov >= MIN_W_COV and np.isfinite(cnt_cov) and cnt_cov >= MIN_PRICE_COV
            miss_w = tot_w - float(snap.loc[snap["stock_code"].isin(codes), "_w"].sum())
            rows.append(dict(
                item=f"  {prod} {idx}",
                value=f"权重覆盖 {w_cov*100:.1f}% / 计数覆盖 {cnt_cov*100:.1f}%（缺 {miss_w:.1f}/100 权重）",
                status="OK（每日权重生效）" if ok else "WARN（回退月末静态权重）",
                note=f"axis={n_axis}，阈值 权重≥{MIN_W_COV*100:.0f}% 且计数≥{MIN_PRICE_COV*100:.0f}%"))
            if not ok:
                warns.append(f"{prod} 每日权重未生效：权重覆盖 {w_cov*100:.1f}% < {MIN_W_COV*100:.0f}%")

    # ---- 最新交易日四口径分化：面板末端的「全同」是预测候选缺失的指纹 ----
    # 四口径只在预测态事件（ann > t）上分化。若事件池不含「未公告候选」，t 逼近数据
    # 末端时 pred_sel 必然趋空 → 当下四口径全同 + 窗口内未公告分红整体漏算
    # （2026-09-16 实况：announced_ratio 恒 1.0，DPV 漏算近半）。历史时点不受影响，
    # 回测 MAE 永远探不到 —— 只能盯面板末端这一天。
    rows.append(dict(item="最新交易日四口径分化", value="", status="",
                     note="全同 = 预测候选缺失 = 当下 DPV 漏算未公告分红（历史回测探不到，只能盯末端）"))
    for prod in PRODUCTS:
        fp = os.path.join(OUT, f"{prod}_panel.csv")
        if not os.path.exists(fp):
            continue
        try:
            dfa = pd.read_csv(fp, dtype={"date": str, "contract": str}).dropna(subset=["dpv_pts"])
        except Exception as e:
            rows.append(dict(item=f"  {prod}", value="检查失败", status="WARN",
                             note=f"{type(e).__name__}: {e}"))
            continue
        if not len(dfa):
            continue
        last_day = dfa["date"].max()
        d = dfa[dfa["date"] == last_day]
        g = d.groupby("contract")["dpv_pts"].agg(["nunique", "count"])
        g = g[g["count"] >= 4]          # 四口径齐全的合约才可比
        if not len(g):
            continue
        n_diff = int((g["nunique"] > 1).sum())
        ar = pd.to_numeric(d["announced_ratio"], errors="coerce")
        ar_min = float(ar.min()) if ar.notna().any() else np.nan
        ok = n_diff >= 1                # 近月无分红窗口内全同属正常，只要求至少一个合约互异
        note = (f"{last_day}：{n_diff}/{len(g)} 个合约四口径互异"
                + (f"；announced_ratio 最低 {ar_min:.2f}" if np.isfinite(ar_min) else ""))
        if not ok:
            note += ("；全部全同 → 未公告候选缺失，当下 DPV 漏算窗口内未公告分红")
            warns.append(f"{prod} 面板末端（{last_day}）四口径全同：预测候选缺失，当下 DPV 漏算未公告分红")
        rows.append(dict(item=f"  {prod}", value=f"{n_diff}/{len(g)} 合约互异",
                         status="OK" if ok else "WARN", note=note))
    return pd.DataFrame(rows), warns


def main():
    rows = []
    for prod in PRODUCTS:
        fp = os.path.join(OUT, f"{prod}_panel.csv")
        if not os.path.exists(fp):
            print(f"[skip] {prod}: panel not found")
            continue
        df = pd.read_csv(fp, dtype={"date": str})
        df = df[df["calibre"] == "y"].dropna(subset=["future", "dpv_pts"])
        if not len(df):
            continue
        df["date"] = df["date"].astype(str)
        remain = (pd.to_datetime(df["expire"]) - pd.to_datetime(df["date"])).dt.days
        use = df[(remain >= MIN_REMAIN_DAYS) & (df["spot"] > 0)].copy()
        if not len(use):
            continue
        use["years"] = (pd.to_datetime(use["expire"]) - pd.to_datetime(use["date"])).dt.days / 365.0
        use["implied_pct"] = use["dpv_pts"] / use["spot"] / use["years"] * 100.0
        use["year"] = use["date"].str[:4]

        cov, have, total = div_file_coverage(prod, PRODUCTS[prod]["index"])
        lo, hi = BANDS.get(prod, (0.2, 6.0))
        prev_med = None
        for yr, g in use.groupby("year"):
            med = g["implied_pct"].median()
            mean = g["implied_pct"].mean()
            gl = g[g["years"] * 365 >= LONG_REMAIN_DAYS]
            p_lo = gl["implied_pct"].quantile(0.01) if len(gl) else np.nan
            p_hi = gl["implied_pct"].quantile(0.99) if len(gl) else np.nan
            yoy = (med / prev_med - 1) * 100 if prev_med and prev_med > 0 else np.nan
            prev_med = med
            notes, status = [], "OK"
            if med < lo or med > hi:
                notes.append(f"中位数{med:.2f}%越界[{lo},{hi}]")
                status = "WARN"
            if np.isfinite(p_hi) and p_hi > HARD_HI:
                notes.append(f"高位p99={p_hi:.2f}%越硬界")
                status = "WARN"
            if np.isfinite(yoy) and abs(yoy) > 60:
                notes.append(f"同比变动{yoy:+.0f}%")   # 窗口成分变化会带来自然波动，仅提示
            if np.isfinite(cov) and cov < 0.95:
                # 覆盖率不足是「补抓进行中」的已知状态，只作提示（避免告警疲劳）；
                # 其后果（DPV 低估）由隐含股息率越界捕获
                notes.append(f"分红文件覆盖{cov*100:.1f}%")
            rows.append(dict(product=prod, year=yr, days=len(g),
                             implied_yield_pct=round(med, 3),
                             mean_pct=round(mean, 3),
                             p01_pct=round(p_lo, 3) if np.isfinite(p_lo) else np.nan,
                             p99_pct=round(p_hi, 3) if np.isfinite(p_hi) else np.nan,
                             yoy_change_pct=round(yoy, 1) if np.isfinite(yoy) else np.nan,
                             dpv_range_pts=f"{g['dpv_pts'].min():.1f}~{g['dpv_pts'].max():.1f}",
                             coverage=round(float(g["announced_ratio"].dropna().mean()), 3)
                             if g["announced_ratio"].notna().any() else np.nan,
                             div_file_cov=round(cov, 3) if np.isfinite(cov) else np.nan,
                             div_files=f"{have}/{total}",
                             status=status, note="; ".join(notes)))
    rep = pd.DataFrame(rows)
    out_fp = os.path.join(OUT, "data_quality_report.csv")
    rep.to_csv(out_fp, index=False, encoding="utf-8-sig")

    # 结构检查（权重文件完整性 / 价格面板覆盖）
    st_rep, st_warns = structure_checks()
    st_rep.to_csv(os.path.join(OUT, "data_quality_structure.csv"), index=False, encoding="utf-8-sig")
    print("[结构检查]")
    for _, r in st_rep.iterrows():
        print(f"  {r['item']}: {r['value']} {r['status']} {r['note']}")
    for w in st_warns:
        print(f"::warning title=数据质量::{w}")

    if not len(rep):
        print("[WARN] data quality report is empty")
        return
    print(f"[OK] data_quality_report rows={len(rep)} -> {out_fp}")
    for _, r in rep.iterrows():
        line = (f"  {r['product']} {r['year']}: 隐含股息率(中位) {r['implied_yield_pct']:.2f}% "
                f"(p1~p99 {r['p01_pct']:.2f}~{r['p99_pct']:.2f}%) 覆盖率 {r['coverage']} "
                f"{r['status']} {r['note']}")
        print(line)
        if r["status"] != "OK":
            print(f"::warning title=数据质量::{r['product']} {r['year']} {r['note']}")
    bad = rep[rep["status"] != "OK"]
    print(f"[SUMMARY] 检查 {len(rep)} 条，告警 {len(bad)} 条")


if __name__ == "__main__":
    main()

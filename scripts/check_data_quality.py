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
import os, sys, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import RAW, OUT, PRODUCTS, env_setup
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

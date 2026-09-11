# -*- coding: utf-8 -*-
"""Streamlit 看板：股指期货剔除分红基差（数据由 GitHub Actions 每日更新）"""
import os
import datetime as dt

import pandas as pd
import streamlit as st

st.set_page_config(page_title="股指期货剔除分红基差", page_icon="📊", layout="wide")

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
PRODUCTS = {"IF": "沪深300", "IH": "上证50", "IC": "中证500", "IM": "中证1000"}
CALIBRES = {"y": "固定股息率", "d": "固定分红", "p": "固定派息率", "pq": "派息率×季报EPS"}
BT_COLS = ["固定股息率", "固定分红", "固定派息率", "派息率x季报EPS"]
COLORS = {"IF": "#1565C0", "IH": "#00897B", "IC": "#F9A825", "IM": "#E53935"}
RANGE_PRESETS = {"近3个月": 90, "近6个月": 182, "近1年": 365, "近3年": 1095, "近5年": 1825, "全部": None}


@st.cache_data(ttl=3600)
def load_panel():
    frames = []
    for p in PRODUCTS:
        fp = os.path.join(OUT, f"{p}_panel.csv")
        if os.path.exists(fp):
            frames.append(pd.read_csv(fp, dtype={"date": str}))
    if not frames:
        st.error("未找到面板数据，请先运行 scripts/run_daily.py")
        st.stop()
    return pd.concat(frames, ignore_index=True)


@st.cache_data(ttl=3600)
def load_backtest():
    fp = os.path.join(OUT, "backtest_2025_calibres.csv")
    return pd.read_csv(fp) if os.path.exists(fp) else pd.DataFrame()


@st.cache_data(ttl=3600)
def load_quality():
    fp = os.path.join(OUT, "data_quality_report.csv")
    return pd.read_csv(fp) if os.path.exists(fp) else pd.DataFrame()


full = load_panel()
full["date"] = full["date"].astype(str)
DATA_END = full["date"].max()

# ---------- 侧边栏 ----------
with st.sidebar:
    prod = st.selectbox("品种", list(PRODUCTS), format_func=lambda x: f"{x} · {PRODUCTS[x]}")
    role = st.selectbox("合约", ["current", "next", "q1", "q2"],
                        format_func=lambda x: {"current": "当月", "next": "次月",
                                               "q1": "当季", "q2": "下季"}[x])
    calibre = st.selectbox("分红预测口径", list(CALIBRES), format_func=CALIBRES.get)
    st.divider()
    preset = st.selectbox("展示时间区间", list(RANGE_PRESETS), index=2)
    if preset == "全部":
        start_str = full["date"].min()
    else:
        default_start = (dt.date.fromisoformat(DATA_END) - dt.timedelta(days=RANGE_PRESETS[preset]))
        custom = st.checkbox("自定义起始日期", value=False)
        if custom:
            start_str = str(st.date_input("起始日期",
                                          value=default_start,
                                          min_value=dt.date.fromisoformat(full["date"].min()),
                                          max_value=dt.date.fromisoformat(DATA_END)))
        else:
            start_str = str(default_start)

# ---------- 时间过滤 ----------
view = full[full["date"] >= start_str].copy()
sub = view[(view["product"] == prod) & (view["role"] == role) & (view["calibre"] == calibre)]
sub = sub.dropna(subset=["future"]).sort_values("date")

# 未调整年化基差率（看板端实时计算：B_raw / S / 剩余年 × 100）
yr = ((pd.to_datetime(sub["expire"]) - pd.to_datetime(sub["date"])).dt.days / 365.0).clip(lower=1e-6)
sub["ann_rate_raw"] = sub["basis_raw"] / sub["spot"] / yr * 100

st.title("股指期货剔除分红基差看板")
st.caption(
    "B_adj = 表观基差 B + DPV = F − (S − DPV)　|　"
    "数据源：中金所官网 / 中证指数公司 / 东方财富 F10（公开数据）　|　"
    f"面板更新时间：{DATA_END}　|　当前展示：{start_str} ~ {DATA_END}"
)

c1, c2, c3, c4, c5 = st.columns(5)
last = sub.iloc[-1] if len(sub) else None
if last is not None:
    c1.metric("现货点位", f"{last['spot']:.1f}")
    c2.metric("原始基差", f"{last['basis_raw']:+.1f}")
    c3.metric("剔除分红基差", f"{last['basis_adj']:+.1f}")
    c4.metric("年化原始基差率", f"{last['ann_rate_raw']:+.2f}%")
    c5.metric("年化调整基差率", f"{last['annualized_rate']:+.2f}%")

st.subheader(f"{prod} {role} 合约：原始基差 vs 剔除分红基差（点）")
chart = sub[["date", "basis_raw", "basis_adj"]].set_index("date")
chart.columns = ["原始基差（未调整）", "剔除分红基差"]
st.line_chart(chart, height=320)

st.subheader(f"{prod} {role} 合约：年化基差率对比（%）")
rate_chart = sub[["date", "ann_rate_raw", "annualized_rate"]].dropna(how="all").set_index("date")
rate_chart.columns = ["年化原始基差率（未调整）", "年化调整基差率（剔除分红）"]
st.line_chart(rate_chart, height=280)

st.subheader("各口径 DPV 对比（点）")
piv = (view[(view["product"] == prod) & (view["role"] == role)]
       .pivot_table(index="date", columns="calibre", values="dpv_pts"))
piv.columns = [f"{CALIBRES.get(c, c)}口径" for c in piv.columns]
st.line_chart(piv, height=260)

st.subheader("DPV 已公告覆盖率")
cov = sub[["date", "announced_ratio"]].dropna().set_index("date")
st.area_chart(cov, height=200)

st.subheader("四品种年化调整基差率（%，当月合约，当前口径）")
cur = view[(view["role"] == "current") & (view["calibre"] == calibre)].dropna(subset=["annualized_rate"])
piv2 = cur.pivot_table(index="date", columns="product", values="annualized_rate")
st.line_chart(piv2, height=280)

st.subheader("2025 样本外回测（每月末视角 vs 最终实际分红）")
bt = load_backtest()
if len(bt):
    agg = {}
    for nm in BT_COLS:
        col = f"{nm}_err"
        if col in bt.columns:
            agg[f"{nm} MAE"] = (col, lambda s: s.abs().mean())
    if agg:
        mae = bt.groupby("product").agg(**agg).round(2)
        st.caption("MAE 单位：指数点。越小越准。")
        st.dataframe(mae, use_container_width=True)
    with st.expander("逐月明细"):
        st.dataframe(bt, use_container_width=True)

# ---------- 数据质量 ----------
q = load_quality()
if len(q):
    bad = q[q["status"] != "OK"] if "status" in q.columns else pd.DataFrame()
    latest = q[q["year"] == q["year"].max()] if "year" in q.columns else q
    with st.expander(f"数据质量校验（年度隐含股息率）{'⚠ 有 %d 条告警' % len(bad) if len(bad) else '✓ 全部通过'}"):
        st.caption("隐含股息率 = 该年度到期合约在年初窗口的 DPV ÷ 现货点位，合理区间 0.2%~5.0%；"
                   "超出或同比跳变>60% 会标记 WARN，通常意味着分红数据缺失或接口异常。")
        show_cols = [c for c in ["product", "year", "dpv_pts", "spot", "implied_yield_pct",
                                 "yoy_change_pct", "coverage", "status", "note"] if c in q.columns]
        st.dataframe(latest[show_cols], use_container_width=True)
        if len(bad):
            st.dataframe(bad[show_cols], use_container_width=True)

with st.expander("口径与数据说明"):
    st.markdown(
        "- **原始基差** B = F − S；**剔除分红基差** B_adj = B + DPV = F − (S − DPV)\n"
        "- 年化均按单利：基差 ÷ 现货 ÷ 剩余年化天数 × 365 × 100%\n"
        "- DPV 点数 = Σ w_i(t) × y_i × S_t；w_i(t) 为 t 前最近月末的历史权重（月度静态文件）\n"
        "- 分红金额分层：实施→真值；预案→公告值；未公告→各口径均值外推（无前视信息集）\n"
        "- 四个口径：固定股息率 / 固定分红 / 固定派息率（上年年报 EPS）/ 派息率×季报 EPS\n"
        "- 「派息率×季报EPS」= 3年平均派息率 × 下一财年 EPS 估计；EPS 由已披露季报 TTM 外推"
        "（上年全年 + 本期累计 − 上年同期累计），比只锚定上年年报更及时\n"
        "- 已公告覆盖率 = DPV 中真值部分占比，低覆盖率时段请谨慎解读\n"
        "- 权重：中证官网月末文件（历史静态）+ 当月快照每日更新；行情：中金所官网；分红：东财 F10；"
        "季报 EPS：东财业绩报表\n"
        "- 数据边界：已退市股票东财 F10 无分红数据，2015-16 年早期成分分红可能低估"
    )

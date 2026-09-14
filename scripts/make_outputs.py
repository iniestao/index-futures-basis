# -*- coding: utf-8 -*-
"""交付包：Excel 多 sheet + 图表 PNG"""
import os, sys, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import RAW, OUT, PRODUCTS, env_setup
env_setup()
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# 字体兜底链：Windows 命中微软雅黑，Linux/CI 命中 Noto CJK（GitHub runner 预装），避免 findfont warning 和中文方框
plt.rcParams["font.sans-serif"] = [
    "Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "Noto Sans CJK JP",
    "WenQuanYi Zen Hei", "DejaVu Sans",
]
plt.rcParams["axes.unicode_minus"] = False
COLORS = {"IF": "#1565C0", "IH": "#00897B", "IC": "#F9A825", "IM": "#E53935"}

def load_all():
    panels = []
    for prod in PRODUCTS:
        p = os.path.join(OUT, f"{prod}_panel.csv")
        if os.path.exists(p):
            panels.append(pd.read_csv(p, dtype={"date": str}))
    return pd.concat(panels, ignore_index=True)

def make_charts(full):
    fig, axes = plt.subplots(2, 2, figsize=(14, 8), dpi=130)
    for ax, prod in zip(axes.flat, ["IF", "IH", "IC", "IM"]):
        sub = full[(full["product"] == prod) & (full["role"] == "current") & (full["calibre"] == "y")]
        sub = sub.dropna(subset=["future"]).sort_values("date")
        if len(sub) == 0:
            continue
        ax.plot(sub["date"], sub["basis_raw"], lw=0.8, color="#9E9E9E", label="表观基差 B=F−S")
        ax.plot(sub["date"], sub["basis_adj"], lw=1.0, color=COLORS[prod], label="剔除分红基差 B_adj（固定股息率口径）")
        ax.axhline(0, color="black", lw=0.6)
        ax.set_title(f"{prod} 当月合约：剔除分红前后基差对比（点）", fontsize=10)
        ax.legend(fontsize=8); ax.grid(alpha=0.25)
        ticks = sub["date"].iloc[::max(len(sub)//8, 1)]
        ax.set_xticks(ticks); ax.set_xticklabels([d[:7] for d in ticks], rotation=30, fontsize=7)
    plt.tight_layout()
    p1 = os.path.join(OUT, "chart_basis_compare.png")
    plt.savefig(p1); plt.close()
    # 年化调整基差率四品种叠加（当月合约）
    fig, ax = plt.subplots(figsize=(12, 5), dpi=130)
    for prod in ["IF", "IH", "IC", "IM"]:
        sub = full[(full["product"] == prod) & (full["role"] == "current") & (full["calibre"] == "y")].dropna(subset=["annualized_rate"])
        ax.plot(sub["date"], sub["annualized_rate"], lw=1.0, color=COLORS[prod], label=prod)
    ax.axhline(0, color="black", lw=0.6)
    ax.set_title("四品种当月合约年化调整基差率（%）——剔除分红后", fontsize=11)
    ax.legend(); ax.grid(alpha=0.25)
    p2 = os.path.join(OUT, "chart_annualized_rate.png")
    plt.tight_layout(); plt.savefig(p2); plt.close()
    # 覆盖率（IF 示例）
    fig, ax = plt.subplots(figsize=(12, 4), dpi=130)
    sub = full[(full["product"] == "IF") & (full["role"] == "current")].dropna(subset=["announced_ratio"])
    ax.fill_between(sub["date"], sub["announced_ratio"], color="#1565C0", alpha=0.35)
    ax.set_ylim(0, 1.05); ax.set_title("IF 当月合约 DPV 已公告覆盖率（示例）", fontsize=11)
    ax.grid(alpha=0.25)
    p3 = os.path.join(OUT, "chart_coverage_if.png")
    plt.tight_layout(); plt.savefig(p3); plt.close()
    # 季报外推 EPS 口径 vs 上年年报 EPS 口径（当月合约 DPV 对比）
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.2), dpi=130)
    for ax, prod in zip(axes, ["IF", "IC"]):
        piv = (full[(full["product"] == prod) & (full["role"] == "current")]
               .pivot_table(index="date", columns="calibre", values="dpv_pts"))
        for k, lab, col in (("p", "p 口径（上年年报EPS）", "#9E9E9E"),
                            ("pq", "pq 口径（季报外推EPS）", COLORS[prod])):
            if k in piv.columns:
                ax.plot(piv.index, piv[k], lw=1.0, label=lab, color=col)
        ax.set_title(f"{prod} 当月合约 DPV：年报EPS vs 季报外推EPS（点）", fontsize=10)
        ax.legend(fontsize=8); ax.grid(alpha=0.25)
        ticks = piv.index[::max(len(piv) // 6, 1)]
        ax.set_xticks(ticks); ax.set_xticklabels([d[:7] for d in ticks], rotation=30, fontsize=7)
    p4 = os.path.join(OUT, "chart_eps_calibre.png")
    plt.tight_layout(); plt.savefig(p4); plt.close()
    return [p1, p2, p3, p4]

def main():
    full = load_all()
    pngs = make_charts(full)
    readme = pd.DataFrame({
        "说明": [
            "股指期货剔除分红基差流水线 V1.4 交付包",
            "口径：B_adj = 表观基差B + DPV = F − (S − DPV)；DPV点数 = Σ w_i(t) × 股息率_i × S_t",
            "权重：中证官网月末权重为锚点，期内用个股流通市值（不复权价×送转修正）相对变动漂移到每日；按权重覆盖率(≥90%)决定该品种是否启用每日权重，不足则回退月末静态权重",
            "分红分层：已实施→真值；预案公告日≤t→公告值；未公告→各口径外推（无前视信息集）",
            "四口径：y=3年均值股息率 | d=3年均值分红 | p=3年均值派息率×上年年报EPS | pq=3年均值派息率×季报外推EPS(TTM)；2025样本外回测中 pq 劣于其余三口径（MAE高0.6~1.0点），其目标财年季报不可得时退化为 p",
            "数据源：现货/期货=中金所官网；分红=东财F10；EPS=东财业绩报表；权重=中证指数公司；个股日线=新浪财经（全部公开）",
            "字段：basis_raw表观基差 | dpv_pts分红点数 | basis_adj剔分红外基差 | annualized_rate年化%(单利365/自然日)",
            "announced_ratio：DPV中真值部分占比（低=预测主导，谨慎解读）；IM自2022-07上市",
            "数据质量sheet：年度隐含股息率带内校验 + 分红文件覆盖率 + 结构检查（月末权重文件完整性、价格面板覆盖、最新交易日价格填充度、逐品种每日权重启用状态），WARN 需人工确认",
            "局限：已退市股票分红/价格无法从免费渠道获取；未公告外推对突变分红行为滞后；每日权重未含解禁/增发造成的股本变动",
            "版本时间：" + __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M"),
        ]
    })
    weights_example = pd.read_csv(os.path.join(RAW, "weights", "IF_weights.csv"))
    events_sample = None
    ev_dir = os.path.join(RAW, "dividends")
    uni = pd.read_csv(os.path.join(RAW, "universe_all.csv"), dtype={"stock_code": str})

    xlsx_path = os.path.join(OUT, "adjusted_basis_deliverable.xlsx")
    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as w:
        readme.to_excel(w, sheet_name="README_说明", index=False)
        for prod in ["IF", "IH", "IC", "IM"]:
            sub = full[full["product"] == prod].copy().sort_values(["date", "calibre", "role"])
            sub.to_excel(w, sheet_name=f"{prod}_全合约面板", index=False)
        current = full[(full["role"] == "current") & (full["calibre"] == "y")].sort_values("date")
        pivot_badj = current.pivot_table(index="date", columns="product", values="basis_adj")
        pivot_raw = current.pivot_table(index="date", columns="product", values="basis_raw")
        both = pivot_raw.join(pivot_badj, lsuffix="_raw", rsuffix="_adj")
        both.to_excel(w, sheet_name="主力序列对照(固定股息率)", index=True)
        bt_path = os.path.join(OUT, "backtest_2025_calibres.csv")
        if os.path.exists(bt_path):
            pd.read_csv(bt_path).to_excel(w, sheet_name="回测_2025各口径", index=False)
        q_path = os.path.join(OUT, "data_quality_report.csv")
        if os.path.exists(q_path):
            pd.read_csv(q_path).to_excel(w, sheet_name="数据质量校验", index=False)
        weights_example.head(320).to_excel(w, sheet_name="权重快照样例_IF", index=False)
    print("[OK] excel ->", xlsx_path)
    print("[OK] charts ->", pngs)

if __name__ == "__main__":
    main()

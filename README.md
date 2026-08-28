# 股指期货剔除分红基差（IF/IH/IC/IM）

日频计算四个股指期货品种的**剔除分红后基差**：`B_adj = 表观基差 B + DPV = F − (S − DPV)`。
全部基于公开数据（中金所官网、中证指数公司、新浪财经、东方财富 F10），GitHub Actions 每日自动更新，Streamlit 看板可视化。

**方法论详见 [docs/METHODOLOGY.md](docs/METHODOLOGY.md)**（含公式、数据流图、状态机图、回测结果）。

## 架构

```
GitHub Actions（每个交易日收盘后的次日凌晨 00:30 北京时间）
  └─ scripts/run_daily.py
       ├─ fetch_weights.py        当月权重快照（历史月度权重为静态文件）
       ├─ fetch_index_prices.py   指数日线（新浪）
       ├─ fetch_cffex_monthly.py  期货日线（中金所官网月度 zip，增量补当月）
       ├─ fetch_dividends.py      分红明细（东财 F10，增量：mtime>20h 重抓）
       ├─ fetch_eps_annual.py     年报 EPS（东财业绩报表）
       ├─ compute_adjusted_basis.py   三口径 DPV + 调整基差
       ├─ compute_backtest.py         样本外回测
       └─ make_outputs.py             图表
  └─ 自动 commit → Streamlit Cloud 看板自动刷新
```

## 目录

```
├── app.py                     Streamlit 看板
├── scripts/                   流水线脚本
├── data_raw/
│   ├── weights/               中证月末权重（静态历史 + 当月快照）
│   ├── index/                 指数日线
│   ├── futures/               期货日线（中金所官方）
│   ├── dividends/             分红明细（每股一文件）
│   └── eps_annual.csv         年报 EPS
├── output/                    调整基差面板 / 回测 / 图表
└── docs/METHODOLOGY.md        方法论
```

## 本地运行

```bash
pip install -r requirements.txt
python scripts/run_daily.py          # 全量/增量更新
streamlit run app.py                 # 启动看板
```

## 部署

1. **数据自动更新**：仓库自带 `.github/workflows/daily_update.yml`，push 后即生效，每个交易日收盘后次日凌晨 00:30（北京时间）自动运行并 commit 更新后的数据。
2. **Streamlit 看板**：在 [share.streamlit.io](https://share.streamlit.io) 用 GitHub 账号登录 → New app → 选择本仓库 → 主模块填 `app.py` → Deploy。此后随数据 commit 自动刷新。

## 口径速览

| 字段 | 含义 |
|---|---|
| basis_raw | 表观基差 F − S |
| dpv_pts | 合约存续期内预期分红点数（分三口径） |
| basis_adj | 剔除分红基差 = basis_raw + dpv_pts |
| annualized_rate | 年化调整基差率（单利，365/自然日） |
| announced_ratio | DPV 中已公告（真值）部分占比 |
| calibre | 分红预测口径：y=固定股息率 / d=固定分红 / p=固定派息率 |

## License

MIT

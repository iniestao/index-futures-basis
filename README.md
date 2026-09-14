# 股指期货剔除分红基差（IF/IH/IC/IM）

日频计算四个股指期货品种的**剔除分红后基差**：`B_adj = 表观基差 B + DPV = F − (S − DPV)`。
全部基于公开数据（中金所官网、中证指数公司、新浪财经、东方财富 F10），GitHub Actions 每日自动更新，Streamlit 看板可视化。

**方法论详见 [docs/METHODOLOGY.md](docs/METHODOLOGY.md)**（含公式、数据流图、状态机图、回测结果）。

## 架构

```
GitHub Actions（每个交易日收盘后的次日凌晨 00:30 北京时间）
  └─ scripts/run_daily.py
       ├─ fetch_weights.py        当月权重快照 + 最新月末权重落库（历史月度权重为静态文件）
       ├─ fetch_index_prices.py   指数日线（新浪）
       ├─ fetch_cffex_monthly.py  期货日线（中金所官网月度 zip，增量补当月）
       ├─ fetch_dividends.py      分红明细（东财 F10，内容驱动轮转：活跃股2天一刷/沉睡股10天一刷）
       ├─ fetch_eps_annual.py     年报 EPS（东财业绩报表）
       ├─ fetch_eps_quarterly.py  季报累计 EPS + 披露日（pq 口径输入）
       ├─ fetch_stock_prices.py   个股不复权日 K（每日权重输入；首次全量、其后增量刷新当前成分）
       ├─ compute_adjusted_basis.py   四口径 DPV + 调整基差（月末锚定 + 每日流通市值权重）
       ├─ compute_backtest.py         样本外回测
       ├─ make_outputs.py             Excel + 图表
       └─ check_data_quality.py       数据质量护栏（隐含股息率/覆盖/结构，告警不阻断）
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
│   ├── stock_prices/          个股不复权收盘价（close_YYYY.csv，日期×代码宽表）
│   ├── eps_annual.csv         年报 EPS
│   └── eps_quarterly.csv      季报累计 EPS（含披露日）
├── output/                    调整基差面板 / 回测 / 图表 / 数据质量报告
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
| calibre | 分红预测口径：y=固定股息率 / d=固定分红 / p=固定派息率 / pq=派息率×季报外推EPS |

四个口径并列输出，不做自动选择（由使用者按品种/用途判断）。`pq` 用季报 TTM 外推下一财年 EPS，比 `p` 的「上年年报 EPS」更及时。

**权重**：中证只公开月末权重，故以月末官方权重为**锚点**，期内用个股流通市值（不复权收盘价 × 送转修正）的相对变动把权重漂移到每日，归一化分母含全部成分股。**启用判据按权重覆盖率**（有价格的成分股权重 / 锚点总权重的中位数 ≥90%），不足则整体回退月末静态权重（`STATIC_WEIGHTS=1` 强制回退，`MIN_W_COV`/`MIN_PRICE_COV` 调阈值）；判据不用「只数占比」，否则会出现「名义启用每日权重、实际 40% 权重被冻结」的伪每日权重。另对月末文件的**整行缺失**（139/141 期，Wind 导出恒定丢掉代码最小的成分股）自动修复：IF 补回 000001 并赋缺口权重，其余指数按缺口归一到 100。详见 [docs/METHODOLOGY.md](docs/METHODOLOGY.md) 第 5 节。

## 数据质量

每次运行产出 `output/data_quality_report.csv`（年度隐含股息率是否落在品种合理带内、分红/已公告覆盖率）与 `output/data_quality_structure.csv`（月末权重文件成分数与合计、个股价格面板覆盖率、**最新交易日价格填充度**（与前一交易日对比，捕捉"当日增量被回补饿死"这类静默部分更新）、**逐品种每日权重启用状态**）。个股日线抓取失败清单写入 `output/price_fetch_status_prices.csv`（随提交入库，便于核查新浪限流影响面；抓取脚本按「当日增量 → 当前成分回补 → 历史成分回补」排序，具备限流自适应冷却，并受单轮时间预算 `PRICE_BUDGET` 保护（本项目 4 小时，脚本默认 1 小时）——预算用尽或冷却主导本轮即落盘已完成部分，剩余下次运行继续，避免撞上 job 上限被整体取消）。异常打印 Actions 注解并在看板「数据质量校验」中展示，不阻断流水线。详见 [docs/METHODOLOGY.md](docs/METHODOLOGY.md) 第 4 节。

## License

MIT

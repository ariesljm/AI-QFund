# quant-hardening 一页总览（推荐逻辑量化加固）

- 日期：2026-09-17
- 起点：`/grill-me` —— 从量化视角拷问推荐逻辑是否有提升空间
- 工单：`.scratch/quant-hardening/`（spec.md + issues/01..06）
- 状态：**线程关闭**。01/02/03/05/06 resolved；04 needs-triage（gate 松动未清除）；中期滚动 IC 降级搁置。

## 起点判断（grilling 共识）

"有提升空间，但空间在**验证与组合层**，不在加因子。" 经代码证实。

## 决策树与结论

```
Q3 市场择时 ── 不做（自洽于主标尺=同类超额；绝对择时是用户决策）  ← 不变量，全程守住
Q1 组合层 ── 做：贪心去相关 + 同类 RBSA≤2
Q2 因子权重 ── 离线对照后做：滚动 IC 证等权被稀释，改静态 mom0.7/sharpe0.3/ttr0
Q4 量化风险 ── 受 gate（vol_20d 未显著→扩展后边际但 FDR 未过），未落地
Q5 回测地基 ── 先做：扣费+walk-forward+bootstrap+BH-FDR
Q6 成本口径 ── 120d 实际费率 0.65%（赎0.5%+申0.15%前端）
Q-STOP ── 部分触发：等权不显著(p=0.156)、mom_250d 唯一存活(p=0.005)、病在权重非无 alpha
```

## 关键数字

| 工单 | 发现 | 数字 |
|---|---|---|
| 01 地基 | 等权组合分扣费后不显著；mom_250d 唯一 BH-FDR 存活；sharpe 归零 | IC +0.044 p=0.156；mom p=0.005；sharpe −0.003 p=0.94 |
| 03 因子权重 | 滚动 IC 救回被等权稀释的 mom；ttr 全 regime 失效退役；正交化无增益；sortino 与 sharpe 逐 regime 共线冗余 | 滚动 +0.107 p=0.0023 vs 等权 +0.057 p=0.081 |
| 05 权重迁移 | 生产等权→mom0.7/sharpe0.3/ttr0；ttr 退役（ADR-0009 留痕） | IC +0.0945 p=0.0075（扩展样本 p=4.4e-05） |
| 02 组合层 | 贪心去相关+同类≤2 上线；不牺牲收益降风险 | std −13%、夏普代理 +21% |
| 06 扩展含熊 | mom 加固存活；新权重双向显著；vol_20d 边际（FDR 未过）；滚动 IC≈静态→降级搁置 | mom p=4.2e-05；vol_20d p=0.044 raw |

## 生产改动

| 文件 | 改动 |
|---|---|
| `app/engine/screen_pipeline.multifactor_scores` | 等权 → `mom 0.7 / sharpe 0.3 / ttr 0`；ttr 退役留痕于 docstring |
| `app/engine/recommend_v2._select_top5` | Top30 全量审计后接 `select_diversified` |
| `app/engine/screen.select_diversified` | 新增纯函数（贪心去相关 + 同类≤2 + 回退补满） |
| `app/features/calculator.nav_score_factors` | 新增：打分三因子单一来源（生产与回测 PIT 共用，回测快 3×） |
| `config/settings.toml [recommend_v2]` | `portfolio_diversify / portfolio_max_corr / portfolio_max_same_peer` 可配 |

测试：全程 67→102 绿，零行为回退（prefactor 纯委托）。

## 留悬项（需时间/数据，非代码）

| 项 | 为什么不是下一步代码 |
|---|---|
| LLM 排雷 efficacy | 历史无 LLM 审计数据，只能前瞻累积（120d 后出第一条） |
| 熊段 clean efficacy | 2022 数据在但 bias_60d±8% 只切 2 熊日；下调阈值=parameter-fishing |
| 超额口径（vs 绝对） | 需历史 RBSA，fund_features 无 PIT 历史，缺数据 |
| 票 04 vol_20d | p=0.044 raw 但 FDR 未过；追=找显著性滑坡 |
| 中期滚动 IC | 扩展样本≈静态权重，已降级搁置 |

## 不变量（全程守住）

1. regime 只进离线 IC 分析，**永不接回推荐数量/择时**（Q3 立场）。
2. LLM 排雷不废（抓非结构化隐患，与量化风险正交）。
3. 主标尺不变（`excess_rbsa120d_v2`），所有对比同口径。
4. 三标尺分职（ADR-0008）：主标尺(推荐质量)/过程标尺(信号校准)/用户口径(展示)，不混用。

## 复现路径

- 脚本：`scripts/backtest_robustness.py` / `factor_weighting_study.py` / `sortino_regime_check.py` / `portfolio_diversify_study.py`（均支持 `start end since` argv）
- 报告：`docs/backtest/{120d-robustness-check, factor-weighting-study, portfolio-diversify-study, bear-segment-extended}.md`

## 何时该重新打开这条线

- 前瞻累积出第一条 LLM 审计的 120d 结算 → 可量化排雷贡献。
- 出现一次真实熊市 → bias_60d 切出足够熊日 → 熊段 efficacy 干净回答。
- 历史多期 RBSA 回填 → 超额口径 IC 可验。
- 满足以上任一，且 vol_20d 在该数据下 BH-FDR 通过 → 票 04 可解 gate 落地。

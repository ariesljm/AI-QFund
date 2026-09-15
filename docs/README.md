# AI-QFund 2.0 — 智能公募基金量化投研终端

本项目基于"原生 Python + 外部 API + 本地 SQLite"极简架构。2.0 重构（`docs/2.0-consensus.md`）把推荐范式从 1.x 的"LLM 选赛道 → 赛道内排序"改为**全市场初筛 → LLM 排雷审计 → Top5 定论**，并把监控从"6 道防线"收敛为**三级状态机（HOLD/WATCH/EXIT）+ 校准层**。

> **过渡态说明（2026-09-16）**：本 README 描述 2.0 目标架构；部分接线处于过渡态——数据地基回填进行中，推荐主流程接入 2.0 特征/模型待回填后完成（见文末进度表）。

---

## 1. 系统运行与调度架构

`docker-compose` 内 `ENABLE_SCHEDULER=true` 开启调度器，每日定时执行一次**全流程**（`app/pipeline.py`）：

| 阶段 | 说明 |
|------|------|
| 数据基座 | 基金列表、净值增量（rankhandler 快路径 + 慢路径兜底，覆盖度闸门）、指数、重仓股（多期 + `disclosure_date` PIT）、个股估值/日线、特征 |
| 模块一 初筛 | 主动权益池 → 硬过滤（规模/申赎/短历史）→ 特征 → LightGBM 打分 → **Top30 候选池**落库 |
| 模块二 排雷 | LLM 对 Top30 审计（VETO/risk_score/四组切片），事件驱动缓存，复合分定 **Top5** |
| 模块三 跟踪 | 三级状态机（HOLD/WATCH/EXIT）+ 虚拟组合脱轨检测 + 校准层（信号命中率记账） |
| 进化 | 结算账本（超额标尺）、知识库（Bad/Good-Case 回流）、影子闸门（模型切换）、规则退役 |

## 2. 数据基座

- **净值**：强制累计/复权净值；增量更新避免接口封禁；覆盖度闸门（`nav_coverage_gap`）超阈即阻断推荐（数据故障可见，不静默）。
- **PIT 公告日**：`fund_holdings.disclosure_date = 报告期 + 15 个工作日`（ADR-0010），所有回测/样本只认 `disclosure_date <= d` 的持仓（防泄漏）。
- **2.0 新增表**：`stock_valuation_daily`（个股日频 PE/PB/市值）、`stock_daily`（前复权日线）、`screen_candidates`（Top30 + 特征快照）。
- **回填入口**：`scripts/backfill_2_0_data.py`（05/06/07/04 四类，幂等断点续传）。

## 3. 标尺与标签（ADR-0008/0009）

| 标尺 | 口径 | 用途 |
|---|---|---|
| 主标尺 | 40 日收益 − 同类均值（同类 = RBSA 第一行业 + n≥10；`BENCHMARK_VERSION`） | 结算/训练/回测唯一依据 |
| 过程标尺 | 校准层的信号命中率 | 概念漂移的可观测数字 |
| 用户口径 | 绝对收益 > 1% 胜率 | Web 展示 |

- 标签：`y_excess = 超额收益 − λ×最大回撤`（λ 在 `settings.toml [label]`，初值 1.0 = 生产标定值）；`y_abs` 并列保留。
- 版本守卫：`LABEL_VERSION` / `BENCHMARK_VERSION` 不一致即拒绝聚合/强制重训。

## 4. 模块一：全市场初筛（`app/engine/screen*.py`）

`active_equity_pool`（混合+股票剔指数）→ `apply_hard_filters`（合并规模 <5000 万 或 >100 亿 / 暂停申购 / 单日上限 <1000 元 / 净值历史 <62 条）→ 特征打分 → `top_n`（Top30 落库 + 特征快照）。空推荐日区分 `no_opportunity`（市场判断）与 `data_failure`（数据故障）。

## 5. 模块二：LLM 排雷（`app/llm/audit*.py`）

- 审计 JSON：`audit_verdict ∈ {PASS, CONDITIONAL_PASS, VETO}`、`risk_score 0-100`、`veto_reasons`、`audit_details`、`recommendation_summary`；schema 严格校验，**非法即失败不静默降级**。
- 剪枝：VETO 或 risk_score > 60 剔除；复合分 `LGBM × (1 − Risk/100)` 定 Top5。
- 缓存：失效条件三元组（持仓期次/重仓股指纹/事件版本），事件驱动而非时钟驱动。
- 素材装配单一归属 `llm/context.py`（ADR-0003）：基金画像/重仓股/事件切片。

## 6. 模块三：跟踪与校准（`app/engine/state_machine*.py`）

- 三级状态机 `transition(current, signals)` 纯函数：HOLD→WATCH（脱轨/估值高分位/Alpha 连续 5 日负）；WATCH→EXIT（跌破 EMA20/极端估值+动量转负/致命公告）；**EXIT 不可逆**；净值陈旧不计入升级。
- 虚拟组合脱轨（`drift.py`）：Top10 持仓归一化组合 vs 实际净值滚动 15 日，`|corr|<0.40 或 r2<0.25` 判脱轨；零方差/NaN 显式退化。
- 校准层（`calibration.py`）：每路信号记账命中率，连续失灵 3 次降权/5 次停用，样本不足不降权；规则退役（否决类不可逆、信号类提案留痕）。

## 7. 进化

- 知识库（`knowledge.py`）：异常回撤 >8%/EXIT 触发归因分叉（特征滞后 vs 隐患漏判）→ 结构化案例 → Few-Shot 回流（只增不改）。
- 影子闸门（`gate.py`）：≥3 段牛/熊/震荡同向不劣 + 非重叠窗口显著性检验；同一标尺版本下比较；上线后自动回滚。
- 账本：`settlement_ledger`（2.0 主标尺记账，版本守卫）。

## 8. 架构决策

见 `docs/adr/`（0001–0011）：数据写不变式 / LLM 单接口 / 素材装配单一归属 / 调度单源 / 读收敛 / 决策读缝 / scipy 豁免（修订）/ **标尺分职冻结 / 进化分层 / PIT 公告日 / 跟踪对象抽象**（2.0 新增）。

## 9. 实施进度（2026-09-16）

| 层 | 状态 |
|---|---|
| 数据地基 04/05/06/07 | 代码层完成（PIT 读端/估值 fetcher/申赎解析/日线复权）；**回填进行中**（`backfill_2_0_data.py`，后台） |
| 模块一 11 | 纯函数 + 接线骨架 + 种子测试；**2.0 特征/模型替换与 recommend 主流程接入待回填** |
| 模块二 12/13/14 | 切片一装配 + 审计校验/复合分 + 缓存三元组（纯函数）；prompt 模板/落库接线待做 |
| 模块三 15/16/18 | 状态机/脱轨/校准纯函数完成；**落库与接线待做** |
| 进化 17/19/20/21 | 知识库/影子闸门/退役纯函数 + ADR 完成；接线待做 |
| Web 23 / 清理 22 / 文档 24 | 待模块一二三接线后 |

完整工单与验收项见 `.scratch/qfund-2.0/issues/`。

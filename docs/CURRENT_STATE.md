# AI-QFund 项目现状盘点（2026-09）

> 本文档是项目现状的**快照白皮书**：架构、数据、各引擎、已知技术债。
> 生成目的：作为"净值反推实时持仓（Style Tracking）"等改进方案的研究输入。
> 与 `docs/README.md`（设计文档）、`docs/quant-overhaul-plan.md`（量化改造共识与后续计划）互补，
> 本文档侧重**当前真实代码与实测数据**，而非设计意图。
>
> 数据事实标注方式：`[实测]` = 直接查库得到的数字；`[代码]` = 从源码读到的行为。

---

## 1. 一句话定位

中国公募基金量化投研终端，日循环：**数据基座 → 推荐引擎 → 追踪监控 → 自我进化**。
每天自动产出"今天买什么"（虚拟池，无真实下单），并持续审计该决策、把教训回流到未来决策。

技术基调：**极简、本地 SQLite、无重型框架**（无 scipy/sklearn，量化计算仅 NumPy + LightGBM）。

---

## 2. 架构全景与日循环

```
数据基座                推荐引擎                监控引擎
┌──────────────┐   ┌─────────────────┐   ┌─────────────────┐
│ 基金/净值/指数 │→  │ LLM 选赛道       │   │ R1 趋势退出      │
│ 重仓股/行业映射│   │ LightGBM 赛道内排│→  │ R2 风格漂移      │
│ 特征/RBSA    │   │ LLM 终选定论    │   │ R3 赛道锚点      │
└──────────────┘   └─────────────────┘   │ R4 板块优势      │
      ↑                                   │ R5 模型信号      │
 ┌─────────┐   ┌─────────────────────┐   │ LLM 逻辑证伪     │
 │ SQLite  │◄──│ 进化引擎（结算/月度重任务）│   └────────┬────────┘
 └─────────┘   └─────────────────────┘              │
                                                    ▼
                                              信号入库(EXIT/WARNING/BUY_MORE/HOLD)
```

**每日调度**（`app/pipeline.py`，Docker 内 daemon 或手动）：
1. 数据基座：基金列表（周更）、净值增量、宏观指数、重仓股+行业映射（7 天周期）
2. 推荐引擎（前置门控：数据就绪才跑）→ 空推荐日合法记录
3. 监控引擎：对 HOLD 持仓跑防线链（推荐失败不中断监控）
4. 进化引擎：每日结算度量（满 40 日窗口）+ 月度重任务（GA 寻优/元分析/置信度衰减）

**各槽位独立容错**：数据基座/推荐失败不影响监控盯盘。

---

## 3. 数据层现状

### 3.1 数据库（SQLite WAL，`data/qfund.db`）

`[实测]` 19 张表（README 旧计数 18，`purchase_restrictions` 为后加）：

| 域 | 表 | 用途 |
|---|---|---|
| 数据基座 | `fund_basic` | 基金列表（12K+，剔除货币/债/封闭/QDII/FOF 等） |
| | `fund_nav` | 历史净值（单位/累计/复权，复合键 code+date） |
| | `index_daily` | 宽基指数日线（沪深300/上证/沪深300ETF） |
| | `fund_holdings` | 季报重仓股（code+report_date+stock_code） |
| | `stock_industry_map` | 股票→申万二级行业映射 |
| | `fund_features` | 每基金每日特征快照（含 RBSA 前三行业） |
| | `sector_daily_snapshot` | 行业板块每日涨跌+主力净流入 |
| | `data_fetch_failures` | 拉取失败追踪 |
| 决策域 | `recommend_log` | 推荐记录（含买入理由/状态/净值锚点/否决） |
| | `sector_selections` | LLM 赛道选择（推荐/回避/理由） |
| | `macro_news` | 每日宏观摘要（新闻/领涨领跌/资金流/上下文快照） |
| | `empty_recommendations` | 空推荐日（区分 no_opportunity / data_failure） |
| | `monitor_events` | 监控防线触发事件 |
| | `monitor_scores` | 模型预测序列（跨日确认期数据源） |
| | `llm_audit` | LLM 调用审计（prompt 快照+原始输出+解析结果） |
| | `evolution_insights` | 进化洞察（教训条目，带置信度/结构化条件） |
| | `quality_metrics` | 月度质量度量（IC/胜率/裁决损耗/分路径/端到端 P&L） |
| | `purchase_restrictions` | 申购限制（手工维护清单） |
| 通用 | `meta` | 键值元数据（各种 last_run / 版本标记） |

### 3.2 关键数据量 `[实测]`（2026-09-10 查库）

| 数据 | 规模 | 说明 |
|---|---|---|
| `fund_nav` | **11,544,963 行 / 12,675 只基金 / 2015-01-21 ~ 2026-09-03** | 日频、11 年全量，净值反推的因变量已就绪 |
| `index_daily` | 沪深300 & 上证各 4,016 行（2010-02-25 起）；ETF 3,470 行（2012 起） | 可作基准/现金因子 |
| `sector_daily_snapshot` | **仅 17 天 / 135 个行业 / 2026-08-13 ~ 2026-09-04** | ⚠️ 严重缺历史，是净值反推的最大数据缺口 |
| `fund_holdings` | 每基金仅最新一期季报快照（2026-06-30） | ⚠️ 多期历史缺失（见 §8 技术债） |

> 探测结论：东财 `push2his` 板块 K 线接口**可用**（半导体 BK1036 有 1345 天、银行 BK0475 有 1623 天历史），
> 板块历史日线可回填。

### 3.3 数据基座步骤（`app/data/foundation.py` STEP_REGISTRY）

Step 1 基金列表（周重建）→ Step 2 净值增量 + 停更/短历史打标 → Step 3 宏观指数（3 标的）
→ Step 4 重仓股 + 行业映射（7 天周期）→ Step 6 RBSA 统计（仅日志）→ Step 7 特征计算 → Step 8 模型就绪检查。

数据源：天天基金（列表/净值/重仓）、东方财富 push2（板块/资金流/新闻）、新浪（沪深300/上证）。

---

## 4. 特征工程与 RBSA 现状（改进的核心靶点）

### 4.1 模型特征列（`app/domain.py` FEATURE_COLS，12 维）

`hurst_60d`、`momentum_20d`、`calmar`、`downside_vol`、`capture_up`、`capture_down`、
`drawdown_60d`、`reversal_20d`、`mom_5d`、`mom_60d`、`vol_20d` —— 全部由**基金自身净值序列**算出。

另加 **市场状态列**（MARKET_COLS，不进表、打分时实时注入）：`idx_mom_20d`、`idx_vol_20d`、`bias_60d`。

### 4.2 RBSA 现状：**持仓聚合，不是净值回归** `[代码]`

当前 `calc_rbsa()`（`app/features/calculator.py`）的算法是：

```
按季报 Top10 重仓股 → 每只股票的申万行业（stock_industry_map）→ 按持仓权重累加 → 取前 3 大行业
```

即：**静态季报持仓的直接加总**。这正是"看季报选基必吃鱼尾"的根源——
季报滞后 15~45 天，RBSA 反映的是**上一个季末**的持仓，而非"此时此刻"。

**系统里目前没有**任何"用每日净值回归反推持仓"的算法（无 Kalman、无弹性网回归）。
`fund_features` 表里的 `rbsa_industry_1/2/3` + `rbsa_weight_1/2/3` 全部来自上述持仓聚合。

### 4.3 赛道与候选池（`app/engine/sector_pool.py`）

- **赛道** = RBSA 第一行业（量化定池产出），LLM 只在池内选择/否决。
- 量化定池规则（纯量化，不喂 LLM）：5 日动量中位数 > 0、过热剔除（60 日动量 P75+）、
  追高降权、牛市热度降权、资金流出降权、极端高波动剔除、熊市低波动防守。
- 候选池目标 12 个赛道，成员 < 3 只基金即无效。
- 赛道纯度门槛：第一行业暴露 < 10% 不视为赛道基金（`MIN_SECTOR_EXPOSURE`）。

---

## 5. 推荐引擎（`app/engine/recommend.py`）

漏斗：**LLM 选赛道（宏观）→ LightGBM 赛道内排序 → LLM 终选定论 → 入库**。

- 模型预测目标：**未来 40 交易日绝对收益**（`FORWARD_DAYS=40`，标签版本 `abs_ret_40d_v2`）。
- 入场门槛：预测绝对收益 > 1%（`PROFIT_THRESHOLD`，覆盖申赎成本）。
- 排序权重（`RankingConfig`，GA 可调）：model 0.7 / rel_strength 0.1 / calmar 0.08 / hurst 0.08。
- LLM 终选定论：逐赛道从候选池选 1 只，恒由 LLM 执行（无纯量化降级）。
- 空推荐日：LLM 判"无合适赛道"或量化池空 → 合法决策日，记录 reason_type 区分市场判断 vs 数据故障。
- 降级路径：赛道无候选 → 全市场 Top10（reco_path=degrade，分口径度量）。

---

## 6. 监控引擎（`app/engine/monitor.py`）

对每只 HOLD 持仓每日跑防线链，输出四类信号（优先级 EXIT > WARNING > BUY_MORE > HOLD）：

| 防线 | 判定 | 与 RBSA 的关系 |
|---|---|---|
| R1 EMA60 趋势 | 净值连续 2 日 < EMA60 → EXIT | 纯净值 |
| R2 风格漂移 | **当前 RBSA 第一行业 vs 买入时**：行业切换 或 权重降 > 15pp → EXIT | **直接消费 RBSA** |
| R3 赛道锚点 | 当前 RBSA 第一行业 vs 推荐时 LLM 赛道 | **直接消费 RBSA** |
| R4 板块优势 | 赛道相对优势转弱 | 间接 |
| R5 模型信号 | 模型预测转负（score < 0） | 模型 |
| LLM 逻辑证伪 | buy_reason + 最新新闻 → 逻辑链断裂 | 无新披露数据时跳过（R4 报告期守卫） |

**关键点**：R2/R3 两道防线消费的是**季度低频 RBSA**——风格漂移检测实际是"季度对比"，
漂移发生后要等下一份季报才能发现。这是 Style Tracking 改进最直接的落点。

---

## 7. 进化引擎与模型

- **进化引擎**（`app/engine/evolve.py`）：每日结算待定推荐（满 40 日窗口）+ 月度质量度量
  + LLM 元分析沉淀教训（`evolution_insights`，置信度衰减、回流 prompt）+ GA 排序权重寻优
  + 否决反事实监控。
- **模型**（`app/model.py`）：LightGBM L1 回归，固定 50 轮，num_leaves=16，**每周重训**（7 天间隔），
  训练/推理解耦（每日用已保存模型 + 最新特征）。训练样本：2000 只代表性基金、面板采样步长 20 天、
  12 个月滚动窗口、时间衰减权重（半衰期 90 天）、walk-forward 切分（前 80% 训练 / 后 20% 验证）。
- **质量度量**（`app/engine/quality.py`）：IC、赚钱胜率、裁决损耗（LLM 选中 vs 候选池均值 40 日收益差）、
  分路径（sector/degrade）口径、端到端 P&L。

---

## 8. 已知技术债与阻塞项（对齐 `docs/quant-overhaul-plan.md`）

| # | 问题 | 影响 | 与 Style Tracking 的关系 |
|---|---|---|---|
| 1 | **历史持仓缺失**：`fund_holdings` 每基金仅最新一期季报快照 | 持仓变化率/漂移特征需两期对比，当前无法做 | 同源的"静态季报"问题 |
| 2 | **板块历史日线缺失**：`sector_daily_snapshot` 仅 17 天 | 无法做板块级时序回归/动量回填 | **净值反推的自变量，最大数据缺口** |
| 3 | **RBSA 是持仓聚合而非净值回归** | 风格漂移检测是季度低频，吃 15~45 天鱼尾 | **Style Tracking 要替换的核心** |
| 4 | 阶段 4 LLM 结构化产业信号：`macro_news` 仅 1 行历史 | 无法回测验证 LLM 信号增益 | 间接 |
| 5 | 价格类止损（2×ATR 等）回测证明结构性负贡献 | 已降级为警示 | 无关 |
| 6 | 依赖刻意极简：无 scipy/sklearn | 新增回归/滤波需手写或破例加依赖 | Style Tracking 的算法实现需决策 |

---

## 9. 与"净值反推实时持仓（Style Tracking）"直接相关的现状盘点

> 这是针对拟议改进的聚焦结论，供方案研究。

**已就绪的资产：**
- ✅ 基金日频净值全量历史（11.5M 行 / 12,675 只 / 11 年）——回归因变量
- ✅ 宽基指数日线（沪深300/上证，2010 起）——基准/现金因子
- ✅ 股票→申万二级行业映射（`stock_industry_map`）——季报持仓可映射到行业
- ✅ 季报重仓股（最新一期）——真值锚点（验证"反推能否还原已知持仓"）
- ✅ 东财板块 K 线接口可用（已验证）——板块历史日线可回填

**缺失的资产（按优先级）：**
1. **板块历史日线**（只有 17 天）——回填 135 行业 × ~600 交易日是新 fetcher 工程，模式同 quant-overhaul-plan §3 历史持仓回填
2. **反推算法**（无 Kalman/无弹性网，需新增）——需决策：算法选型 + 是否破例加 scipy
3. **风格跟踪输出表**（`fund_style_track`）——权重向量落库
4. **下游接入**：R2/R3 防线从季度 RBSA 切换为日频 style track；新特征进 LightGBM；Web 面板

**核心验收口径**（对齐 quant-overhaul-plan §7）：终极目标不是"还原持仓"而是赚钱——
用"截至 T 日反推的主线权重"预测 T+1~T+40 收益，walk-forward 回测对比是否有增益；
还原持仓（vs 季报披露）只是中间验证。

---

## 10. 目录结构与关键文件索引

```
app/
├── config.py / database.py / domain.py / model.py / pipeline.py
├── repo/          # 数据访问层：base（宽读）/ decision（决策域）/ nav（净值序列）/ meta_keys
├── data/          # 数据基座：foundation / fetchers / holdings / industry_map / nav / macro / store / ingest
├── features/      # 特征计算：calculator（Hurst/动量/RBSA/状态机）/ sector
├── engine/        # 引擎：sector_pool / recommend / monitor / evolve / quality / ga / insights / valuation / macro_agent
├── llm/           # LLM：client / prompts / context
├── web/           # Web：app / dashboard / charts / quotes / runner
└── utils/         # log / trading_calendar / sina_calendar_decode
backtest/          # 回测研究脚本
scripts/init_db.py # 建库入口
data/schema.sql    # 表结构单一真相源
docs/              # 设计文档 + adr/ + research/ + agents/
```

关键单一来源：
- 表结构：`data/schema.sql`（`app/database._init_schema` 读取）
- 特征列清单：`app/domain.py` FEATURE_COLS / MARKET_COLS
- 领域词汇表：根 `CONTEXT.md`
- 决策 ADR：`docs/adr/`（0001~0006，数据读写不变式 / LLM 单接口 / 上下文装配 / 调度单源 / 读收敛 / 决策读缝）
- Issue tracker：`.scratch/<feature-slug>/`（spec.md + issues/NN-*.md）

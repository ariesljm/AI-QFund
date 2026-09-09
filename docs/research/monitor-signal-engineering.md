# 监控信号工程调研：'规则先行 + LLM 复核'分层架构与信号质量工程

> 调研日期：2026（基于在线检索时的最新来源）
> 用途：AI-QFund 监控引擎（五道防线 → HOLD/WARNING/BUY_MORE/EXIT 四类信号）的架构与参数设计依据存档。
> 说明：所有关键论点均经 web_search 在线核实并给出真实来源链接；全文可抓取验证的标注【全文核实】，仅搜索摘要核实或实践类博客标注【低可信/部分核实】。本文件为研究存档，输出位置为运行时指定的 artifacts 路径，可整体复制至 `docs/research/monitor-signal-engineering.md`。

---

## 摘要

- **分层架构成立且是主流做法**：确定性规则做全量低成本的"高召回"第一道防线，LLM/人只处理规则触发的灰区子集——这同时解决了纯规则的语义盲区、纯 LLM 的时延与成本问题（Clampd 生产实践：LLM 只裁决 5% 灰区调用，账单比纯 LLM 低 20 倍）。关键纪律是**单向性**：LLM 复核只能把边界案例"向上推"（确认/升级），不能推翻规则已明确的决定。
- **信号质量靠"迟滞双阈值 + 确认期/双路径 + 冷却去重"三件套**，Google SRE 的 multiwindow multi-burn-rate 告警是被广泛引用的权威模板（短窗=长窗的 1/12，长短窗同时超标才告警，兼顾检测速度与误报）。
- **项目现状已内置大量正确默认**（LLM 3 次重试、空内容视为失败、复核失败降级 HOLD、数据三级降级、月末进化回流），本调研主要补充：双阈值分流、复核结构化验证轴、熔断与速率上限、状态机防抖/防翻转、EXIT 信号自身的质量度量与回流。

---

## A. 分层信号系统工程实践：规则引擎 + LLM/人复核

### A.1 实践总结（已核实的经典架构）

**A.1.1 规则先行、LLM 裁决灰区（生产系统）** — Clampd 的安全网关是直接同构的案例【全文核实】：
- 规则引擎给每个调用输出 0-1 风险分，用两个阈值切成三区：auto-allow（<0.2）、灰区（0.2–0.75，LLM judge 裁决）、auto-block（>0.75）；LLM 永远不进入高流量路径。
- **LLM judge 只能把边界调用"向上推"（max(rules, model, judge)），不能覆盖规则已明确的阻挡**——否则 prompt 攻击者可以通过 LLM 这条管道"说情"绕过规则。
- **四个 judge 不触发的条件**：开关关闭、规则分落在明确区、冷却期（fail-open 回落到规则结论，避免重试风暴）、每分钟速率上限（成本封顶，超限灰区调用直接按规则放行）。所有旁路都记录结构化事件，保证可观测。
- 成本量化：灰区占比 5% 时，1000 次调用只跑 50 次 LLM；纯 LLM 方案跑 1000 次（20 倍账单）。"LLM judge 是灰区的捕网，不是 ground truth。" [Source](https://clampd.dev/blog/llm-as-judge-when-it-fires)

**A.1.2 双阈值人审路由（学术框架）** — arXiv 2601.05974 把上面的直觉形式化【全文核实】：低阈值 τl 以下自动拒绝、高阈值 τu 以上自动接受、中间区间路由人工复核；在固定复核预算下最大化正确率，得到"准确率–复核量"Pareto 前沿，且收益递减。对 F1 目标，最优区是"高 τu + 低 τl"（右下角）：只接受最确信的正例，把边缘低分也送审。含义：**上阈值（规则直判）要够高，下阈值（进复核）要够低，扩大复核覆盖比提高直判标准更划算**。 [Source](https://arxiv.org/html/2601.05974v1)

**A.1.3 LLM 复核要"预定义验证步骤"，而非自由式复核** — npj Digital Medicine 随机/盲审实验（589 MedQA + 300 NEJM，GPT-4o 与 DeepSeek-V3）【全文核实】：两阶段"初诊 → 验证 → 终诊"中，加入结构化验证步骤后准确性最高提升约 +5.2 个百分点（NEJM/GPT-4o 差分验证 36.13%→41.33%），不确定回答显著减少（如 DeepSeek-V3 MedQA 差分验证 4.67%→0.87%），一致性提升；而自由式策略（ToT、Self-Refine、CoV）在抽样中**没有提升甚至下降**。结论：复核的价值来自**固定的验证轴**（步骤化/差异化交叉检查），不是"再想一遍"。 [Source](https://www.nature.com/articles/s41746-025-02146-4)

**A.1.4 人机互补与延迟转交** — Nature Medicine CoDoC 学习"何时用 AI 结论、何时转临床路径"，比纯 AI 或纯人更准【部分核实】。 [Source](https://www.nature.com/articles/s41591-023-02437-x)；HILAD 框架把"行为摘要→人标注→模型增强"做成闭环【部分核实】。 [Source](https://arxiv.org/html/2405.03234v1)

### A.2 规则与 LLM 的职责边界（原则）

1. **规则层**：全量、确定性、零成本、可解释、高召回。负责"教科书案例"（Clampd 表述）——本项目即：止损阈值、回撤、模型分转负、RBSA 权重跳变这类可精确定义的数值条件。
2. **LLM 复核层**：只处理规则触发的预警子集，负责"语义灰区"——赛道逻辑是否被证伪、新闻是否实质利空、持仓是否印证风险（本项目 LLM 逻辑证伪的三条轴）。
3. **单向性纪律**：复核可以确认或升级信号，**不可以把规则已判的 EXIT 改回 HOLD**；若复核认为规则误报，输出"证据不足/维持"，交由冷却期观察或人工。理由：防止模型被 prompt 说服而系统性放松防线（Clampd："don't make the LLM the floor"；AI Secured by Design 也指出 judge 本身是概率性的、可被愚弄，只能"inform"不能"decide"）。 [Source](https://aisecuredbydesign.io/core/controls/)
4. **分层职责小结（三明治）**：Guardrails（规则前置）→ Reviewing controls（LLM 复核，二道意见）→ Human oversight（用户最终决策）。本项目用户即"人"，信号必须给出证据链让用户可复核。

### A.3 LLM 复核失败（超时/格式错/不可用）的兜底设计

**A.3.1 三类失败模式要分别对待**（tianpan 生产指南【全文核实】）：
- **Hard down**（5xx/超时/断连）：易检测。
- **Brownout**（延迟升高、间歇 429/529）：阈值检测问题，**naive 重试会放大限流**。
- **Silent degradation**（200 OK 但输出变差/变空）：常规监控抓不到，只有输出质量抽检能发现（文中引 Anthropic 2025 年 8-9 月事故：接口全程 200，但响应质量被硬件故障降低）。 [Source](https://tianpan.co/blog/2026-07-02-the-model-api-is-tier-0-now-design-the-degraded-mode)

**A.3.2 标准做法清单**（Boundev/CallSphere 等聚合【低可信，实践类】）：
- 激进超时 + 指数退避 + 抖动重试，只重试瞬时错误；失败聚集时熔断（circuit breaker），冷却后探测恢复。 [Source](https://www.boundev.ai/blog/llm-timeouts-retries-graceful-degradation-saas)
- 降级选项四选一：**缓存旧结论**（需置信度阈值+TTL，防止把陈旧答案当新鲜答案）、**回退到规则实现**（本项目天然有：复核失败→取规则结论）、**排队延迟处理**（异步）、**诚实告知不可用**。 [Source](https://tianpan.co/blog/2026-07-02-the-model-api-is-tier-0-now-design-the-degraded-mode)
- "没评估过的 fallback 链不是 fallback，是另一个产品"——多模型备援必须逐任务预评估质量。

### A.4 对本项目的具体建议（A 节）

1. **规则层输出统一风险分，双阈值分流**（对齐 arXiv 双阈值 + Clampd）：
   - 每个防线输出 0-1 触发强度 → 聚合为 per-fund 风险分。
   - τ_low（如 0.30，进 LLM 复核）与 τ_high（如 0.85，规则直判 EXIT，不经 LLM——对应单日极端事件，如单日跌幅 > 5%）。
   - τ_low 宁低勿高（扩大复核覆盖，arXiv：低 τl 对 F1 增益大）；τ_high 宁高勿低（保持复核的纠错价值）。
2. **复核单向性落地**：`final_signal = max(rules_signal, llm_confirmed)`；LLM 输出"证伪成立→确认 EXIT / 证伪不成立→维持规则信号"，禁止输出"改回 HOLD"。复核失败时取规则结论（与项目现状"逻辑证伪失败降级为 HOLD"一致，但要明确：仅当规则未触发 EXIT 时降级为 HOLD；规则已触发时维持规则结论）。
3. **复核结构化**（npj 证据）：prompt 固定三条验证轴（赛道逻辑自洽性 / 新闻实质性与时效 / 持仓交叉验证），每轴输出 {结论, 引用证据, 置信度}，最终 JSON：`{verdict, confidence, evidence[], checks[{axis, pass, note}]}`。JSON schema 校验失败=复核失败（silent degradation 的抓手）。
4. **兜底四级**：
   - ① 3 次重试 + 空内容/解析失败视为失败（项目已有，补 schema 校验）；
   - ② 熔断：连续失败 ≥ 5 次进入 15 分钟冷却，冷却期跳过复核、直接取规则结论并记 bypass 事件；
   - ③ 每轮管线复核调用上限（如 20 次/日），超限只保最高风险基金；
   - ④ 全部 bypass 写结构化日志（Clampd："旁路必须可观测"），Web 面板展示"该信号未经 LLM 复核"。
5. **不建议多模型自动 failover**（tianpan 警示：语义不一致、prompt 效果不可迁移）；如要备援，先离线评估备选模型的证伪任务质量。

---

## B. 信号质量工程：防抖动、升级机制与 BUY_MORE 条件

### B.1 避免信号抖动/翻转的机制

**B.1.1 迟滞（hysteresis）双阈值** — 进入与解除用不同阈值，制造"状态保持"区间【低可信，实践类但广泛使用】：价格/指标越过进入阈值才触发，回落到更宽松的解除阈值才复位。EasyLanguage 的 200-SMA 趋势过滤器教程明示"用两条 SMA 构建迟滞区间来消除 whipsaw" [Source](https://easylanguagemastery.com/building-strategies/building-a-better-trend-filter-2-2/)；TradingView 的 Deadband Hysteresis 过滤器用"基线只在偏离足够大时才移动"抑制微噪声 [Source](https://www.tradingview.com/script/TPvNyPwv-Deadband-Hysteresis-Filter-BackQuant/)；LuxAlgo 把"信号卫生"归纳为：收盘确认（防 repaint）、follow-through 确认、debounce（一个市场事件只产生一个信号）、迟滞。 [Source](https://www.luxalgo.com/library/concept/signal-hygiene/)

**B.1.2 确认期/连续 N 天** — 行业表述是"持久性过滤"（persistence filter）：IBM（ICSE-NIER 2022）提出只保留**持久异常**（频繁、密集、持续），瞬态异常应被后验抑制——"只有持续存在的异常对 SRE 有用"【部分核实】。 [Source](https://research.ibm.com/publications/utilizing-persistence-for-post-facto-suppression-of-invalid-anomalies-using-system-logs) 风力发电机监测论文同样发现：报警系统的实际行为取决于持久性过滤器的参数，而非检测模型本身【部分核实】。 [Source](https://www.mdpi.com/1424-8220/26/12/3896) **重要反方提醒**：QuanterLab 指出"给策略加确认过滤器"是最常被建议的修法，但有时只是对糟糕回测的 curve-fitting 伪装——确认期/过滤器必须由样本外表现证明其价值，而非"加了就更好"。 [Source](https://quanterlab.com/articles/indicators-confirmation-filters)

**B.1.3 权威模板：Google SRE 的 multiwindow multi-burn-rate 告警**【全文核实】——这是"防抖 + 分级"最被引用的工程范式：
- 用**两个窗口同时超标**才告警：长窗捕捉持续性问题（好召回）、短窗保证快速检测与快速复位（短窗=长窗的 1/12）。
- 分级：Page（1 小时窗+5 分钟窗，14.4x burn，2% 预算）、Page（6h+30m，6x，5%）、Ticket（3 天+6h，1x，10%）。"快坏快告、慢坏慢告"。
- 低流量场景警示：样本太少时单次事件即触发极端 burn rate，会误报——**对本项目"几十只基金"正是低流量场景**，需用最小样本/连续性条件对冲。 [Source](https://sre.google/workbook/alerting-on-slos/)

**B.1.4 事件去重/冷却（alert dedup & cooldown）** — 权威文档化的做法：dedupe key 应基于**业务身份**（如 fund_id+防线+信号类别），而不是行 ID 或消息文本；配冷却窗口与过期时间，防止同一问题重复轰炸（"one incident = one page, not 37"）【全文核实 PagerDuty 原则 + 文档化做法】。 [Source](https://response.pagerduty.com/oncall/alerting_principles/) [Source](https://anriku.com/en/patterns/deduplication-cooldowns-and-expiry-in-operational-alerting) [Source](https://openobserve.ai/docs/user-guide/analytics/alerts/)

### B.2 WARNING 升级机制：持续 N 天自动升级 EXIT 的实践与风险

- **实践侧**：PagerDuty 升级策略=有序层级 + 每级超时（"N 分钟未确认→升到下一级"），策略耗尽则通知兜底【官方文档】。 [Source](https://support.pagerduty.com/main/docs/escalation-policies) Grafana 升级链同理。 [Source](https://grafana.com/docs/grafana-cloud/alerting-and-irm/irm/configure/escalation-routing/escalation-chains) SRE 的"慢速持续烧预算→Ticket 级告警"本质就是"持续 N 天 → 升级"（3 天窗 10% 预算）。 [Source](https://sre.google/workbook/alerting-on-slos/)
- **风险**：
  1. **纯慢路径会错过急跌**：SRE 明确指出单一长窗的缺点（低召回、复位慢）。若 EXIT 只走"连续 N 天"，单日暴跌会漏报。→ 必须"快路径 + 慢路径"并存。
  2. **升级后反复横跳**：升级触发后若条件短时解除又复现，会来回翻转。→ 升级/降级都要冷却与对称解除条件。
  3. **确认期参数过拟合**：N 天这个数字易被回测调参调成"事后最优"（QuanterLab 警示）。
  4. **陈旧数据污染确认期**：连续 N 天计数必须基于"净值已更新的交易日"，否则停牌/数据缺失会被误算进确认期。

### B.3 BUY_MORE 信号合理使用条件

- **反面教训（平均向下）**：Investopedia 定义"摊低平均成本"=价格下跌时加仓，但可能是在滚雪球式放大亏损仓位；Pomegra 直言平均向下是"行为陷阱"，掩盖了破了的逻辑或错误的初始仓位，数学上只有确定会收复新成本价才成立；Fidelity 的仓位管理建议聚焦"何时止损/何时止盈"而非下跌加仓。 [Source](https://www.investopedia.com/ask/answers/04/052704.asp) [Source](https://pomegra.io/learn/library/track-c-strategies/first-portfolio/chapter-13-adding-to-vs-replacing-positions/averaging-down-the-trap) [Source](https://www.fidelity.com/learning-center/trading-investing/trading/managing-positions)
- **正面条件**：加仓应挂钩"逻辑完好 + 估值支持 + 风险受控"（TIKR/EquityAnalysisLab 的加仓框架），即模型预测分数门槛 + 无风险信号，而非价格便宜。 [Source](https://www.tikr.com/blog/how-to-decide-when-to-add-to-a-winning-stock) [Source](https://equityanalysislab.com/en/investor-decision-making/buy-process/when-to-add-to-a-stock-position/)
- 结论：**BUY_MORE 应挂钩模型分数门槛**（横截面 Top 分位或绝对阈值），并禁止在 WARNING/EXIT 状态触发。

### B.4 对本项目的具体建议（B 节）

1. **参数设定原则**：
   - 数值阈值先按历史分布定：回撤用"进入 -8% / 解除 -5%"这类迟滞对；模型分数"进入 <0 / 解除 >+0.2"；单日暴跌"≤-5%"走快路径（这些数字需用 `backtest.py` 在历史净值上扫参数网格校准，并保留样本外验证，防过拟合）。
   - 确认期 N 取 2-3 天（温和触发）起步，N 的增减必须有回测证据（QuanterLab 警示）。
   - 所有参数进 `app/domain.py` 单一来源，月末进化引擎可调（与现状一致）。
2. **状态机设计（建议）**：
   - 每基金每防线独立状态：`IDLE → TRIGGERED(未确认) → WARNING(确认) → EXIT`，加 `COOLDOWN`。
   - **双路径**：快路径（单日跌幅/模型骤变 → 规则直判 EXIT，无确认期）；慢路径（温和触发连续 N 天 → WARNING → 第 N+1 天升级 EXIT）。
   - **对称解除**：回到安全区连续 2 天 → 降回上一级或 HOLD；升级/降级各带冷却（5 个交易日），冷却期内只更新状态不推送。
   - **去重**：dedupe key=(fund_id, defense, signal_type)；同一 key 冷却期内不重复推送，仅更新 last_triggered_at。
   - **计数规则**：确认期只累计"净值已更新"的交易日（防停牌污染）。
3. **BUY_MORE 触发条件（全部 AND）**：① 当前非 WARNING/EXIT；② 模型预测分 > 阈值（如横截面 Top 20% 或绝对 > +0.05）；③ 近 20 日无风格漂移告警；④ 距上次 BUY_MORE ≥ 20 交易日（冷却）；⑤ LLM 复核确认赛道逻辑未变。**明确不做"跌了加仓"**。
4. **WARNING 升级 EXIT 的护栏**：升级后 30 日内的走势记录进质量度量（见 D 节），用于校准 N 天参数与 WARNING 阈值；升级事件必须附带"连续 N 天明细"证据链。

---

## C. 信号新鲜度与幂等：净值未更新、陈旧数据、成本模型

### C.1 幂等：净值未更新时避免重复分析

- **工程范式**：通知/告警系统用 dedupe key + 状态机保证"同一事件只处理一次"，处理结果幂等可重放【文档化做法】。 [Source](https://anriku.com/en/patterns/deduplication-cooldowns-and-expiry-in-operational-alerting) [Source](https://www.nazarboyko.com/articles/designing-notification-systems)
- **无数据 = 破阈值**：PagerDuty 官方文档明确"没有收到数据与突破阈值同等对待"（no-data 必须可告警、可区分）【全文核实】。 [Source](https://response.pagerduty.com/oncall/alerting_principles/)

### C.2 数据陈旧/缺失时信号降级策略

- **四态健康模型**：dbt 的 data health signals 用 Healthy / Caution / Degraded / Unknown 四态——**Unknown 明确表示"资源近期未运行/状态未知"**，与"Healthy（好）"区分开【全文核实】。这正是"unknown vs 沿用昨日"问题的答案：未知要显式表达，不能伪装成健康。 [Source](https://docs.getdbt.com/docs/explore/data-health-signals)
- **新鲜度 SLA**：数据新鲜度=最新数据年龄，是独立于可用性/正确性的第三根支柱；定义"新鲜度 SLI/SLO"，滞后超限即触发告警【部分核实，实践类】。 [Source](https://oneuptime.com/blog/post/2026-01-30-freshness-slos/view)
- **降级策略结论**：按净值滞后天数分档降级（见 C.4），不能无限期沿用昨日信号（旧数据会产生新的假 EXIT/BUY_MORE）。

### C.3 几十只基金规模的成本模型

- **LLM 计价结构**：输出 token 单价是输入的 4-8 倍；**batch 接口半价**；**prompt caching 再省 50-90% 输入成本**（2026 年聚合数据【低可信，但多家一致】）。 [Source](https://pecollective.com/tools/llm-pricing-per-million-tokens/) [Source](https://thetokenmart.ai/blog/batch-api-economics-async-inference) 前沿 API 约 $2.5/百万输出 token 起，整体成本 2023→2026 年约降 10 倍【低可信】。 [Source](https://packet.ai/blog/llm-inference-cost)
- **分层架构的成本意义**：LLM 调用数 = 全量 × 规则触发率。规则触发率 10-20% 时，50 只基金每日仅 5-10 次复核调用（Clampd 的 5% 灰区 → 20 倍成本差的同构论证）。 [Source](https://clampd.dev/blog/llm-as-judge-when-it-fires)

### C.4 对本项目的具体建议（C 节）

1. **幂等落地**：`monitor_events` 表加唯一约束 `(fund_id, trade_date, defense)`；管线重跑时命中已存在记录即跳过（含 LLM 调用）；每日"当日已分析基金数/跳过数"打日志。
2. **数据时点标注**：每个信号事件记录 `nav_date`（实际使用的净值日期）；Web 面板与推送展示"信号基于净值日 X"。
3. **降级四态（净值滞后天数分档）**：
   - 滞后 0-1 天（T+1 正常发布）：正常分析；
   - 滞后 2-3 天：沿用昨日信号 + 显式标注"数据陈旧（滞后 N 天）"，不产生新的 EXIT/BUY_MORE；
   - 滞后 ≥4-5 天：该基金信号置 `UNKNOWN`，暂停所有信号生成并告警（无数据=破阈值，PagerDuty）；
   - 数据源恢复后自动回到正常态，无需人工。
   - 与既有三级数据降级（`app/data/fetchers.py`）联动：抓取失败不产生新信号。
4. **成本模型（估算公式）**：
   - 日复核调用数 ≈ 基金数 × 规则触发率 × (1 − 快路径直判比例)。50 只 × 15% × 0.7 ≈ 5 次/日；
   - 单次复核 3-6K token（输入为主）→ 日输入 ~20-30K token、输出 ~2-5K token；
   - 按 2026 年中端 API 价格（约 $1-2/百万输入、$8/百万输出）：日成本约 ¥0.05-0.2，月成本约 ¥2-6（不到一杯咖啡）；若复核用 batch 接口再减半，prompt caching 缓存公共段（宏观摘要/基金画像）再省输入大头。
   - **结论：成本不是约束，稳定性与信号质量才是**；因此建议把工程精力放在熔断/冷却/校验上，而不是省 token。
5. **复核幂等**：同 (fund, nav_date, 触发防线集合) 的复核结论缓存当日复用，避免同一天内重复跑同一逻辑证伪。

---

## D. 监控信号的质量度量与闭环

### D.1 评估"退出信号"本身的准确度

- **框架一（信号质量的两种能力）**：信号质量 = 预测未来收益的统计能力 + 用于仓位时产生的经济价值，两者不同且都要证据（Macrosynergy 官方研究【部分核实：页面 403，核心论点经搜索摘要核实】）。 [Source](https://macrosynergy.com/research/how-to-measure-the-quality-of-a-trading-signal/)
- **框架二（非对称损失）**：ECB 工作论文用"Usefulness"指标显式权衡"假警报 vs 漏报危机"，并计入类别先验与政策制定者的偏好——**漏报真实危机比多报一次假警报代价高，损失是非对称的**【部分核实】。 [Source](https://ideas.repec.org/p/ecb/ecbwps/20131509.html)
- **框架三（及时性）**：G-AMOC 曲线把"检测到事件到用户开始响应"的时间成本纳入评估，标准 ROC/精确率-召回率没有这个维度【部分核实】。 [Source](https://cs.nyu.edu/~neill/papers/gamoc.pdf)
- **框架四（退出效率）**：交易中常见"赢家卖太早、输家拿太久"，用 MFE（最大有利偏移）度量"何时该跑"【低可信，实践类】。 [Source](https://www.tradesviz.com/blog/trade-exit-strategy/)
- **运营侧**：告警质量也要度量（精准率、误报率、可行动性），"无法改进未被度量的东西"【部分核实，实践类】。 [Source](https://oneuptime.com/blog/post/2026-01-30-alert-quality-metrics/view)

### D.2 监控信号与推荐质量度量的衔接（闭环）

- **生产 ML 最佳实践**：AWS Well-Architected MLOE-08 明确要求"跨 ML 生命周期各阶段建立反馈闭环"，SE-ML 最佳实践专章讲"在生产监控与训练管线之间自动化反馈回路"【官方文档】。 [Source](https://docs.aws.amazon.com/wellarchitected/latest/machine-learning-lens/mloe-08.html) [Source](https://se-ml.github.io/best_practices/04-data-pipeline-feedback/)
- **模型漂移监控**：生产模型性能会随时间退化，监控信号（含预测分与实盘结果）是重训与提示词进化的输入【官方/权威文档】。 [Source](https://mlflow.org/articles/why-monitor-model-drift-production/) [Source](https://www.evidentlyai.com/ml-in-production/model-monitoring)
- 本项目已有进化引擎（月末 `evolution_insights` 回流 + GA 寻优），方向与 MLOE-08 一致，缺的是"监控信号本身的效果度量"这一环。

### D.3 对本项目的具体建议（D 节）

1. **EXIT 信号质量指标（按月回测，与推荐质量度量 R1 同口径"未来 20 日绝对收益"）**：
   - **命中率**：EXIT 后 20 日收益 < 0 的比例（应显著高于 50% 才有价值）；
   - **提前量**：EXIT 日 → 其后 60 日内最大回撤日 的天数（中位数；负值=迟报，即已跌破才跑）；
   - **误杀率（机会成本）**：EXIT 后 20 日收益 > +3% 的比例；
   - **漏报率**：未发 EXIT 但 20 日后收益 < -5% 的比例（ECB 非对称损失：漏报权重 > 误杀）；
   - **假警报率**：WARNING 未升级且 20 日后转好的比例（校准 WARNING 阈值与 N 天参数）；
   - **抖动率**：每基金每月信号翻转次数（迟滞/冷却效果的 KPI，目标趋近 0）。
2. **代价矩阵原则**：给"漏报"赋比"误杀"更高的负分（ECB 非对称损失），用它在 backtest 里扫参数，而不是只看命中率。
3. **闭环落地**：
   - EXIT 事件及其后 20/60 日表现，月末写入 `evolution_insights`（"EXIT 教训"），并回流监控/推荐提示词；
   - 被 EXIT 的基金进入下次推荐的**降权/剔除名单**（推荐→监控→推荐的反向流，AWS MLOE-08）；
   - 推荐质量度量（赚钱胜率/期望绝对收益/盈亏比）按监控状态分层统计，防止"已 EXIT 基金"混入推荐口径制造假象；
   - GA 寻优把监控阈值（回撤、确认期 N、冷却天数）纳入可选参数空间。
4. **回测先行**：监控规则（止损参数、确认期、升级路径）先在 `backtest.py` 离线回测再上线，用样本外验证防过拟合（B 节 QuanterLab 警示）。

---

## 已核实 / 未能核实清单

### 已核实（全文抓取验证）
1. Google SRE Workbook《Alerting on SLOs》：multiwindow multi-burn-rate 告警、短窗=1/12 长窗、分级窗口表、低流量场景警示。 [Source](https://sre.google/workbook/alerting-on-slos/)
2. Clampd《LLM-as-Judge: when (and why) we let an LLM grade our security decisions》：双阈值三区、单向 max 组合、四个 judge 旁路条件、成本量化。 [Source](https://clampd.dev/blog/llm-as-judge-when-it-fires)
3. arXiv 2601.05974《A Framework for Optimizing Human-Machine Interaction in Classification Systems》：双阈值人审路由、Pareto 前沿、F1 最优区在高 τu 低 τl。 [Source](https://arxiv.org/html/2601.05974v1)
4. npj Digital Medicine《Two-stage prompting framework with predefined verification steps...》：验证步骤提升准确率（+5.2pp 上限）、降低不确定率、自由式复核无效。 [Source](https://www.nature.com/articles/s41746-025-02146-4)
5. dbt《Data health signals》：Healthy/Caution/Degraded/Unknown 四态。 [Source](https://docs.getdbt.com/docs/explore/data-health-signals)
6. PagerDuty《Alerting Principles》：alert=可行动、无数据=破阈值、分级。 [Source](https://response.pagerduty.com/oncall/alerting_principles/)
7. TianPan《The Model API Is Tier 0 Now...》：三类失败模式（hard down/brownout/silent degradation）、四种降级模式、fallback 需预评估。 [Source](https://tianpan.co/blog/2026-07-02-the-model-api-is-tier-0-now-design-the-degraded-mode)
8. PagerDuty 官方《Escalation Policy Basics》：层级+超时升级。 [Source](https://support.pagerduty.com/main/docs/escalation-policies)

### 已核实（搜索摘要 + 多源交叉，未抓全文）
9. Macrosynergy《How to measure the quality of a trading signal》（页面 403，核心论点经搜索摘要核实：预测力与经济价值是两回事）。 [Source](https://macrosynergy.com/research/how-to-measure-the-quality-of-a-trading-signal/)
10. Nature Medicine CoDoC（互补性延迟转交）。 [Source](https://www.nature.com/articles/s41591-023-02437-x)
11. IBM 持久性抑制瞬态异常（ICSE-NIER 2022）。 [Source](https://research.ibm.com/publications/utilizing-persistence-for-post-facto-suppression-of-invalid-anomalies-using-system-logs)
12. ECB WP《On policymakers' loss function and the evaluation of early warning systems》（非对称损失 Usefulness）。 [Source](https://ideas.repec.org/p/ecb/ecbwps/20131509.html)
13. G-AMOC（及时性评估曲线）。 [Source](https://cs.nyu.edu/~neill/papers/gamoc.pdf)
14. AWS Well-Architected MLOE-08 / SE-ML（反馈闭环）。 [Source](https://docs.aws.amazon.com/wellarchitected/latest/machine-learning-lens/mloe-08.html) [Source](https://se-ml.github.io/best_practices/04-data-pipeline-feedback/)
15. 风格漂移：SSRN《Matter of Style》持仓式漂移度量 [Source](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2024259)、Morningstar RBSA 说明 [Source](https://advisor.morningstar.com/Principia/pdf/StyleAnalysis_FactSheet.pdf)。
16. 加仓纪律：Investopedia 平均向下定义 [Source](https://www.investopedia.com/ask/answers/04/052704.asp)、Pomegra 平均向下陷阱 [Source](https://pomegra.io/learn/library/track-c-strategies/first-portfolio/chapter-13-adding-to-vs-replacing-positions/averaging-down-the-trap)。

### 未能核实 / 低可信（明确标注）
1. **"WARNING 持续 N 天自动升级 EXIT"的直接行业案例文献未找到**——用 SRE 分级告警 + PagerDuty 升级策略 + 持久性过滤（IBM/MDPI）间接支撑；"N 天"数值无权威推荐，需项目回测自定。
2. **BUY_MORE 挂钩模型分数门槛**：无直接量化研究，仅有投资行为学/券商实践类来源（低可信）；阈值（Top 20% 等）为建议值，需回测校准。
3. **LLM 单价数字**（batch 半价、prompt caching 折扣、2026 年每百万 token 价格）为 2026 年聚合博客数据（pecollective/packet.ai/tokenmart），未与官方价目表逐条核对，用于量级估算足够、用于精确预算不足。
4. TradingView/LuxAlgo 迟滞指标、oneuptime/upstat/incident.io 等博客为实践类观点（低可信），仅作操作性参考，不作权威结论。
5. 中国公募基金净值 T+1 发布时滞未单独核实官方文档（项目已有运行经验，视为已知约束）。

---

## 参考文献汇总（按节）

- A 节：[Clampd](https://clampd.dev/blog/llm-as-judge-when-it-fires) ｜ [arXiv 双阈值](https://arxiv.org/html/2601.05974v1) ｜ [npj 两阶段验证](https://www.nature.com/articles/s41746-025-02146-4) ｜ [CoDoC](https://www.nature.com/articles/s41591-023-02437-x) ｜ [HILAD](https://arxiv.org/html/2405.03234v1) ｜ [三层控制](https://aisecuredbydesign.io/core/controls/) ｜ [TianPan 降级模式](https://tianpan.co/blog/2026-07-02-the-model-api-is-tier-0-now-design-the-degraded-mode) ｜ [Boundev 可靠性](https://www.boundev.ai/blog/llm-timeouts-retries-graceful-degradation-saas)
- B 节：[SRE Workbook](https://sre.google/workbook/alerting-on-slos/) ｜ [LuxAlgo 信号卫生](https://www.luxalgo.com/library/concept/signal-hygiene/) ｜ [EasyLanguage 迟滞](https://easylanguagemastery.com/building-strategies/building-a-better-trend-filter-2-2/) ｜ [QuanterLab 确认过滤器](https://quanterlab.com/articles/indicators-confirmation-filters) ｜ [IBM 持久性](https://research.ibm.com/publications/utilizing-persistence-for-post-facto-suppression-of-invalid-anomalies-using-system-logs) ｜ [MDPI 持久性报警](https://www.mdpi.com/1424-8220/26/12/3896) ｜ [PagerDuty 升级策略](https://support.pagerduty.com/main/docs/escalation-policies) ｜ [Grafana 升级链](https://grafana.com/docs/grafana-cloud/alerting-and-irm/irm/configure/escalation-routing/escalation-chains) ｜ [dedup/冷却模式](https://anriku.com/en/patterns/deduplication-cooldowns-and-expiry-in-operational-alerting) ｜ [OpenObserve 冷却](https://openobserve.ai/docs/user-guide/analytics/alerts/)
- B.3 节：[Investopedia 平均向下](https://www.investopedia.com/ask/answers/04/052704.asp) ｜ [Pomegra 陷阱](https://pomegra.io/learn/library/track-c-strategies/first-portfolio/chapter-13-adding-to-vs-replacing-positions/averaging-down-the-trap) ｜ [Fidelity 仓位管理](https://www.fidelity.com/learning-center/trading-investing/trading/managing-positions) ｜ [TIKR 加仓](https://www.tikr.com/blog/how-to-decide-when-to-add-to-a-winning-stock)
- C 节：[dbt 数据健康四态](https://docs.getdbt.com/docs/explore/data-health-signals) ｜ [Freshness SLO](https://oneuptime.com/blog/post/2026-01-30-freshness-slos/view) ｜ [PagerDuty 无数据=破阈值](https://response.pagerduty.com/oncall/alerting_principles/) ｜ [LLM 计价对比](https://pecollective.com/tools/llm-pricing-per-million-tokens/) ｜ [Batch 半价](https://thetokenmart.ai/blog/batch-api-economics-async-inference) ｜ [推理成本趋势](https://packet.ai/blog/llm-inference-cost)
- D 节：[Macrosynergy 信号质量](https://macrosynergy.com/research/how-to-measure-the-quality-of-a-trading-signal/) ｜ [ECB 非对称损失](https://ideas.repec.org/p/ecb/ecbwps/20131509.html) ｜ [G-AMOC](https://cs.nyu.edu/~neill/papers/gamoc.pdf) ｜ [MFE/退出效率](https://www.tradesviz.com/blog/trade-exit-strategy/) ｜ [告警质量度量](https://oneuptime.com/blog/post/2026-01-30-alert-quality-metrics/view) ｜ [AWS MLOE-08](https://docs.aws.amazon.com/wellarchitected/latest/machine-learning-lens/mloe-08.html) ｜ [SE-ML 反馈回路](https://se-ml.github.io/best_practices/04-data-pipeline-feedback/) ｜ [MLflow 漂移](https://mlflow.org/articles/why-monitor-model-drift-production/) ｜ [风格漂移 SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2024259) ｜ [Morningstar RBSA](https://advisor.morningstar.com/Principia/pdf/StyleAnalysis_FactSheet.pdf)

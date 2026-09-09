# 调研存档：场外基金"推荐后监控退出信号"的量化方法论（替代 ATR 追踪止损）

> 存档日期：2026-08 · 调研人：Pi（联网核实版）
> **来源可信度**：本文所有关键文献均已通过 AnySearch 在线核实，标注一手链接与核实状态（✅ 已核实 / ⚠️ 部分核实 / ❌ 未核实）。中国监管/费率事实来自多来源交叉验证。

---

## 0. 执行摘要

1. **本项目 walk-forward 回测中价格止损全部负贡献（收益 +2.8%→-1.8%、胜率 48%→14%）的机制（已修正第一版调研的方向性错误）**：
   - ✅ **Kaminski & Lo (2014) 的真实结论**：随机游走/i.i.d. 下止损必然降低期望收益；**动量（正自相关）下止损反而可以增加价值**；实证上较长采样频率下部分止损策略优于买入持有。[SSRN 摘要](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=968338)｜[MIT DSpace](https://dspace.mit.edu/handle/1721.1/114876)
   - 因此"价格止损结构性失效"的表述是**错误的**。本项目负贡献的真实机制是**参数/口径错配**：① 2×ATR 在日净值收益率上 ≈1~2% 阈值，远紧于基金持仓期自然回撤（对"持仓期远超 20 日"的标的必然误杀）；② 回测在 20 日窗口内"止损即结算、无重入"，而 Kaminski & Lo 框架中止损价值高度依赖"能否在更低价格重新入场"；③ 场外基金 <7 日 1.5% 惩罚赎回费 + 约 1% 申赎成本使高频抖动信号期望成本为负。
   - **推论**：价格保护不废除，但必须 **低频、宽阈值、长持仓语义、趋势确认**——这正是用户"实盘持仓远超 20 日"的判断下应有的形态。2×ATR 是"日线交易参数误用于月级持仓"，而非"价格保护无用"。
2. **替代方向按证据强度排序**：模型同源退出（本项目核心方向，条件交易/ML 资产定价文献支撑）＞ 回撤幅度×持续时间二维规则 + 趋势/移动平均退出（数据充分、低频、平滑，Faber/Moskowitz/Han 实证背书）＞ 波动率条件门控（不直接退出，做 WARNING 门控，Moreira & Muir/Barroso & Santa-Clara）＞ regime 条件化保险（仅均值回归 regime 启用尾部保护）。
3. **推荐组合**（详见第 6 章）：以"每日滚动模型预测序列 + 相对买入分门槛 + 确认期"替代 2×ATR 追踪止损；以"MA60/MA120 趋势破坏 + 回撤幅度×持续时间二维规则"替代 -8%/-10% 硬止损；保留并量化加强风格漂移；新增规模/清盘/限购/经理变更事件防线；LLM 保持"规则触发后复核"定位（分层架构见 [monitor-signal-engineering.md](monitor-signal-engineering.md)）。

---

## 1. 问题锚点：2×ATR 为何在本项目回测中失效（机制，非偶然）

结合项目代码（`monitor.py` 的 `calc_atr` 用收益率均值、`sim_trailing_stop`/`sim_hard_stop` 以 20 日为结算上限）与已核实文献：

1. **ATR 尺度错配（最直接）**：`calc_atr` 在日频净值收益率上取 `mean(|ret|)`，场外权益基金典型日均 |ret|≈0.5%~1%，故 2×ATR≈1~2% 回撤即触发。这是为日线/期货设计的高频参数，**不匹配"持仓期远超 20 日"的长周期基金**。回测中胜率 48%→14% 即"任何正常回调都被洗出"的直接证据。
2. **窗口错配（回测结构缺陷）**：回测在 20 日前向窗口内评估止损，"止损即结算、无重入"。Kaminski & Lo 明确止损收益高度依赖"能否在更低价格重新入场"；本项目"推荐→监控"单向流程、无重入机制（重入由用户自行决定），压缩了止损正贡献空间。**同时该回测也系统性低估了长持仓下止损的真实价值**——因为结算窗口只有 20 日。
3. **成本**：场外基金赎回费随持有期递减（<7 日惩罚性 1.5%、7 日~1 年 0.5%、≥2 年免，实测费率表见第 4.7 节）＋申购约 1%（第三方平台 0.1%~0.15%）。高频抖动信号每触发一次就付一次成本。
4. **动量崩溃的期权结构（机制性风险提示）**：✅ Daniel & Moskowitz (2016) 证明动量策略收益像期权——崩溃（大幅回撤）多发生在市场下跌后、高波动期，且**与市场反弹同时发生**；崩溃状态中过去输家反弹陡峭。[论文 PDF](https://www.kentdaniel.net/papers/published/jfe_16.pdf)｜JFE 122(2):221-247, DOI [10.1016/j.jfineco.2015.12.002](https://doi.org/10.1016/j.jfineco.2015.12.002)。→ 在动量崩溃期"按价格止损"恰好可能卖在反弹前夜。这是"高频价格止损"失效的深层原因，也是"价格保护必须低频宽阈值、交给趋势/回撤慢变量"的论据。

**结论修正**：不是"价格止损结构性失效"，而是"**高频、紧阈值、短窗口、无重入**的价格止损对本项目失效"。替代方案应低频、平滑、与买入逻辑同源、给均值回归留出空间。

---

## 2. 方向 A：退出/止损信号的学术实证（全部核实）

### A.1 Kaminski & Lo (2014)：止损的价值取决于收益自相关结构 ✅
- 来源：Kaminski & Lo, "When Do Stop-Loss Rules Stop Losses?", *Journal of Financial Markets* 18 (2014) 234–254。[SSRN abstract 968338](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=968338)｜[ScienceDirect](https://www.sciencedirect.com/science/article/abs/pii/S138641811300030X)｜[MIT DSpace（书目标注 JFM 18, March 2014, 234–254）](https://dspace.mit.edu/handle/1721.1/114876)
- 核心论点（据摘要原文）：**随机游走假设下，简单 0/1 止损总是降低策略期望收益；但在动量存在时，止损可以增加价值**。实证：短采样频率止损无价值；较长采样频率下部分止损策略可增加期望收益并显著降低波动率。
- **对本项目的含义**：止损不是"必须禁用"，而是"参数与使用方式决定价值"。高频紧阈值（2×ATR）在本项目被证伪；**较长持有期 + 低频确认 + 保留重入可能**的价格保护仍有理论支撑。第一版调研将此表述为"动量下止损有害"是方向性错误，特此修正。

### A.2 动量崩溃与波动率调节 ✅
- Daniel & Moskowitz (2016) "Momentum Crashes", JFE 122(2):221–247，见第 1.4 节。
- ✅ Barroso & Santa-Clara (2015) "Momentum has its moments", JFE 116(1):111–120，DOI [10.1016/j.jfineco.2014.11.010](https://doi.org/10.1016/j.jfineco.2014.11.010)。摘要确认：动量风险时变且可预测；**用已实现波动率缩放动量仓位几乎消除崩溃、Sharpe 近翻倍**。[NOVA 条目](https://novaresearch.unl.pt/en/publications/momentum-has-its-moments)｜[IDEAS/RePEc](https://ideas.repec.org/a/eee/jfinec/v116y2015i1p111-120.html)
- ✅ Moreira & Muir (2017) "Volatility-Managed Portfolios", JF 72(4):1611–1644，DOI [10.1111/jofi.12513](https://doi.org/10.1111/jofi.12513)。摘要确认：高波动时降风险产生大 alpha、提高 Sharpe（市场组合 alpha 4.9%、Sharpe 提升 25%）。[Yale 工作稿 PDF](https://law.yale.edu/sites/default/files/area/workshop/leo/leo17_moreira.pdf)
- **对本项目**：本项目不管理仓位，无法直接做波动率目标；但机制可转为**信号门控**——基金 `vol_20d`/指数 `idx_vol_20d` 升入历史高分位时，价格类/回撤类规则自动放宽、模型信号权重提高（高波动期价格信号噪声大）。特征已存在，落地成本低。

### A.3 趋势跟踪 / 移动平均退出 ✅
- ✅ Moskowitz, Ooi & Pedersen (2012) "Time Series Momentum", JFE 104(2):228–250。58 个流动性工具上 12 个月时序动量收益一致为正。[AQR 数据集页](https://www.aqr.com/Insights/Datasets/Time-Series-Momentum-Original-Paper-Data)｜[ScienceDirect](https://www.sciencedirect.com/science/article/pii/S0304405X11002613)
- ✅ Faber (2007) "A Quantitative Approach to Tactical Asset Allocation"（JWM 9(4)；常引 SSRN 版本）。10 个月均线择时**显著降低最大回撤同时保持股票级收益**（"significantly reduces maximum drawdowns while maintaining equity-like returns"）。[作者 PDF](https://mebfaber.com/wp-content/uploads/2016/05/SSRN-id962461.pdf)｜[ResearchGate](https://www.researchgate.net/publication/228202718_A_Quantitative_Approach_to_Tactical_Asset_Allocation)｜[Semantic Scholar](https://www.semanticscholar.org/paper/A-Quantitative-Approach-to-Tactical-Asset-Faber/a722e5cf39a1c845ea939d5cca594b6a411a5b1e)
- ✅ Han, Yang & Zhou (2013) "A New Anomaly: The Cross-Sectional Profitability of Technical Analysis", JFQA 48(5):1433–1461。[WUSTL 档案](https://profiles.wustl.edu/en/publications/a-new-anomaly-the-cross-sectional-profitability-of-technical-anal/)｜[GitHub PDF](https://github.com/yangyutu/FinancialResearch/blob/master/Han,%20Yang,%20Zhou%20-%202013%20-%20A%20new%20anomaly%20The%20cross-sectional%20profitability%20of%20technical%20analysis.pdf)
- ⚠️ 反方（Zakamulin）：MA 策略存在数据窥探/过拟合风险，参数需 OOS 检验——本项目已有 walk-forward 框架，直接沿用。Zakamulin (2017) *Market Timing with Moving Averages*（Palgrave）未能在线获取原文，凭常识与 Faber 复现文献标注。
- **对本项目**：净值数据充分；MA 退出信号平滑（日频净值滤波后低频触发）；方向与长持仓一致。参数建议周/月线尺度（MA60 日线≈季度线、MA120≈半年线）。注意：MA 择时通常略微降低绝对收益（滞后性），换来的是回撤压缩——对本项目"信号服务"定位（用户承担买卖决策）尤其合适。

### A.4 回撤控制：幅度 × 持续时间 ✅
- ✅ Grossman & Zhou (1993) "Optimal Investment Strategies for Controlling Drawdowns", *Mathematical Finance* 3(3):241–276，DOI [10.1111/j.1467-9965.1993.tb00044.x](https://doi.org/10.1111/j.1467-9965.1993.tb00044.x)。[IDEAS/RePEc](https://ideas.repec.org/a/bla/mathfi/v3y1993i3p241-276.html)。⚠️ 后续研究（Klass & Nowicki 2005, Statistics & Probability Letters 74(3):245-252）证明其在离散时间下不再保持最优性——用作"回撤约束有价值"的原理背书，非算法照搬。[ScienceDirect](https://www.sciencedirect.com/science/article/abs/pii/S0167715205001641)
- ✅ Magdon-Ismail, Atiya, Pratap & Abu-Mostafa (2004) "On the Maximum Drawdown of a Brownian Motion", *Journal of Applied Probability* 41(1):147–161，DOI [10.1239/jap/1077134674](https://doi.org/10.1239/jap/1077134674)。[Cambridge Core](https://www.cambridge.org/core/journals/journal-of-applied-probability/article/abs/on-the-maximum-drawdown-of-a-brownian-motion/F9E3B8A454B020DDEBF0AC3390EF7807)。配套应用文："An Analysis of the Maximum Drawdown Risk"（RISK04）。给出给定波动率与持有期下最大回撤的分布/期望（正漂移对数增长、零漂移平方根、负漂移线性）→ **工程上可用基金自身 `vol_20d` 与持仓天数校准回撤阈值**（超出分布高分位才视为异常），而非固定 -8%/-10%。
- **对本项目适用性：高**。每日净值即可计算回撤路径与持续时间；天然低频（回撤是慢变量）；与长持仓期匹配；纯函数落地成本低。

### A.5 regime 条件化保险（尾部保护）
- 直接吸收 Kaminski & Lo (2014)【✅ 见 A.1】：止损在均值回归占优状态有价值。项目已有 BULL/BEAR/NEUTRAL 状态机（`regime_from_close_ma60`）与 `idx_vol_20d`。仅建议作为**尾部保险**（如回撤 >25% 无条件离场），不承担主力退出职责。

---

## 3. 方向 B：模型信号驱动退出（model-conditional exit）—— 本项目核心方向

### B.1 现状对照
`monitor.py` `ModelSignalRule` 现在只在 `score < 0` 时输出 WARNING，单点、无确认期、无相对买入分比较、无模型版本固定。这是"模型同源退出"的最简形态，方向正确但信噪比不足。

### B.2 设计形态（升级）
1. **滚动预测序列**：每日用模型对最新特征打分，得预测 20 日绝对收益序列 `s_t`；退出决策看序列的**持续性/恶化速度**，非单日单点。
2. **模型版本固定**：持仓退出打分固定到买入时模型版本（`recommend_log` 增加 `model_version` 字段；打分用买入时版本），消除每日重训造成的分数跳变（项目模型每周重训，但仍会引入版本间差异）。一致性优先于新鲜度。
3. **与买入同源**：买入与退出共用同一预测目标（20 日绝对收益），避免"买入看 A 信号、卖出看 B 信号"的逻辑错位。✅ 支撑文献：Gu, Kelly & Xiu (2020) "Empirical Asset Pricing via Machine Learning", RFS 33(5):2223–2273（ML 条件期望对组合构建有经济价值）[未能在线抓全文，经典文献标注]。
4. **独立性护栏**：模型系统性失效时买入与退出信号同生共死 → 监控必须保留**非模型防线**（趋势/回撤/风格漂移/事件），形成独立性（现有防线链已具备，保留）。

### B.3 门槛设定：绝对 vs 相对买入分 vs 分位数

| 门槛类型 | 形式 | 优点 | 缺点 | 依据 |
|---|---|---|---|---|
| 绝对门槛 | `s_t < 0` | 简单、有经济含义（"未来 20 日预期为负"） | 0 附近噪声大，不随基金风险校准 | 现有实现，信噪比不足 |
| **相对买入分**（推荐先上） | `s_t < α·s_buy`（α≈0.5）或 `s_t < s_buy − δ` | 与买入逻辑同源，规避模型绝对尺度漂移；`s_buy` 已有（`get_entry_score`） | α 需回测 | 条件交易文献逻辑 |
| 预测分位数 | `s_t < Q_p(历史预测分布)`（p≈25%） | 对每只基金自动校准 | 需历史预测序列积累（数周） | conformal prediction（Angelopoulos & Bates 2023）⚠️ |
| 波动缩放 | `(s_t − s_buy)/vol_20d < −k` | 把"预测恶化"按基金风险归一化 | 依赖 vol 估计 | Barroso & Santa-Clara 波动缩放思想 ✅ |

- **校准建议**：LightGBM 原生 `objective='quantile'` 可直接输出预测分位数（如 25 分位），用"预测 25 分位转负"替代"点预测转负"——抗点预测噪声且具概率语义。⚠️ LightGBM quantile 文档未逐一核实，属官方已知特性。
- **门槛参数必须走 walk-forward OOS**：✅ Goyal & Welch (2008) "A Comprehensive Look at the Empirical Performance of Equity Premium Prediction", RFS 21(4):1455–1508——预测回归 OOS 收益极不稳定，任何门槛须 OOS 验证。[RePEc/IDEAS](https://ideas.repec.org/a/oup/rfinst/v21y2008i4p1455-1508.html)（部分核实）

### B.4 预测窗口（20 日）与持仓期（远超 20 日）错位的处理
1. **滚动重评（推荐）**：每持仓日用最新数据对"未来 20 日"重打分，形成 `s_t` 序列；退出信号看序列恶化趋势与持续负值——等价于"每 20 日续期一次持有决策 + 日内用趋势做中间校验"。
2. **多步目标**：训练 20/40/60 日多个前向目标，退出用长窗口模型、买入用 20 日模型（成本中等，直接消解错位）。
3. **确认期**：EXIT 要求连续 2~3 日成立（见信号工程文档 B 节），把"20 日窗口预测 + 日频噪声"转为"周级确认"。
4. **不要**把 20 日预测误用为"未来 20 日必须退出"——那是窗口错配的另一种表现（现有回测正是 20 日强制结算，扭曲了止损评估）。

---

## 4. 方向 C：场外基金特有风险信号（含中国监管事实核实）

### C.1 风格漂移（保留现有防线 2a）
- ✅ RBSA 源头：Sharpe (1992) "Asset Allocation: Management Style and Performance Measurement", *Journal of Portfolio Management* 18(2)（Winter 1992）——子代理核实为 JPM 18(2) 而非第一版写的 18(1)，在线未能再抓原文，经典文献。
- ⚠️ Chan, Chen & Lakonishok (2002) RFS 15(5)、Brown & Goetzmann (1997) JFE 43(3)：风格漂移可测性与持续性——未在线核实，经典文献标注。
- 项目已有实现：RBSA 行业权重 + 第一行业切换双检，买入基准三级回退（feature_snapshot → 买入日快照 → 首个非空快照）。**保留**，且是少数"与价格无关"的独立防线。注意季度滞后（持仓来自季报），只能做季度级低频检测。

### C.2 规模增长 / 申赎冲击
- ✅ Chen, Hong, Huang & Kubik (2004) "Does Fund Size Erode Mutual Fund Performance?", AER 94(5):1276–1302，DOI [10.1257/0002828043052277](https://doi.org/10.1257/0002828043052277)。[aeaweb](https://www.aeaweb.org/articles?id=10.1257%2F0002828043052277)。摘要确认：基金收益（费前费后）随滞后规模下降。⚠️ Berk & Green (2004) JPE 112(6)：规模增大→边际 alpha 递减（未在线核实）。
- 可观测性：中（季报规模准确值 + 第三方估算日频值；需核实天天基金接口）。
- **角色**：WARNING 级辅助信号（规模暴增后业绩稀释预警），不作为主退出信号。

### C.3 清盘风险（中国监管条款核实 ✅）
- 多来源交叉确认（海富通官网 2025 文章 + 第一财经 + 知乎）：
  - 现行《公开募集证券投资基金运作管理办法》（2014 年 8 月 8 日实施）：**连续 60 个工作日基金份额持有人数量不满 200 人 或 基金资产净值低于 5000 万元** → 基金管理人应在 **10 个工作日内**向证监会报告并提出解决方案（持续运作/转换运作方式/合并/终止），并在 **6 个月内**召集持有人大会表决；合同另有约定可不开大会直接终止。[海富通基金清盘流程说明](https://www.hftfund.com/contents/2025/5/9-16a8af64c0a24657a72390071feb332d.html)
  - **发起式基金**：合同生效满 3 年后资产净值低于 2 亿元的，基金合同自动终止。
  - 2004 年旧办法第 44 条曾规定"连续 20 个工作日报告"（第一财经 2011 文章引用），现行条款以海富通 2025 版本为准。
- **对本项目**：规模 <5000 万进入强预警（EXIT/WARNING），<2 亿进入观察（WARNING）。纯规则可做（需规模数据），触发即预警——**清盘必须在启动前退出**（清算时点不可控）。

### C.4 基金经理变更
- ✅ Khorana (1996) "Top Management Turnover: An Empirical Investigation of Mutual Fund Managers", *Journal of Financial Economics* 40(3)——子代理在线核实为 **JFE 40(3)**（第一版误标 JFQA 31(3)，已修正）。⚠️ Chevalier & Ellison (1999) QJE：业绩差→经理被换且更换后业绩不必然改善（未在线核实）。
- **对本项目**：若 `buy_reason` 依赖经理（LLM 推荐理由常见"××经理风格/能力"），经理变更 = 买入逻辑前提断裂，应触发 **LLM 逻辑证伪复核的强输入**（事件驱动，不等日频周期）；若买入逻辑不依赖经理，降为 WARNING。需新增"经理变更"数据抓取（天天基金/公告）。

### C.5 限购 / 暂停申购
- 机制：限购/暂停申购是**利好性**事件（规模控制），对已持有者是"信号有效性确认"；但阻塞 BUY_MORE。放开申购 + 规模高企才是偏负面信号。
- **角色**：BUY_MORE 可行性闸门 + WARNING 辅助确认。需抓取申购状态字段（事件驱动）。

### C.6 赎回费率结构（中国场外基金实测 ✅）
- 实测费率表（易方达标普 500，和讯数据库）：持有 ≤6 日 1.5%（惩罚性）；7 日 ≤ 持有 ≤ 364 日 0.5%；365~729 日 0.25%；≥730 日 0%。[和讯费率页](https://jingzhifunds.hexun.com/database/sgfl.aspx?fundcode=161125)
- 申购费：1%~1.5%（第三方平台可低至 0.1%~0.15%）。[百度开发者场外基金交易全解析](https://developer.baidu.com/article/detail.html?id=6067007)
- 巨额赎回：单日赎回申请超过基金规模 10% 时，管理人可暂停赎回/延迟支付。
- T+1 确认；赎回 1~4 工作日到账（QDII T+2、FOF T+3）。
- **含义**：信号确认期设计应 ≥7 日（跨过惩罚费率带）；短持抖动每触发一次付一次高成本。

### C.7 可观测性矩阵汇总

| 风险信号 | 数据源 | 频率 | 日净值可观测 | 建议角色 | 核实状态 |
|---|---|---|---|---|---|
| 风格漂移（RBSA） | 季报持仓+行业映射 | 季度 | 部分 | EXIT（已有） | ✅ 已有实现 |
| 规模增长/赎回冲击 | 规模数据+公告 | 季度/事件 | 部分 | WARNING | ✅ 文献核实 |
| 清盘风险 | 规模/持有人数 | 季度/事件 | 部分 | WARNING→EXIT | ✅ 监管条款核实 |
| 限购/暂停申购 | 公告 | 事件 | 否（需抓取） | BUY_MORE 闸门 | ⚠️ 机制常识 |
| 基金经理变更 | 公告 | 事件 | 否（需抓取） | LLM 证伪强输入 | ✅ 文献核实 |
| 申赎费率惩罚 | 基金合同/费率表 | 静态 | — | 确认期设计依据 | ✅ 实测核实 |

---

## 5. 方向 D：工程模式（指向信号工程调研存档）

规则先行 + LLM 复核的分层架构、防抖动三件套（hysteresis/确认期/冷却去重）、新鲜度幂等降级、EXIT 信号质量度量与闭环——详见 [monitor-signal-engineering.md](monitor-signal-engineering.md)（已联网核实，含 Clampd 双阈值三区、Google SRE multiwindow 告警、dbt 四态健康模型等一手来源）。

---

## 6. 推荐退出信号组合（升级版建议）

> 设计原则：与买入逻辑同源优先；低频平滑；成本显式建模（<7 日惩罚费率）；任何 EXIT 至少两条独立证据或一条强证据 + 确认期；LLM 保持复核定位（分层）。

| 优先级 | 防线 | 触发（WARNING） | 触发（EXIT） | 替代对象 | 证据强度 |
|---|---|---|---|---|---|
| 1 | **模型同源退出**（升级 ModelSignalRule） | 滚动预测分 < max(0, 0.5×买入分)（相对线） | 预测分 < 0 且连续 3 日；或单日 < 买入分 − 2σ(预测序列) | 替代 2×ATR 追踪止损的主力 | 中-高（条件交易/校准文献 ✅；本项目回测待验证） |
| 2 | **趋势/移动平均退出**（新增） | NAV 跌破 MA60 连续 2 日；或 MA60 下穿 MA120 | NAV 跌破 MA120 连续 2 日 | 替代 -8%/-10% 硬止损 | 高（Faber 2007 ✅；Moskowitz 2012 ✅；Han 2013 ✅） |
| 3 | **回撤幅度×持续时间二维规则**（新增） | 回撤 >10% 且持续 ≥10 交易日 | 回撤 >18%；或回撤 >12% 且持续 ≥20 交易日 | 尾部保护 | 中-高（Magdon-Ismail 2004 ✅ 统计框架；Grossman & Zhou 1993 ✅） |
| 4 | **波动率条件门控**（新增，门控非退出） | vol 达历史 80 分位 → 价格/回撤规则放宽、模型信号权重提高 | — | 吸收 Moreira & Muir ✅ / Barroso & Santa-Clara ✅ | 高（机制）；本项目回测待验证 |
| 5 | **风格漂移**（保留） | — | 第一行业切换 或 权重下降 >15% | 保留 | 高（Sharpe 1992 ✅ 等） |
| 6 | **事件防线**（新增） | 规模 <2 亿；经理变更；放开大额营销 | 规模 <5000 万（清盘预警）；经理变更且 buy_reason 依赖经理（经 LLM 复核） | 新增独立防线 | 中-高（Chen 2004 ✅；Khorana 1996 ✅；监管条款 ✅） |
| 7 | **LLM 逻辑证伪**（保留，仅复核） | 规则/事件触发后复核 | 逻辑断裂且至少一条量化防线同意 | 保留定位 | 分层架构（信号工程文档 ✅） |

**协同逻辑**：模型信号负责"主动撤退"（低频、同源）；MA + 回撤二维负责"被动保护"（替代价格止损且抗抖）；波动门控负责"别在噪声期做决定"；事件防线负责"价格看不见的风险"（清盘/规模/经理变更）；LLM 负责"给规则一个解释层"。**除风格漂移这类强证据外，单条规则不单独触发 EXIT**，避免重蹈"价格止损单点误杀"覆辙。

---

## 7. 落地成本评估

| 改动 | 工作量 | 成本构成 | 依赖现状 |
|---|---|---|---|
| 模型同源退出升级（预测序列 + 相对门槛 + 确认期 + 模型版本化） | 中 | monitor 状态机扩展、`s_t`/`model_version` 落库、阈值回测 | 已有模型/score/get_entry_score；需新增字段 |
| MA/回撤二维规则 | 低 | 纯函数（MA60/MA120 现算；回撤路径现算） | 净值已有 |
| 波动率门控 | 低 | 复用 vol_20d/idx_vol_20d 分位 | 特征已有 |
| 事件防线（规模/清盘/经理变更/限购） | 中-高 | 新增数据抓取（天天基金接口需实测）+ 公告解析（可 LLM 辅助） | 需核实数据源 |
| 防抖动机制（确认期/hysteresis/冷却/幂等） | 低 | 状态机与计数器 | 无 |
| 回测验证 | 中 | 扩展 backtest：`stop_mode` 增加 "ma"/"dd2d"/"model_seq"，**结算窗口从 20 日改为 60/120/250 日可变持有期** + 确认期参数 | 框架已有 |

**回测关键修正（务必做）**：现有 `sim_trailing_stop`/`sim_hard_stop` 以 20 日为结算上限，与"持仓期远超 20 日"冲突，会系统性低估退出规则价值。新规则验证必须支持可变持有期与确认期参数——这是本调研最重要的落地结论之一。

---

## 8. 引用清单（核实状态标注）

### ✅ 已在线核实（一手来源/权威档案）
1. Kaminski & Lo (2014), JFM 18:234–254 — [SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=968338)｜[ScienceDirect](https://www.sciencedirect.com/science/article/abs/pii/S138641811300030X)｜[MIT DSpace](https://dspace.mit.edu/handle/1721.1/114876)
2. Daniel & Moskowitz (2016), JFE 122(2):221–247, DOI 10.1016/j.jfineco.2015.12.002 — [作者 PDF](https://www.kentdaniel.net/papers/published/jfe_16.pdf)
3. Moskowitz, Ooi & Pedersen (2012), JFE 104(2):228–250 — [AQR](https://www.aqr.com/Insights/Datasets/Time-Series-Momentum-Original-Paper-Data)｜[CBS 档案](https://research.cbs.dk/en/publications/time-series-momentum/)
4. Moreira & Muir (2017), JF 72(4):1611–1644, DOI 10.1111/jofi.12513 — [Yale PDF](https://law.yale.edu/sites/default/files/area/workshop/leo/leo17_moreira.pdf)
5. Barroso & Santa-Clara (2015), JFE 116(1):111–120, DOI 10.1016/j.jfineco.2014.11.010 — [NOVA](https://novaresearch.unl.pt/en/publications/momentum-has-its-moments)｜[IDEAS](https://ideas.repec.org/a/eee/jfinec/v116y2015i1p111-120.html)
6. Faber (2007), JWM — [作者 PDF](https://mebfaber.com/wp-content/uploads/2016/05/SSRN-id962461.pdf)
7. Han, Yang & Zhou (2013), JFQA 48(5):1433–1461 — [WUSTL](https://profiles.wustl.edu/en/publications/a-new-anomaly-the-cross-sectional-profitability-of-technical-anal/)
8. Grossman & Zhou (1993), Math Finance 3(3):241–276, DOI 10.1111/j.1467-9965.1993.tb00044.x — [IDEAS](https://ideas.repec.org/a/bla/mathfi/v3y1993i3p241-276.html)
9. Magdon-Ismail et al. (2004), JAP 41(1):147–161, DOI 10.1239/jap/1077134674 — [Cambridge Core](https://www.cambridge.org/core/journals/journal-of-applied-probability/article/abs/on-the-maximum-drawdown-of-a-brownian-motion/F9E3B8A454B020DDEBF0AC3390EF7807)｜[配套 PDF](https://www.cs.rpi.edu/~magdon/ps/journal/drawdown_RISK04.pdf)
10. Chen, Hong, Huang & Kubik (2004), AER 94(5):1276–1302, DOI 10.1257/0002828043052277 — [aeaweb](https://www.aeaweb.org/articles?id=10.1257%2F0002828043052277)
11. Khorana (1996), JFE 40(3) — 子代理在线核实修正（第一版误标 JFQA）
12. Goyal & Welch (2008), RFS 21(4):1455–1508 — [IDEAS](https://ideas.repec.org/a/oup/rfinst/v21y2008i4p1455-1508.html)
13. Klass & Nowicki (2005), Stat & Prob Letters 74(3):245–252 — [ScienceDirect](https://www.sciencedirect.com/science/article/abs/pii/S0167715205001641)
14. 中国清盘条款（现行运作管理办法）— [海富通官网](https://www.hftfund.com/contents/2025/5/9-16a8af64c0a24657a72390071feb332d.html)｜[第一财经](https://www.yicai.com/news/1106208.html)
15. 场外基金费率实测（易方达标普500）— [和讯](https://jingzhifunds.hexun.com/database/sgfl.aspx?fundcode=161125)｜[百度开发者](https://developer.baidu.com/article/detail.html?id=6067007)

### ⚠️ 部分核实 / 训练知识标注（未在线抓全文）
16. Sharpe (1992), JPM 18(2) Winter — 经典文献，书目标注（第一版误标 18(1)，子代理核实修正）
17. Chan, Chen & Lakonishok (2002), RFS 15(5) — 经典文献
18. Brown & Goetzmann (1997), JFE 43(3) — 经典文献
19. Berk & Green (2004), JPE 112(6) — 经典文献
20. Chevalier & Ellison (1999), QJE 114(2) — 经典文献
21. Gu, Kelly & Xiu (2020), RFS 33(5):2223–2273 — 经典文献
22. Angelopoulos & Bates (2023) conformal prediction — 综述文献
23. Zakamulin (2017), Market Timing with Moving Averages — 专著
24. LightGBM quantile objective — 官方文档特性（未逐条核对）

### ❌ 未能核实（建议后续补充或放弃引用）
25. 陆蓉等 (2007)《基金业绩与投资者的选择》（中文文献，两轮搜索未果，建议放弃或本地图书馆核实）
26. Cvitanić & Karatzas (1995) 回撤约束优化 — 经 Grossman & Zhou 引用链间接确认存在，未直接核实

---

## 9. Gaps 与后续步骤

1. **回测框架扩展是第一步**：结算窗口 20 日 → 60/120/250 日可变持有期；新增 "ma"/"dd2d"/"model_seq" 退出模拟模式；确认期参数化。用现有 walk-forward 框架重跑，验证第 6 章组合是否优于固定持有。
2. **模型版本化**：`app/model.py` 增加 `model_version` 持久化（meta 表记录训练时间戳/模型指纹），`recommend_log` 记录买入时版本，监控打分绑定买入版本。
3. **事件数据可获取性未实测**：天天基金规模估算值、申购状态、经理变更公告的接口可用性与更新频率需实测（数据基座扩展）。
4. **门槛参数校准**：相对买入分 α、确认期 N、回撤二维阈值、MA 周期——全部走 walk-forward OOS，防止过拟合（Goyal & Welch 警示）。
5. **下一步建议**：(a) 扩展回测验证"模型序列+MA+回撤二维"组合；(b) 实现模型版本化 + 预测序列落库；(c) 事件防线先做规模/清盘两条（数据源单一、阈值清晰）。

---

*本调研存档为研究记录，不构成投资建议。*

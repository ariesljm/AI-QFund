# AI-QFund 前端 UI 提示词（stitch 输入版）

> 用途：直接粘贴进 stitch 生成前端原型。配色、字体、组件库等视觉细节交由 stitch 自由设计，不约束。

## 通用设定

- style: dashboard / fintech / data-driven / minimal / read-only
- audience: 个人投资者、策略研究者
- 约束: 纯只读展示，无交易、无表单提交、无下单按钮；标注数据绑定字段；先使用假数据进行填充

## 页面 1：总览仪表板 (Dashboard)

- aspect: 16:9
- prompt: 基金量化投研只读仪表板首页。包含：顶部大盘环境状态条（BULL 顺势 / BEAR 防御反弹 二态徽章，附指数值、MA60、乖离率 BIAS）；今日运行概览 4 个指标卡（推荐日期、本日新推荐数、当前持仓数、今日平仓数）；今日推荐卡片（唯一 HOLD 推荐：基金代码、名称、推荐日期、评分、环境标签、LLM 买入逻辑自然语言、RBSA 第一大重仓行业及权重进度条）；候选 Top 10 漏斗表格（代码、名称、评分、重仓行业、LLM 否决状态、否决理由，被否决行置灰）；持仓虚拟池列表（累计净值、距最高点回撤%、HOLD/EXIT 状态徽章、平仓理由与日期）；宏观摘要卡（新闻摘要、领涨/领跌行业、ETF 净流入）。组件标注数据绑定字段：regime、score、buy_reason、rbsa_weight_1、highest_nav 等。

## 页面 2：持仓监控详情 (Holding Detail)

- aspect: 16:9
- prompt: 单只基金持仓监控详情页。包含：累计净值走势折线图（累计净值线 + 持仓最高点 highest_nav 水平参考线）；三道防线可视化——① 追踪止损（当前回撤 vs 2×ATR 阈值对比）；② 风格漂移（买入时 vs 当前 RBSA 第一大行业权重差值，超 15% 醒目标识）；③ 逻辑证伪（buy_reason 与最新新闻对比状态）；HOLD/EXIT 状态徽章。组件标注数据绑定字段：highest_nav、atr、rbsa_weight_1、buy_reason、sell_reason。

## 页面 3：进化错题本 (Evolution Ledger)

- aspect: 16:9
- prompt: 系统进化错题本页面。包含：顶部说明区（解释系统从每月亏损超 -5% 的 EXIT 交易学到的硬规则，作为次日 LLM 否决的核心宪法）；规则列表（每条=规则文本、来源亏损交易 ID、创建日期、启用状态 active 徽章）。组件标注数据绑定字段：rule、source_trade_id、created_date、active。

## 交付要求

- 高保真可点击原型，覆盖上述 3 个页面
- 每页含空数据状态与有数据状态两种展示
- 组件标注命名与数据绑定字段，便于后端 FastAPI 只读 API 对接
- 页面使用采用中文显示
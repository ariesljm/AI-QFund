# 素材装配单一来源

监控引擎此前私有实现 `rbsa_distribution`/`anchor_holdings_text`/`market_technical_text`/`pool_text` 等素材拼接，推荐引擎与宏观引擎也各自拼装，同一持仓快照的文案表述可能漂移。决定：`llm/context.py` 为 LLM 素材装配单一来源——入参为基础元组（避免 llm→engine 反向依赖），各引擎改薄委托调用；monitor 私有工厂保留为测试 seam（monkeypatch 兼容）。

## Considered Options

- **各引擎自行拼装** — 被拒：同一事实多处表述，文案与数据口径漂移风险。
- **素材直接放 prompts.py** — 被拒：prompts 是模板文本，素材是数据转译，职责不同。

## Consequences

- 新增 LLM 素材段落须进 `llm/context.py`；引擎只消费，不自行拼接。
- monitor 的薄委托保测试 seam（`mon._rbsa_distribution` 等 monkeypatch 继续有效）。

## 修订（2026-09，2.0）

**原则不变**：LLM 素材装配仍收敛在 `llm/context.py` 单一归属，引擎只消费。
**内容变化**：装配素材从 1.x 的赛道/锚点/RBSA 分布（随赛道下线）换成
基金画像/重仓股（PIT 可见）/事件切片（票 23 Web 改造时同步落地）。
本 ADR 的验收句“新增素材段落须进 llm/context.py”继续适用。

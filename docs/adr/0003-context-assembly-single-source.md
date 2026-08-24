# 素材装配单一来源

监控引擎此前私有实现 `rbsa_distribution`/`anchor_holdings_text`/`market_technical_text`/`pool_text` 等素材拼接，推荐引擎与宏观引擎也各自拼装，同一持仓快照的文案表述可能漂移。决定：`llm/context.py` 为 LLM 素材装配单一来源——入参为基础元组（避免 llm→engine 反向依赖），各引擎改薄委托调用；monitor 私有工厂保留为测试 seam（monkeypatch 兼容）。

## Considered Options

- **各引擎自行拼装** — 被拒：同一事实多处表述，文案与数据口径漂移风险。
- **素材直接放 prompts.py** — 被拒：prompts 是模板文本，素材是数据转译，职责不同。

## Consequences

- 新增 LLM 素材段落须进 `llm/context.py`；引擎只消费，不自行拼接。
- monitor 的薄委托保测试 seam（`mon._rbsa_distribution` 等 monkeypatch 继续有效）。

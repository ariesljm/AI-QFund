# LLM 调用单一接口收敛

推荐引擎（选赛道/终选定论）与宏观引擎此前各自调 `call_llm` + `parse_llm_json`，审计写入散落多处、解析失败语义不统一。决定：`call_llm_json` 为唯一 LLM 接口——调用方只传 prompt + 可选 validator，审计写入（成功/解析失败/技术失败）收敛在 `_call_llm` 单一 choke point，解析失败统一记 ok=False、validator 拒绝返回 fallback。`call_llm` 保留供仅需原始文本的场景。

## Considered Options

- **各自 call_llm + parse_llm_json** — 被拒：审计双写/漏写难排查，解析失败有的返回 None 有的抛异常，调用方解读不一致。
- **新增中间层统一封装** — 被拒：`call_llm_json` 已是合适粒度，再加层增加间接无收益。

## Consequences

- 新增 LLM 调用点必须用 `call_llm_json`（传 caller 标识进审计）；仅原始文本场景用 `call_llm`。
- validator core（如 `_validate_final_pick`）可被测试单独复用，解析与校验解耦。

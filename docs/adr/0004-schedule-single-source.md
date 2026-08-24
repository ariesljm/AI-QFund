# 调度时间解析单一口径

调度启用判定（scheduler_loop：到点触发）与下次运行展示（next_run_for：今日未跑显示今天补跑）此前各自解析 hour/minute 配置，str/int 兼容、None 启用语义各写一遍。决定：`_sched_run_time(now, sched) -> datetime | None` 为单一解析口径——hour/min 两者皆非空才启用，返回触发点（到期=触发，未到=显示）。`_slot_key` 统一 meta key 构造。

## Considered Options

- **两条解析各保留** — 被拒：配置 str/int 混用 + 启用语义靠约定，修改一处忘改另一处是陈旧调度 bug 根因。
- **meta 存结构化 schedule 对象** — 被拒：meta 是 KV，结构化对象序列化增加耦合，标量 hour/minute/enabled 足够。

## Consequences

- 新增调度配置项只改 `_sched_run_time` + `_slot_key`；触发与展示语义由调用方各自判断（调度=触发点，展示=显示点）。

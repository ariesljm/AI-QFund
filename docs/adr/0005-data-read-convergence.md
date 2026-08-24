# 数据访问读侧收敛（概念读服务 + meta 单一 + conn 不暴露）

foundation 此前 15 条内联 SELECT + 4 处逐字重复查询（buyable ×4、每基金 MAX(date) ×3）散落业务层；meta 四轨写入（foundation/trading_calendar/decision/database.config）；公共读接口暴露 conn=None 参数导致连接管理泄漏。决定：7 个概念读服务按语义聚合（不 1:1 搬 SQL）入 `repo/base.py`——`get_nav_time_state`/`get_holdings_report_dates`/`has_nav_data`/`get_industry_map_stats` 等替代内联查询；meta 唯一写路径 `repo.get_meta/save_meta`（`database.meta_get/set` 降为内部实现）；连接工厂按库路径缓存 `_init_schema/_migrate`；repo 公共读签名不暴露 conn（批处理骨架 `run_batched_fetch` 与管道级共享 conn 是结构性例外）。

## Considered Options

- **1:1 搬 SQL 到 repo** — 被拒：失去语义聚合，buyable/MAX(date) 仍重复。
- **公共 API 全去 conn** — 被拒：`run_batched_fetch(conn=)` 是批处理骨架结构性契约，`run_pipeline` 管道共享连接是写链路必需，保留。

## Consequences

- 新增读查询先查 `repo/base.py` 是否有语义聚合的服务，无则加一个（不内联）。
- meta 读写只经 `repo.get_meta/save_meta`；`database.meta_get/set` 不再被外部 import。
- 公共 repo 读不接 conn；批处理/管道的 conn 共享是存储层/骨架的结构性约定，非公共接口。
- 与 0001（写不变式）互补：0001 管"谁写哪张表"，本 ADR 管"读与连接怎么收敛"。

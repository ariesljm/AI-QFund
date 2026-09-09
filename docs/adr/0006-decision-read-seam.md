# 决策域读侧归一 decision seam（跨域读归属成文）

架构深化候选 ⑫：`repo/base.py` 声称"可重建底层数据只读"，却直读决策域表
`recommend_log`（entry_nav 成对匹配）与 `monitor_events`（rank-1 最新信号）——
领域承诺（可重建域）与读侧现实（决策域表）不一致。决定：决策域表的读侧归一
`repo/decision.py` seam，`base.py` 不再出现决策域表名。

## 落地（2026-09-07）

- `get_candidate_nav_summaries`（候选批量汇总，4 查询中含 recommend_log/monitor_events 2 条跨域）
  从 `repo/base.py` 迁入 `repo/decision.py`，进入 `__all__`；包聚合层 `app/repo/__init__.py`
  star-export 不变，`repo.get_candidate_nav_summaries` 调用方（web/dashboard）零改动。
- 迁移后 `base.py` 决策域表引用清零（grep 验证无 recommend_log / monitor_events /
  evolution_insights / sector_selections / empty_recommend）。
- decision.py 直读可重建表（fund_features 等）是既有先例：决策域读基础数据（只读）
  是允许方向，反向（基础域读决策表）才是跨域，本 ADR 只禁后者。

## Considered Options

- **仅把 2 条跨域查询挪 decision、保留函数在 base** — 被拒：函数主体仍是决策域素材装配，
  拆开反而让"候选汇总"概念跨两文件。
- **整体函数搬家**（采纳）— 决策域读侧自洽；fund_nav 2 条查询随函数进入 decision 属
  决策域读基础数据的允许方向。

## Consequences

- 新增对决策域表的读，一律先进 `repo/decision.py` 查是否已有语义聚合服务，无则加一个。
- `repo/base.py` 只读可重建域表（fund_*/index_daily 等）；领域承诺与读侧现实一致。
- 与 0001（写不变式）/0005（读与连接收敛）互补：0001 管谁写、0005 管读收敛、本 ADR 管
  决策域读归属。

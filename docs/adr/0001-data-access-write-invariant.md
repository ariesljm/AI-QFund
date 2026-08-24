# 数据访问写不变式（Data Access Write Invariant）

数据基座可重建表与决策域表共用同一个 sqlite，此前"谁负责写哪张表"没有被强制的不变式（store seam 只覆盖了部分表，meta 存在三条写路径）。决定：**可重建表（fund_basic / fund_nav / index_daily / fund_holdings / stock_industry_map / fund_features）的写归 `app/data/store.py`；决策域表（recommend_log / sector_selections / monitor_events / monitor_scores / evolution_insights / quality_metrics / macro_news / empty_recommendations / llm_audit）的写归 `app/repo/decision.py`**；meta 表唯一写路径为 `repo.get_meta/save_meta`（`database.meta_get/meta_set` 降级为内部实现细节）；连接管理收敛为按库路径缓存初始化的连接工厂（`database.get_db`），repo 公共读接口不暴露连接参数（批量特征计算与管道内共享连接是存储层/批处理骨架的结构性约定，非公共接口）。

## Considered Options

- **方案 B：所有表写归 data/store，repo 只读** — 被拒：决策域写携带大量语义（exit_position 状态机、幂等结算、同日去重），迁去 store 会损失语义局部性。
- **现状（保持三轨 meta + 内联读旁路）** — 被拒：注释不是约束，两次 MAX(date) 语义靠约定一致，正是系统日志反复出现的陈旧/断档 bug 类。

## Consequences

- 新增表时须按不变式选择写入方：可重建（数据基座可重跑产出）进 store，决策域（用户/推荐生命周期数据）进 decision。
- 批量路径（特征全量计算、持仓下载管道）经存储层结构性 conn 约定复用连接；公共 repo 读签名无连接参数。
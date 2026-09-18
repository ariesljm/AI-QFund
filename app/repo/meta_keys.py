"""meta 表键单一来源（架构深化 I）。

编排状态与配置以裸字符串键跨 module 传递曾造成三轨读写（db_conn 直连 /
透传 get_meta / 专用函数）与魔法串拼写错误；现键定义收敛于此，读写路径统一
经 repo.get_meta/save_meta seam（含值类型化窄读 get_interval_days/get_int_cursor）。
"""

# 数据基座编排状态
HOLDINGS_LAST_RUN = "holdings_last_run"          # 持仓+行业映射最近成功日（Step 4 后置位）
INDUSTRY_MAP_UPDATED = "industry_map_updated"    # 行业映射最近更新日
FUND_LIST_LAST_UPDATE = "fund_list_last_update"  # 基金列表周重建最近时间

# 特征列 schema 版本：特征列增删后递增，强制 fund_features 快照全量重算
# （否则旧的"已最新"快照会带着新增列的 NULL 被跳过，dropna(FEATURE_COLS) 清空候选）
FEATURE_SCHEMA_VERSION = "feature_schema_version"

# 排序配置（推荐/回测/GA 共享）
RANKING_CFG = "ranking_cfg"

# 交易日历缓存 / 运行状态
TRADE_DATES_CACHE = "trade_dates_cache"
# 全历史交易日（1990 起）：公告日推算要覆盖历史报告期，与近两年窗口刻意分开
TRADE_DATES_HISTORY = "trade_dates_history"
UPTIME_START = "uptime_start"
INDEX_FRESHNESS = "index_freshness"  # 指数新鲜度告警留痕（审计 P1-2：指数断档无显式告警）

# 调度器去重（动态键前缀，槽位值 runner 内定义）
SCHED_LAST_RUN_PREFIX = "sched_last_run:"

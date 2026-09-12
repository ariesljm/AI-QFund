"""meta 表键单一来源（架构深化 I）。

编排状态与配置以裸字符串键跨 module 传递曾造成三轨读写（db_conn 直连 /
透传 get_meta / 专用函数）与魔法串拼写错误；现键定义收敛于此，读写路径统一
经 repo.get_meta/save_meta seam（含值类型化窄读 get_interval_days/get_int_cursor）。
"""

# 数据基座编排状态
HOLDINGS_LAST_RUN = "holdings_last_run"          # 持仓+行业映射最近成功日（Step 4 后置位）
INDUSTRY_MAP_UPDATED = "industry_map_updated"    # 行业映射最近更新日
FUND_LIST_LAST_UPDATE = "fund_list_last_update"  # 基金列表周重建最近时间

# 模型生命周期
MODEL_LAST_TRAINED = "model_last_trained"
MODEL_LABEL_VERSION = "model_label_version"   # 训练标签版本（防新旧标签错配）
# 特征列 schema 版本：特征列增删后递增，强制 fund_features 快照全量重算
# （否则旧的"已最新"快照会带着新增列的 NULL 被跳过，dropna(FEATURE_COLS) 清空候选）
FEATURE_SCHEMA_VERSION = "feature_schema_version"
LGB_PARAMS_SNAPSHOT = "lgb_params_snapshot"   # GA 月度寻优的树结构超参快照（每日训练读取）

# 排序配置（推荐/回测/GA 共享）
RANKING_CFG = "ranking_cfg"

# 推荐门控自愈冷却
RECOMMEND_DATA_HEAL_FAILED = "recommend_data_heal_failed"

# 进化引擎限频/游标
LAST_MONTHLY_EVOLVE = "last_monthly_evolve"      # 月度重任务最近执行日
LAST_GA_RUN = "last_ga_run"                      # GA 寻优最近评估日
LAST_GA_APPLIED = "last_ga_applied"              # GA 权重应用留痕（只写不读）
LAST_ANALYSIS_SS_ID = "last_analysis_ss_id"      # 元分析增量游标
LAST_ANALYSIS_OUTCOME = "last_analysis_outcome"  # 元分析健康度（审计 P1-1：空转可见性）

# 交易日历缓存 / 运行状态
TRADE_DATES_CACHE = "trade_dates_cache"
UPTIME_START = "uptime_start"
INDEX_FRESHNESS = "index_freshness"  # 指数新鲜度告警留痕（审计 P1-2：指数断档无显式告警）

# 调度器去重（动态键前缀，槽位值 runner 内定义）
SCHED_LAST_RUN_PREFIX = "sched_last_run:"

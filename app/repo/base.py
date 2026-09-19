"""底层数据 seam（兼容 re-export 层）。

历史 561 行巨型模块已按数据域拆分为：
- fund_data.py：基金/持仓/估值/个股/行业/nav_state
- features.py：特征/候选池/硬过滤
- market.py：指数行情/大盘 regime
- meta_system.py：配置/游标/系统日志/数据就绪

本文件保留顶层常量与 re-export：`from app.repo.base import X`、
`from app.repo import X`（__init__ 转 re-export）、`import app.repo.base as base_mod`
（tests monkeypatch）全部兼容，不破坏任何调用点。
"""

from app import domain
from app.database import db_conn, meta_get, meta_set  # noqa: F401  (re-export 供 from app.repo.base import db_conn)
from app.utils.log import get_logger

logger = get_logger("repo")


# 模型特征列清单（fund_features 表列名，单一来源；repo 拼 SQL / 特征计算 / 回测均从此导入）
FEATURE_COLS = domain.FEATURE_COLS

# 市场状态列（R1 绝对收益目标配套）：不进 fund_features 表，训练/打分时从指数现算注入
MARKET_COLS = domain.MARKET_COLS

# 推荐模型前向预测窗口（交易日），训练与回测共用（领域常量单一来源）
FORWARD_WINDOW = domain.FORWARD_DAYS


from app.repo.fund_data import *  # noqa: E402,F401,F403
from app.repo.features import *  # noqa: E402,F401,F403
from app.repo.features import _latest_feature_join  # noqa: E402,F401  (下划线名 import * 不导出)
from app.repo.market import *  # noqa: E402,F401,F403
from app.repo.market import _ema250_latest  # noqa: E402,F401  (下划线名 import * 不导出)
from app.repo.meta_system import *  # noqa: E402,F401,F403


__all__ = ["FEATURE_COLS", "FORWARD_WINDOW", "MARKET_COLS", "_ema250_latest", "_latest_feature_join", "check_data_ready", "get_all_ranking_rows", "get_buyable_codes", "get_codes_missing_rbsa", "get_data_latest_date", "get_feature_codes_before", "get_feature_dates_map", "get_fund_basics", "get_fund_pool_stats", "get_holdings", "get_holdings_at_report", "get_holdings_report_dates", "get_holdings_report_dates_all", "get_holdings_summaries", "get_holdings_two_periods", "get_index_close", "get_index_rows", "get_index_series", "get_industry_map", "get_industry_map_gap_count", "get_industry_map_stats", "get_industry_map_targets", "get_int_cursor", "get_interval_days", "get_latest_feature_date_before", "get_latest_features", "get_latest_features_batch", "get_latest_holdings_rows", "get_market_regime", "get_meta", "get_nav_time_state", "get_pe_histories", "get_restriction_facts", "get_sector_heatmap", "get_settings_all", "get_stock_daily", "get_system_logs", "get_uptime_days", "has_index_data", "has_nav_data", "save_meta", "save_settings_all"]

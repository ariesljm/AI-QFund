"""决策域 seam 门面（ADR-0006：决策域读归一 decision seam）。

本文件只做 re-export——按表族拆到子模块（recommend_log / screening /
tracked_state / quality_metric / llm_audit / purchase_restriction / sector_daily），
外部访问 `from app.repo import decision` / `repo.X` / `decision.X` 全部不变。
模块级 db_conn / get_meta / domain / META / logger 亦在此 re-export，
保既有 `decision_mod.db_conn` / `from app.repo.decision import db_conn` 可达。
新增决策域读先查对应子模块有无语义聚合服务，无则加在对应子模块。
"""
from app import (
    domain,  # noqa: F401  # 模块级名字 re-export（测试与调用方经 decision.db_conn 等访问）
)
from app.database import db_conn  # noqa: F401
from app.repo import meta_keys as META  # noqa: F401
from app.repo.base import get_meta  # noqa: F401
from app.repo.llm_audit import *  # noqa: F401,F403
from app.repo.purchase_restriction import *  # noqa: F401,F403
from app.repo.quality_metric import *  # noqa: F401,F403
from app.repo.recommend_log import *  # noqa: F401,F403
from app.repo.screening import *  # noqa: F401,F403
from app.repo.sector_daily import *  # noqa: F401,F403
from app.repo.tracked_state import *  # noqa: F401,F403
from app.utils.log import get_logger  # noqa: F401

logger = get_logger("repo")

__all__ = ["clear_recommendations", "count_recommendation_domain", "get_all_tracked_states", "get_candidate_nav_summaries", "get_e2e_sample_rows", "get_entry_nav", "get_first_reco_date", "get_fund_detail", "get_holding_codes", "get_latest_reco_id", "get_latest_recommendations", "get_purchase_status", "get_purchase_statuses", "get_quality_metrics", "get_quality_sample_rows", "get_ranking_cfg", "get_recent_audits", "get_recommend_v2", "get_recommend_v2_codes", "get_screen_candidates", "get_sector_pct_map", "get_sector_pct_series", "get_signal_history", "get_signal_stats", "get_tracked_state", "get_tracking_list", "insert_llm_audit", "insert_recommendation", "record_signal_trigger", "save_purchase_restriction", "save_quality_metrics", "save_recommend_v2", "save_screen_candidates", "save_sector_history_batch", "save_tracked_state", "settle_signal"]

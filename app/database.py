"""数据库连接管理：连接、schema 初始化、迁移、上下文管理器。"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.utils.log import get_logger

logger = get_logger("database")

DB_PATH = Path("data/qfund.db")

# Q4-B 连接工厂：schema 初始化与迁移按库路径缓存（同一路径仅初始化一次）。
# 生产路径一次性建表；测试注入的临时库（每测试独立路径）各自首访时初始化，
# 消除 get_db 每次调用重复的 _init_schema/_migrate（39 个 repo getter 自开连接场景）。
_INITIALIZED_PATHS: set[str] = set()


def get_db() -> sqlite3.Connection:
    # timeout=30：管线批量写（长事务）+ web/调度器并发连接时，等待写锁 30s 而非默认 5s 抛 locked
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    if str(DB_PATH) not in _INITIALIZED_PATHS:
        _init_schema(conn)
        _migrate(conn)
        _INITIALIZED_PATHS.add(str(DB_PATH))
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    # 查找顺序：先镜像内备份（Dockerfile 已 cp data/schema.sql 到 /app/schema.sql，
    # 不被宿主 data 卷遮蔽），再本地 data/ 源文件（本地开发）——
    # 修复：旧顺序先读 data/ 时，Docker 数据卷中的旧版 schema.sql 会遮蔽镜像内新文件
    schema = Path(__file__).resolve().parent.parent / "schema.sql"
    if not schema.exists():
        schema = Path(__file__).resolve().parent.parent / "data" / "schema.sql"
    if not schema.exists():
        if not getattr(_init_schema, '_warned', False):
            _init_schema._warned = True  # type: ignore[attr-defined]  # 函数对象动态标记
            logger.warning("schema.sql 未找到，跳过初始化")
        return
    conn.executescript(schema.read_text(encoding="utf-8"))
    conn.commit()


@contextmanager
def db_conn() -> Iterator[sqlite3.Connection]:
    conn = get_db()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    # 架构深化 E：schema 单一真相——表 DDL 全部由 schema.sql 负责（_init_schema），
    # 此处仅保留历史 ALTER 迁移（旧库补列）。原 11 张重复 CREATE 兜底已删：
    # 镜像内 /app/schema.sql 备份不被 data 卷遮蔽，兜底造成双真相（加列需双写）。
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    if "fund_features" in tables:
        ff_cols = {row[1] for row in conn.execute("PRAGMA table_info(fund_features)").fetchall()}
        if "style_r2" not in ff_cols:
            conn.execute("ALTER TABLE fund_features ADD COLUMN style_r2 REAL DEFAULT 0")
            conn.commit()

    if "recommend_log" in tables:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(recommend_log)").fetchall()}
        for col, typ in [("return_rate", "REAL"), ("feature_snapshot", "TEXT"), ("entry_nav", "REAL"), ("candidate_codes", "TEXT"), ("rec_count", "INTEGER DEFAULT 1"), ("vetoed_json", "TEXT"), ("reco_path", "TEXT DEFAULT 'sector'"), ("decision_logic", "TEXT")]:
            if col not in cols:
                conn.execute(f"ALTER TABLE recommend_log ADD COLUMN {col} {typ}")
                conn.commit()
        # 旧行 rec_count 回填：ALTER 后应为 1，防御性兜底 NULL（幂等更新 rec_count+1 时 NULL 会吞计数）
        # 先 COUNT 再写：避免每次连接都触发 UPDATE+commit（并发连接时是无条件写锁源）
        if conn.execute("SELECT COUNT(*) FROM recommend_log WHERE rec_count IS NULL").fetchone()[0]:
            conn.execute("UPDATE recommend_log SET rec_count = 1 WHERE rec_count IS NULL")
            conn.commit()

    # ── 历史 ALTER 迁移：旧库补列（schema.sql 已含新列，新建库无需执行）──

    if "fund_basic" in tables:
        fb_cols = {row[1] for row in conn.execute("PRAGMA table_info(fund_basic)").fetchall()}
        for col in ("aum", "shares"):
            if col not in fb_cols:
                conn.execute(f"ALTER TABLE fund_basic ADD COLUMN {col} REAL")
                conn.commit()

    if "purchase_restrictions" in tables:
        pr_cols = {row[1] for row in conn.execute("PRAGMA table_info(purchase_restrictions)").fetchall()}
        if "daily_limit" not in pr_cols:
            conn.execute("ALTER TABLE purchase_restrictions ADD COLUMN daily_limit REAL")
            conn.commit()

    if "fund_holdings" in tables:
        fh_cols = {row[1] for row in conn.execute("PRAGMA table_info(fund_holdings)").fetchall()}
        if "disclosure_date" not in fh_cols:
            conn.execute("ALTER TABLE fund_holdings ADD COLUMN disclosure_date TEXT")
            conn.commit()
        # 存量回填（共识 Q15）：按不同报告期各算一次。不回填的话 NULL 在 PIT
        # 过滤下等价于“永不可见”，持仓素材会静默变空。
        # 日历从本 conn 直读（不能走 get_meta：那会 db_conn → get_db → _migrate 递归），
        # 无缓存时退化为 +31 自然日上界。
        from app.utils.trading_calendar import disclosure_date as _disclosure_date
        try:
            import json as _json

            from app.repo import meta_keys as _META
            _raw = conn.execute("SELECT value FROM meta WHERE key = ?",
                                (_META.TRADE_DATES_HISTORY,)).fetchone()
            _hist = set(_json.loads(_raw[0])) if _raw else set()
        except (sqlite3.Error, ValueError, TypeError):
            _hist = set()
        pending = [r[0] for r in conn.execute(
            "SELECT DISTINCT report_date FROM fund_holdings "
            "WHERE disclosure_date IS NULL").fetchall()]
        for report_date in pending:
            conn.execute("UPDATE fund_holdings SET disclosure_date = ? WHERE report_date = ?",
                         (_disclosure_date(report_date, days=_hist), report_date))
        if pending:
            conn.commit()

    if "monitor_events" in tables:
        me_cols = {r[1] for r in conn.execute("PRAGMA table_info(monitor_events)").fetchall()}
        # C5：净值陈旧等数据告警与信号语义分离——stale 事件不计入 WARNING 升级序列
        if "is_stale" not in me_cols:
            conn.execute("ALTER TABLE monitor_events ADD COLUMN is_stale BOOLEAN DEFAULT 0")
            conn.commit()

    if "empty_recommendations" in tables:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(empty_recommendations)").fetchall()}
        if "reason_type" not in cols:
            # 审计 P1-2：空推荐语义分层（no_opportunity=市场判断 / data_failure=数据故障）
            conn.execute("ALTER TABLE empty_recommendations ADD COLUMN reason_type TEXT DEFAULT 'no_opportunity'")
            conn.commit()

    if "macro_news" in tables:
        macro_cols = {r[1] for r in conn.execute("PRAGMA table_info(macro_news)").fetchall()}
        for col, typ in [("flow_json", "TEXT"), ("context_json", "TEXT"),
                         ("news_date", "TEXT")]:
            if col not in macro_cols:
                conn.execute(f"ALTER TABLE macro_news ADD COLUMN {col} {typ}")
                conn.commit()

    if "index_daily" in tables:
        idx_cols = {r[1] for r in conn.execute("PRAGMA table_info(index_daily)").fetchall()}
        # 旧库补列：EMA60 趋势列（schema.sql 已含，新建库无需执行）
        if "ema60" not in idx_cols:
            conn.execute("ALTER TABLE index_daily ADD COLUMN ema60 REAL")
            conn.commit()

    if "fund_features" in tables:
        ff_cols = {row[1] for row in conn.execute("PRAGMA table_info(fund_features)").fetchall()}
        for col, typ in [("rbsa_industry_2", "TEXT"), ("rbsa_weight_2", "REAL DEFAULT 0"),
                         ("rbsa_industry_3", "TEXT"), ("rbsa_weight_3", "REAL DEFAULT 0"),
                         ("drawdown_60d", "REAL"), ("reversal_20d", "REAL"),
                         ("mom_5d", "REAL"), ("mom_60d", "REAL"), ("mom_250d", "REAL"),
                         ("vol_20d", "REAL"),
                         ("sharpe_60d", "REAL"), ("sortino_60d", "REAL"), ("ttr_60d", "REAL")]:
            if col not in ff_cols:
                conn.execute(f"ALTER TABLE fund_features ADD COLUMN {col} {typ}")
                conn.commit()

    if "quality_metrics" in tables:
        qm_cols = {row[1] for row in conn.execute("PRAGMA table_info(quality_metrics)").fetchall()}
        if "points_json" not in qm_cols:
            conn.execute("ALTER TABLE quality_metrics ADD COLUMN points_json TEXT")
            conn.commit()
        # 阶段5：赚钱口径新指标（profit_rate/mean_abs_ret/payoff_ratio）
        for col, typ in (("profit_rate", "REAL"), ("mean_abs_ret", "REAL"),
                         ("payoff_ratio", "REAL")):
            if col not in qm_cols:
                conn.execute(f"ALTER TABLE quality_metrics ADD COLUMN {col} {typ}")
                conn.commit()
        # Q5 共识：裁决损耗（LLM 选中基金 vs 候选池均值的 40 日收益差）
        if "decision_loss" not in qm_cols:
            conn.execute("ALTER TABLE quality_metrics ADD COLUMN decision_loss REAL")
            conn.commit()
        # P1-4 回滚后扩展：LLM 选中 vs 候选池最优的收益差（与裁决损耗同一套月度样本）
        if "decision_gap_best" not in qm_cols:
            conn.execute("ALTER TABLE quality_metrics ADD COLUMN decision_gap_best REAL")
            conn.commit()
        # 分口径度量（按 reco_path 分组）
        if "by_path_json" not in qm_cols:
            conn.execute("ALTER TABLE quality_metrics ADD COLUMN by_path_json TEXT")
            conn.commit()
        # 端到端 P&L（#2）：按实际退出日期扣赎回费后的净收益与择时贡献
        for col, typ in (("e2e_profit_rate", "REAL"), ("e2e_mean_ret", "REAL"),
                         ("e2e_payoff_ratio", "REAL"), ("timing_contribution", "REAL"),
                         ("e2e_sample_count", "INTEGER"), ("e2e_points_json", "TEXT")):
            if col not in qm_cols:
                conn.execute(f"ALTER TABLE quality_metrics ADD COLUMN {col} {typ}")
                conn.commit()
        # 体验指标（ticket 12）：推荐至退出的平均持仓天数与平均最大回撤
        for col, typ in (("e2e_mean_hold_days", "REAL"), ("e2e_mean_max_drawdown", "REAL")):
            if col not in qm_cols:
                conn.execute(f"ALTER TABLE quality_metrics ADD COLUMN {col} {typ}")
                conn.commit()
        # 分桶赚钱率（模型校准观测 #1）：按预测分分桶的赚钱率/样本数（JSON）
        if "by_score_bucket_json" not in qm_cols:
            conn.execute("ALTER TABLE quality_metrics ADD COLUMN by_score_bucket_json TEXT")
            conn.commit()
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_quality_metrics_period "
            "ON quality_metrics (period_start, period_end)"
        )
        conn.commit()

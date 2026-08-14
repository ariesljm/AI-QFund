"""数据库连接管理：连接、schema 初始化、迁移、上下文管理器。"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.utils.log import get_logger

logger = get_logger("database")

DB_PATH = Path("data/qfund.db")


def get_db() -> sqlite3.Connection:
    # timeout=30：管线批量写（长事务）+ web/调度器并发连接时，等待写锁 30s 而非默认 5s 抛 locked
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _init_schema(conn)
    _migrate(conn)
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
            _init_schema._warned = True
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

    if "recommend_log" in tables:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(recommend_log)").fetchall()}
        for col, typ in [("return_rate", "REAL"), ("feature_snapshot", "TEXT"), ("entry_nav", "REAL"), ("candidate_codes", "TEXT"), ("rec_count", "INTEGER DEFAULT 1")]:
            if col not in cols:
                conn.execute(f"ALTER TABLE recommend_log ADD COLUMN {col} {typ}")
                conn.commit()
        # 旧行 rec_count 回填：ALTER 后应为 1，防御性兜底 NULL（幂等更新 rec_count+1 时 NULL 会吞计数）
        # 先 COUNT 再写：避免每次连接都触发 UPDATE+commit（并发连接时是无条件写锁源）
        if conn.execute("SELECT COUNT(*) FROM recommend_log WHERE rec_count IS NULL").fetchone()[0]:
            conn.execute("UPDATE recommend_log SET rec_count = 1 WHERE rec_count IS NULL")
            conn.commit()

    # ── 历史 ALTER 迁移：旧库补列（schema.sql 已含新列，新建库无需执行）──

    if "sector_selections" in tables:
        ss_cols = {r[1] for r in conn.execute("PRAGMA table_info(sector_selections)").fetchall()}
        if "used_insight_ids" not in ss_cols:
            conn.execute("ALTER TABLE sector_selections ADD COLUMN used_insight_ids TEXT")
            conn.commit()
        # P1-5 否决反事实度量：量化池内候选赛道 + 结算回填的池内收益
        if "pool_sectors" not in ss_cols:
            conn.execute("ALTER TABLE sector_selections ADD COLUMN pool_sectors TEXT")
            conn.commit()
        if "pool_outcomes" not in ss_cols:
            conn.execute("ALTER TABLE sector_selections ADD COLUMN pool_outcomes TEXT")
            conn.commit()

    if "monitor_events" in tables:
        me_cols = {r[1] for r in conn.execute("PRAGMA table_info(monitor_events)").fetchall()}
        # C5：净值陈旧等数据告警与信号语义分离——stale 事件不计入 WARNING 升级序列
        if "is_stale" not in me_cols:
            conn.execute("ALTER TABLE monitor_events ADD COLUMN is_stale BOOLEAN DEFAULT 0")
            conn.commit()

    if "evolution_insights" in tables:
        ei_cols = {r[1] for r in conn.execute("PRAGMA table_info(evolution_insights)").fetchall()}
        # P3-11 洞察结构化：可判定前置条件
        if "condition" not in ei_cols:
            conn.execute("ALTER TABLE evolution_insights ADD COLUMN condition TEXT")
            conn.commit()

    if "macro_news" in tables:
        macro_cols = {r[1] for r in conn.execute("PRAGMA table_info(macro_news)").fetchall()}
        for col, typ in [("flow_json", "TEXT"), ("context_json", "TEXT")]:
            if col not in macro_cols:
                conn.execute(f"ALTER TABLE macro_news ADD COLUMN {col} {typ}")
                conn.commit()

    if "evolution_rules" in tables:
        evolution_rules_exists = True
    else:
        evolution_rules_exists = False

    if evolution_rules_exists and "evolution_insights" in tables:
        old_cnt = conn.execute("SELECT COUNT(*) FROM evolution_rules").fetchone()[0]
        new_cnt = conn.execute("SELECT COUNT(*) FROM evolution_insights").fetchone()[0]
        if old_cnt == 0 or new_cnt > 0:
            conn.execute("DROP TABLE IF EXISTS evolution_rules")
            conn.commit()
            logger.info("evolution_rules 旧表已清理 (old=%d, new=%d)", old_cnt, new_cnt)

    if "fund_features" in tables:
        ff_cols = {row[1] for row in conn.execute("PRAGMA table_info(fund_features)").fetchall()}
        for col, typ in [("rbsa_industry_2", "TEXT"), ("rbsa_weight_2", "REAL DEFAULT 0"),
                         ("rbsa_industry_3", "TEXT"), ("rbsa_weight_3", "REAL DEFAULT 0"),
                         ("drawdown_60d", "REAL"), ("reversal_20d", "REAL"),
                         ("mom_5d", "REAL"), ("mom_60d", "REAL"), ("vol_20d", "REAL")]:
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
        # Q5 共识：裁决损耗（LLM 选中基金 vs 候选池均值的 20 日收益差）
        if "decision_loss" not in qm_cols:
            conn.execute("ALTER TABLE quality_metrics ADD COLUMN decision_loss REAL")
            conn.commit()
        # P1-4 回滚后扩展：LLM 选中 vs 候选池最优的收益差（与裁决损耗同一套月度样本）
        if "decision_gap_best" not in qm_cols:
            conn.execute("ALTER TABLE quality_metrics ADD COLUMN decision_gap_best REAL")
            conn.commit()
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_quality_metrics_period "
            "ON quality_metrics (period_start, period_end)"
        )
        conn.commit()

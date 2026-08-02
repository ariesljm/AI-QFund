"""数据库连接管理：连接、schema 初始化、迁移、上下文管理器。"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.utils.log import get_logger

logger = get_logger("database")

DB_PATH = Path("data/qfund.db")


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _init_schema(conn)
    _migrate(conn)
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    schema = Path(__file__).resolve().parent.parent / "data" / "schema.sql"
    if not schema.exists():
        schema = Path(__file__).resolve().parent.parent / "schema.sql"
    if not schema.exists():
        if not getattr(_init_schema, '_warned', False):
            _init_schema._warned = True
            logger.warning("schema.sql 未找到，跳过初始化")
        return
    conn.executescript(schema.read_text(encoding="utf-8"))
    conn.commit()


@contextmanager
def db_conn():
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
    if getattr(_migrate, '_done', False):
        return

    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    if "recommend_log" in tables:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(recommend_log)").fetchall()}
        for col, typ in [("return_rate", "REAL"), ("feature_snapshot", "TEXT"), ("entry_nav", "REAL")]:
            if col not in cols:
                conn.execute(f"ALTER TABLE recommend_log ADD COLUMN {col} {typ}")
                conn.commit()

    conn.execute(
        "CREATE TABLE IF NOT EXISTS sector_selections ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "date TEXT NOT NULL, "
        "recommend_log_id INTEGER, "
        "recommended_sectors TEXT, "
        "risk_sectors TEXT, "
        "sector_reasoning TEXT, "
        "regime_label TEXT, "
        "key_news_snippet TEXT, "
        "outcome TEXT DEFAULT '待定', "
        "outcome_date TEXT, "
        "outcome_note TEXT, "
        "created_at TEXT DEFAULT (datetime('now')))"
    )
    conn.commit()

    conn.execute(
        "CREATE TABLE IF NOT EXISTS monitor_events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "code TEXT NOT NULL, "
        "date TEXT NOT NULL, "
        "signal TEXT NOT NULL, "
        "trigger_trailing BOOLEAN DEFAULT 0, "
        "trigger_drift BOOLEAN DEFAULT 0, "
        "trigger_sector_adv BOOLEAN DEFAULT 0, "
        "logic_verdict TEXT, "
        "sector_risk BOOLEAN, "
        "holding_risk BOOLEAN, "
        "detail TEXT, "
        "recommend_log_id INTEGER, "
        "created_at TEXT DEFAULT (datetime('now')))"
    )
    conn.commit()

    conn.execute(
        "CREATE TABLE IF NOT EXISTS evolution_insights ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "insight TEXT NOT NULL, "
        "insight_type TEXT NOT NULL, "
        "source_ids TEXT, "
        "confidence REAL DEFAULT 1.0, "
        "created_date TEXT NOT NULL, "
        "last_applied_date TEXT, "
        "apply_count INTEGER DEFAULT 0, "
        "active INTEGER DEFAULT 1)"
    )
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

    conn.execute(
        "CREATE TABLE IF NOT EXISTS stock_industry_map ("
        "stock_code TEXT PRIMARY KEY, "
        "industry_code TEXT, "
        "industry_name TEXT, "
        "update_date TEXT)"
    )
    conn.commit()

    if "fund_features" in tables:
        ff_cols = {row[1] for row in conn.execute("PRAGMA table_info(fund_features)").fetchall()}
        for col, typ in [("rbsa_industry_2", "TEXT"), ("rbsa_weight_2", "REAL DEFAULT 0"),
                         ("rbsa_industry_3", "TEXT"), ("rbsa_weight_3", "REAL DEFAULT 0")]:
            if col not in ff_cols:
                conn.execute(f"ALTER TABLE fund_features ADD COLUMN {col} {typ}")
                conn.commit()

    if "quality_metrics" in tables:
        qm_cols = {row[1] for row in conn.execute("PRAGMA table_info(quality_metrics)").fetchall()}
        if "points_json" not in qm_cols:
            conn.execute("ALTER TABLE quality_metrics ADD COLUMN points_json TEXT")
            conn.commit()
    else:
        # 兜底：镜像内 schema.sql 可能被 data 卷中的旧版遮蔽，保证新表在旧卷上也创建
        conn.execute(
            "CREATE TABLE IF NOT EXISTS quality_metrics ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "computed_date TEXT NOT NULL, "
            "period_start TEXT, period_end TEXT, "
            "ic REAL, excess_win_rate REAL, mean_excess REAL, cum_excess REAL, "
            "sample_count INTEGER, points_json TEXT)"
        )
        conn.commit()
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_quality_metrics_period "
        "ON quality_metrics (period_start, period_end)"
    )
    conn.commit()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS empty_recommendations ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "date TEXT NOT NULL UNIQUE, "
        "reasoning TEXT, "
        "created_at TEXT DEFAULT (datetime('now')))"
    )
    conn.commit()

    conn.execute(
        "CREATE TABLE IF NOT EXISTS data_fetch_failures ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "fetch_type TEXT NOT NULL, "
        "target TEXT NOT NULL, "
        "stage TEXT DEFAULT '', "
        "error TEXT, "
        "attempts INTEGER DEFAULT 1, "
        "status TEXT DEFAULT 'failed', "
        "first_failed_at TEXT DEFAULT (datetime('now')), "
        "last_failed_at TEXT, "
        "recovered_at TEXT, "
        "UNIQUE (fetch_type, target))"
    )
    conn.commit()

    _migrate._done = True

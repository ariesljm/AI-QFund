"""数据库持久化层：连接管理、迁移、数据写入。"""

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import log_utils  # noqa: F401

logger = logging.getLogger("data_store")

DB_PATH = Path("data/qfund.db")
SETTINGS_PATH = Path("config/settings.toml")


import os as _os

_ENV_OVERRIDE_MAP = {
    "LLM_BASE_URL": ("llm", "base_url"),
    "LLM_API_KEY": ("llm", "api_key"),
    "LLM_MODEL": ("llm", "model"),
    "SCHEDULER_HOUR": ("scheduler", "hour"),
    "SCHEDULER_MINUTE": ("scheduler", "minute"),
    "WEB_PORT": ("web", "port"),
}


import tomllib as _tomllib

_SETTINGS_CACHE: dict | None = None


def _load_settings():
    global _SETTINGS_CACHE
    if _SETTINGS_CACHE is not None:
        return _SETTINGS_CACHE
    with open(SETTINGS_PATH, "rb") as f:
        settings = _tomllib.load(f)
    for env_key, (section, key) in _ENV_OVERRIDE_MAP.items():
        val = _os.environ.get(env_key)
        if val:
            settings.setdefault(section, {})[key] = val
    _SETTINGS_CACHE = settings
    return settings


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _init_schema(conn)
    _migrate(conn)
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    schema = Path("schema.sql")
    if not schema.exists():
        logger.warning("schema.sql 未找到，跳过建表 (路径: %s)", schema.resolve())
        return
    conn.executescript(schema.read_text(encoding="utf-8"))
    conn.commit()


@contextmanager
def _db_conn():
    conn = _get_db()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    # recommend_log 扩展列
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "recommend_log" in tables:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(recommend_log)").fetchall()}
        for col, typ in [("return_rate", "REAL"), ("feature_snapshot", "TEXT"), ("entry_nav", "REAL")]:
            if col not in cols:
                conn.execute(f"ALTER TABLE recommend_log ADD COLUMN {col} {typ}")
                conn.commit()

    # 赛道选择记录
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

    # 监控事件记录
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

    # 进化洞察（替代 evolution_rules）
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

    # macro_news 扩展列
    if "macro_news" in tables:
        macro_cols = {r[1] for r in conn.execute("PRAGMA table_info(macro_news)").fetchall()}
        if "flow_json" not in macro_cols:
            conn.execute("ALTER TABLE macro_news ADD COLUMN flow_json TEXT")
            conn.commit()

    # 删除旧表（仅当新表已创建时执行一次）
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "evolution_insights" in tables and "evolution_rules" in tables:
        # 检查 evolution_rules 是否已无数据或新表已有数据，避免误删
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


def _save_settings(settings: dict) -> bool:
    import re
    text = SETTINGS_PATH.read_text(encoding="utf-8")
    for section, values in settings.items():
        for key, value in values.items():
            if isinstance(value, bool):
                line = f'{key} = {"true" if value else "false"}'
            elif isinstance(value, int):
                line = f'{key} = {value}'
            elif isinstance(value, float):
                line = f'{key} = {value}'
            else:
                line = f'{key} = "{value}"'
            text = re.sub(
                rf'^{re.escape(key)}\s*=.*$',
                line,
                text,
                flags=re.MULTILINE,
            )
    SETTINGS_PATH.write_text(text, encoding="utf-8")
    global _SETTINGS_CACHE
    _SETTINGS_CACHE = None
    logger.info("配置已保存: %s", {k: list(v.keys()) for k, v in settings.items()})
    return True


def _meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
    conn.commit()


def save_fund_list(funds: list[dict]) -> int:
    with _db_conn() as conn:
        conn.execute("DELETE FROM fund_basic")
        count = 0
        for f in funds:
            conn.execute(
                "INSERT OR REPLACE INTO fund_basic (code, name, type, is_buyable) VALUES (?, ?, ?, ?)",
                (f["code"], f["name"], f["type"], f["is_buyable"]),
            )
            count += 1
        return count


def _save_nav_batch(conn: sqlite3.Connection, code: str, navs: list[dict]) -> int:
    if not navs:
        return 0
    cur = conn.execute("SELECT MAX(date) FROM fund_nav WHERE code = ?", (code,))
    row = cur.fetchone()
    local_max = row[0] if row and row[0] else None
    if local_max:
        navs = [n for n in navs if n["date"] > local_max]
    if not navs:
        return 0
    conn.executemany(
        "INSERT OR IGNORE INTO fund_nav (code, date, cum_nav) VALUES (?, ?, ?)",
        [(code, n["date"], n["cum_nav"]) for n in navs],
    )
    dividends = [
        (code, n["date"], n["dividend"])
        for n in navs
        if n.get("dividend") is not None
    ]
    if dividends:
        conn.executemany(
            "INSERT OR IGNORE INTO fund_dividend (code, date, dividend_per_unit) "
            "VALUES (?, ?, ?)",
            dividends,
        )
    return len(navs)


_EMA_PERIOD = 60
_EMA_K = 2 / (_EMA_PERIOD + 1)


def save_index_daily(index_code: str, data: list[dict]) -> int:
    with _db_conn() as conn:
        row = conn.execute(
            "SELECT date, ma60 FROM index_daily WHERE code = ? ORDER BY date DESC LIMIT 1",
            (index_code,),
        ).fetchone()
        local_max_date = row[0] if row else None
        prev_ema = row[1] if row and row[1] is not None else None

        data_sorted = sorted(data, key=lambda d: d["date"])
        if local_max_date:
            new_data = [d for d in data_sorted if d["date"] > local_max_date]
        else:
            new_data = data_sorted

        if not new_data:
            return 0

        seed_closes = [
            r[0] for r in conn.execute(
                "SELECT close FROM index_daily WHERE code = ? ORDER BY date ASC",
                (index_code,),
            ).fetchall()
        ]

        written = 0
        for d in new_data:
            close = d["close"]
            if prev_ema is not None:
                ema = close * _EMA_K + prev_ema * (1 - _EMA_K)
            else:
                seed_closes.append(close)
                if len(seed_closes) >= _EMA_PERIOD:
                    ema = sum(seed_closes[-_EMA_PERIOD:]) / _EMA_PERIOD
                else:
                    ema = None

            conn.execute(
                "INSERT OR REPLACE INTO index_daily (code, date, open, high, low, close, volume, ma60) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (index_code, d["date"], d["open"], d["high"], d["low"], d["close"], d["volume"], ema),
            )
            if ema is not None:
                prev_ema = ema
            written += 1

        return written


def save_holdings(code: str, holdings: list[dict], report_date: str) -> int:
    with _db_conn() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO fund_holdings (code, report_date, stock_code, stock_name, weight) "
            "VALUES (?, ?, ?, ?, ?)",
            [(code, report_date, h["stock_code"], h["stock_name"], h["weight"]) for h in holdings],
        )
        return len(holdings)

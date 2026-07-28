"""数据库仓库层：封装原始 SQL，统一数据访问入口。"""

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import sqlite3

from log_utils import get_logger

logger = get_logger("repo")

DB_PATH = Path("data/qfund.db")


@contextmanager
def db():
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


class NavRepo:
    @staticmethod
    def get_nav_since(code: str, since_date: str) -> list[float]:
        with db() as conn:
            rows = conn.execute(
                "SELECT cum_nav FROM fund_nav WHERE code = ? AND date >= ? ORDER BY date ASC",
                (code, since_date),
            ).fetchall()
        return [r[0] for r in rows]

    @staticmethod
    def get_latest_nav(code: str) -> float | None:
        with db() as conn:
            row = conn.execute(
                "SELECT cum_nav FROM fund_nav WHERE code = ? ORDER BY date DESC LIMIT 1",
                (code,),
            ).fetchone()
        return row[0] if row else None

    @staticmethod
    def get_nav_series(code: str, limit: int = 250) -> list[tuple[str, float]]:
        with db() as conn:
            rows = conn.execute(
                "SELECT date, cum_nav FROM fund_nav WHERE code = ? ORDER BY date DESC LIMIT ?",
                (code, limit),
            ).fetchall()
        return [(r[0], r[1]) for r in rows]

    @staticmethod
    def get_nav_on_date(code: str, date: str) -> float | None:
        with db() as conn:
            row = conn.execute(
                "SELECT cum_nav FROM fund_nav WHERE code = ? AND date = ?",
                (code, date),
            ).fetchone()
        return row[0] if row else None

    @staticmethod
    def get_all_nav_dates() -> list[tuple[str, str]]:
        with db() as conn:
            return conn.execute(
                "SELECT code, MAX(date) FROM fund_nav GROUP BY code"
            ).fetchall()

    @staticmethod
    def get_latest_navs_for_all(dt: str) -> list[tuple[str, float]]:
        with db() as conn:
            return conn.execute(
                "SELECT code, cum_nav FROM fund_nav WHERE date <= ? "
                "AND code IN (SELECT code FROM fund_nav WHERE date <= ? "
                "GROUP BY code HAVING COUNT(*) >= 60) "
                "GROUP BY code",
                (dt, dt),
            ).fetchall()

    @staticmethod
    def get_samples_for_training(min_days: int, limit: int) -> list[str]:
        with db() as conn:
            rows = conn.execute(
                "SELECT code FROM fund_nav GROUP BY code HAVING COUNT(*) >= ? "
                "ORDER BY RANDOM() LIMIT ?",
                (min_days, limit),
            ).fetchall()
        return [r[0] for r in rows]

    @staticmethod
    def get_nav_all(code: str) -> list[tuple[str, float]]:
        with db() as conn:
            rows = conn.execute(
                "SELECT date, cum_nav FROM fund_nav WHERE code = ? ORDER BY date ASC",
                (code,),
            ).fetchall()
        return [(r[0], r[1]) for r in rows]

    @staticmethod
    def save_batch(records: list[tuple]) -> int:
        with db() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO fund_nav (code, date, unit_nav, cum_nav, equity_return, unit_dividend) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                records,
            )
            return len(records)

    @staticmethod
    def get_max_date(code: str) -> str | None:
        with db() as conn:
            row = conn.execute(
                "SELECT MAX(date) FROM fund_nav WHERE code = ?",
                (code,),
            ).fetchone()
        return row[0] if row else None


class IndexRepo:
    @staticmethod
    def get_all(code: str = "sh000300") -> list[tuple[str, float, float, float]]:
        with db() as conn:
            rows = conn.execute(
                "SELECT date, close, volume, ma60 FROM index_daily WHERE code = ? ORDER BY date ASC",
                (code,),
            ).fetchall()
        return rows

    @staticmethod
    def get_recent(code: str = "sh000300", limit: int = 21) -> list[float]:
        with db() as conn:
            rows = conn.execute(
                "SELECT close FROM index_daily WHERE code = ? ORDER BY date DESC LIMIT ?",
                (code, limit),
            ).fetchall()
        return [r[0] for r in rows]

    @staticmethod
    def get_latest_ma60(code: str = "sh000300") -> tuple[float | None, float | None]:
        with db() as conn:
            row = conn.execute(
                "SELECT close, ma60 FROM index_daily WHERE code = ? ORDER BY date DESC LIMIT 1",
                (code,),
            ).fetchone()
        return (row[0], row[1]) if row else (None, None)

    @staticmethod
    def get_close_on_date(date: str, code: str = "sh000300") -> float | None:
        with db() as conn:
            row = conn.execute(
                "SELECT close FROM index_daily WHERE code = ? AND date <= ? ORDER BY date DESC LIMIT 1",
                (code, date),
            ).fetchone()
        return row[0] if row else None

    @staticmethod
    def get_full_series(code: str = "sh000300") -> list[tuple[str, float, float]]:
        with db() as conn:
            rows = conn.execute(
                "SELECT date, close, volume FROM index_daily WHERE code = ? ORDER BY date ASC",
                (code,),
            ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]


class FeatureRepo:
    @staticmethod
    def get_latest(code: str) -> dict | None:
        with db() as conn:
            row = conn.execute(
                "SELECT date, hurst_60d, momentum_20d, calmar, downside_vol, "
                "capture_up, capture_down, bias_60d, "
                "rbsa_industry_1, rbsa_weight_1, "
                "rbsa_industry_2, rbsa_weight_2, "
                "rbsa_industry_3, rbsa_weight_3 "
                "FROM fund_features WHERE code = ? ORDER BY date DESC LIMIT 1",
                (code,),
            ).fetchone()
        if not row:
            return None
        keys = ["date", "hurst_60d", "momentum_20d", "calmar", "downside_vol",
                "capture_up", "capture_down", "bias_60d",
                "rbsa_industry_1", "rbsa_weight_1",
                "rbsa_industry_2", "rbsa_weight_2",
                "rbsa_industry_3", "rbsa_weight_3"]
        return dict(zip(keys, row))

    @staticmethod
    def get_latest_date(code: str) -> str | None:
        with db() as conn:
            row = conn.execute(
                "SELECT date FROM fund_features WHERE code = ? ORDER BY date DESC LIMIT 1",
                (code,),
            ).fetchone()
        return row[0] if row else None

    @staticmethod
    def get_max_date() -> str | None:
        with db() as conn:
            row = conn.execute("SELECT MAX(date) FROM fund_features").fetchone()
        return row[0] if row else None

    @staticmethod
    def get_momentum_in_sector(sector: str, date: str) -> list[float]:
        with db() as conn:
            rows = conn.execute(
                "SELECT momentum_20d FROM fund_features "
                "WHERE rbsa_industry_1 = ? AND date = ? AND momentum_20d IS NOT NULL",
                (sector, date),
            ).fetchall()
        return [r[0] for r in rows]

    @staticmethod
    def get_industries() -> list[str]:
        with db() as conn:
            rows = conn.execute(
                "SELECT DISTINCT rbsa_industry_1 FROM fund_features "
                "WHERE rbsa_industry_1 IS NOT NULL AND rbsa_industry_1 != '' "
                "UNION "
                "SELECT DISTINCT rbsa_industry_2 FROM fund_features "
                "WHERE rbsa_industry_2 IS NOT NULL AND rbsa_industry_2 != '' "
                "UNION "
                "SELECT DISTINCT rbsa_industry_3 FROM fund_features "
                "WHERE rbsa_industry_3 IS NOT NULL AND rbsa_industry_3 != ''"
            ).fetchall()
        return [r[0] for r in rows]

    @staticmethod
    def get_all_buyable() -> list[tuple[str, str, float, float, float, float, float, float, float,
                                        str, float, str, float, str, float]]:
        with db() as conn:
            return conn.execute(
                "SELECT ff.code, fb.name, ff.momentum_20d, ff.hurst_60d, ff.calmar, "
                "ff.downside_vol, ff.capture_up, ff.capture_down, ff.bias_60d, "
                "ff.rbsa_industry_1, ff.rbsa_weight_1, "
                "ff.rbsa_industry_2, ff.rbsa_weight_2, "
                "ff.rbsa_industry_3, ff.rbsa_weight_3 "
                "FROM fund_features ff JOIN fund_basic fb ON fb.code = ff.code "
                "WHERE fb.is_buyable = 1"
            ).fetchall()


class RecommendLogRepo:
    @staticmethod
    def get_holding_codes(statuses: tuple[str, ...] = ("HOLD", "BUY_MORE", "WARNING")) -> list[tuple]:
        with db() as conn:
            placeholders = ",".join("?" * len(statuses))
            rows = conn.execute(
                f"SELECT code, name, recommend_date, buy_reason, "
                f"(SELECT rbsa_industry_1 FROM fund_features ff "
                f" WHERE ff.code = r.code ORDER BY ff.date DESC LIMIT 1) as sector "
                f"FROM recommend_log r "
                f"WHERE r.status IN ({placeholders})",
                statuses,
            ).fetchall()
        return rows

    @staticmethod
    def get_latest_id(code: str, statuses: tuple[str, ...] = ("HOLD", "BUY_MORE", "WARNING")) -> int | None:
        with db() as conn:
            placeholders = ",".join("?" * len(statuses))
            row = conn.execute(
                f"SELECT id FROM recommend_log WHERE code = ? AND status IN ({placeholders}) "
                f"ORDER BY id DESC LIMIT 1",
                (code, *statuses),
            ).fetchone()
        return row[0] if row else None

    @staticmethod
    def get_entry(code: str, statuses: tuple[str, ...] = ("HOLD", "BUY_MORE", "WARNING")) -> tuple | None:
        with db() as conn:
            placeholders = ",".join("?" * len(statuses))
            row = conn.execute(
                f"SELECT recommend_date, entry_nav FROM recommend_log "
                f"WHERE code = ? AND status IN ({placeholders}) "
                f"ORDER BY id DESC LIMIT 1",
                (code, *statuses),
            ).fetchone()
        return row

    @staticmethod
    def get_entry_nav(code: str, date: str) -> float | None:
        with db() as conn:
            row = conn.execute(
                "SELECT entry_nav FROM recommend_log WHERE code = ? AND recommend_date = ? "
                "ORDER BY id ASC LIMIT 1",
                (code, date),
            ).fetchone()
        return row[0] if row else None

    @staticmethod
    def get_recent_with_funds(limit: int = 2) -> list[dict]:
        with db() as conn:
            rows = conn.execute(
                "SELECT r.id, r.code, fb.name, r.buy_reason, r.status, r.entry_nav, "
                "r.recommend_date, r.highest_nav, r.return_rate, r.sell_reason, r.exit_date "
                "FROM recommend_log r LEFT JOIN fund_basic fb ON fb.code = r.code "
                "ORDER BY r.id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        keys = ["id", "code", "name", "buy_reason", "status", "entry_nav",
                "recommend_date", "highest_nav", "return_rate", "sell_reason", "exit_date"]
        return [dict(zip(keys, r)) for r in rows]

    @staticmethod
    def get_latest_id_and_date() -> tuple[int | None, str | None]:
        with db() as conn:
            row = conn.execute(
                "SELECT id, created_at FROM recommend_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return (row[0], row[1]) if row else (None, None)

    @staticmethod
    def get_detail(code: str) -> list[dict]:
        with db() as conn:
            rows = conn.execute(
                "SELECT r.id, r.code, fb.name, r.buy_reason, r.status, "
                "r.entry_nav, r.highest_nav, r.return_rate, r.sell_reason, "
                "r.recommend_date, r.exit_date, "
                "(SELECT momentum_20d FROM fund_features ff WHERE ff.code = r.code "
                " ORDER BY ff.date DESC LIMIT 1) as momentum_20d, "
                "(SELECT rbsa_industry_1 FROM fund_features ff WHERE ff.code = r.code "
                " ORDER BY ff.date DESC LIMIT 1) as sector "
                "FROM recommend_log r LEFT JOIN fund_basic fb ON fb.code = r.code "
                "WHERE r.code = ? ORDER BY r.id DESC",
                (code,),
            ).fetchall()
        keys = ["id", "code", "name", "buy_reason", "status", "entry_nav",
                "highest_nav", "return_rate", "sell_reason", "recommend_date",
                "exit_date", "momentum_20d", "sector"]
        return [dict(zip(keys, r)) for r in rows]

    @staticmethod
    def update_status(code: str, signal: str,
                      statuses: tuple[str, ...] = ("HOLD", "BUY_MORE", "WARNING")) -> None:
        with db() as conn:
            placeholders = ",".join("?" * len(statuses))
            conn.execute(
                f"UPDATE recommend_log SET status = ? "
                f"WHERE code = ? AND status IN ({placeholders})",
                (signal, code, *statuses),
            )

    @staticmethod
    def update_highest_nav(code: str, highest: float,
                           statuses: tuple[str, ...] = ("HOLD", "BUY_MORE", "WARNING")) -> None:
        with db() as conn:
            placeholders = ",".join("?" * len(statuses))
            conn.execute(
                f"UPDATE recommend_log SET highest_nav = ? "
                f"WHERE code = ? AND status IN ({placeholders})",
                (highest, code, *statuses),
            )

    @staticmethod
    def get_min_date() -> str | None:
        with db() as conn:
            row = conn.execute("SELECT MIN(recommend_date) FROM recommend_log").fetchone()
        return row[0] if row else None

    @staticmethod
    def get_reco_date_of(code: str,
                         statuses: tuple[str, ...] = ("HOLD", "BUY_MORE", "WARNING")) -> str | None:
        with db() as conn:
            placeholders = ",".join("?" * len(statuses))
            row = conn.execute(
                f"SELECT recommend_date FROM recommend_log "
                f"WHERE code = ? AND status IN ({placeholders}) "
                f"ORDER BY id DESC LIMIT 1",
                (code, *statuses),
            ).fetchone()
        return row[0] if row else None
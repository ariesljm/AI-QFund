"""数据库仓库层 Deep Module：封装所有 SQL 查询，统一数据访问 seam。

Interface: 约 20 个方法，隐藏 SQLite 实现、连接管理、表结构细节。
"""

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import json as _json
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


# ============================================================
# 宏观新闻 / 赛道上下文
# ============================================================

def get_latest_macro_news() -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT news_summary, top_gainers, top_losers, etf_net_flow, flow_json, context_json "
            "FROM macro_news ORDER BY date DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    flow = _json.loads(row[4]) if row[4] else {}
    ctx = _json.loads(row[5]) if row[5] else {}
    return {
        "news_summary": row[0] or "",
        "top_gainers": row[1] or "",
        "top_losers": row[2] or "",
        "etf_net_flow": row[3] or "",
        "flow_inflows": flow.get("top_flows", []),
        "flow_outflows": flow.get("top_outflows", []),
        "recommended_sectors": ctx.get("recommended_sectors", []),
        "risk_sectors": ctx.get("risk_sectors", []),
        "sector_reasoning": ctx.get("sector_reasoning", ""),
        "regime_label": ctx.get("regime_label", "NEUTRAL"),
    }


def save_macro_news(date_str: str, news: str, top_gainers: str, top_losers: str, etf_net_flow: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO macro_news (date, news_summary, top_gainers, top_losers, etf_net_flow) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(date) DO UPDATE SET "
            "news_summary=excluded.news_summary, top_gainers=excluded.top_gainers, "
            "top_losers=excluded.top_losers, etf_net_flow=excluded.etf_net_flow",
            (date_str, news, top_gainers, top_losers, etf_net_flow),
        )


def save_flow_data(date_str: str, flow_json: dict) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO macro_news (date, flow_json) VALUES (?, ?) "
            "ON CONFLICT(date) DO UPDATE SET flow_json = excluded.flow_json",
            (date_str, _json.dumps(flow_json, ensure_ascii=False)),
        )


def save_context(date_str: str, context_json: dict) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO macro_news (date, context_json) VALUES (?, ?) "
            "ON CONFLICT(date) DO UPDATE SET context_json = excluded.context_json",
            (date_str, _json.dumps(context_json, ensure_ascii=False)),
        )


def get_cached_context(date_str: str) -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT context_json FROM macro_news WHERE date = ? AND context_json IS NOT NULL",
            (date_str,),
        ).fetchone()
    if row:
        try:
            return _json.loads(row[0])
        except Exception:
            return None
    return None


# ============================================================
# 基基金数据
# ============================================================

def get_buyable_funds() -> list[str]:
    with db() as conn:
        rows = conn.execute("SELECT code FROM fund_basic WHERE is_buyable = 1").fetchall()
    return [r[0] for r in rows]


def get_fund_info(code: str) -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT code, name, type FROM fund_basic WHERE code = ?", (code,)
        ).fetchone()
    return {"code": row[0], "name": row[1], "type": row[2]} if row else None


def get_fund_pool_stats() -> tuple[int, list[dict]]:
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM fund_basic WHERE is_buyable = 1").fetchone()[0]
        by_type = conn.execute(
            "SELECT type, COUNT(*) FROM fund_basic WHERE is_buyable = 1 GROUP BY type ORDER BY COUNT(*) DESC"
        ).fetchall()
    return total, [{"type": t[0] or "其他", "count": t[1]} for t in by_type]


# ============================================================
# 基金特征 / RBSA / 持仓
# ============================================================

def get_fund_features(code: str) -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT hurst_60d, momentum_20d, calmar, downside_vol, capture_up, capture_down, "
            "bias_60d, rbsa_industry_1, rbsa_weight_1 "
            "FROM fund_features WHERE code = ? ORDER BY date DESC LIMIT 1",
            (code,),
        ).fetchone()
    if not row:
        return None
    return {
        "hurst": row[0],
        "momentum": round(row[1] or 0, 2) if row[1] is not None else None,
        "calmar": round(row[2] or 0, 2) if row[2] is not None else None,
        "downside_vol": round(row[3] or 0, 2) if row[3] is not None else None,
        "capture_up": round(row[4] or 0, 1) if row[4] is not None else None,
        "capture_down": round(row[5] or 0, 1) if row[5] is not None else None,
        "bias": round(row[6] or 0, 2) if row[6] is not None else None,
        "top_industry": row[7] or "",
        "top_industry_weight": round(row[8] or 0, 1),
    }


def get_funds_by_sectors(sectors: list[str]) -> list[dict]:
    if not sectors:
        return []
    placeholders = ", ".join("?" * len(sectors))
    params = sectors * 3
    with db() as conn:
        rows = conn.execute(
            f"SELECT ff.code, fb.name, ff.regime, "
            f"ff.rbsa_industry_1, ff.rbsa_weight_1, "
            f"ff.rbsa_industry_2, ff.rbsa_weight_2, "
            f"ff.rbsa_industry_3, ff.rbsa_weight_3 "
            f"FROM fund_features ff JOIN fund_basic fb ON fb.code = ff.code "
            f"WHERE fb.is_buyable = 1 "
            f"AND (ff.rbsa_industry_1 IN ({placeholders}) "
            f"  OR ff.rbsa_industry_2 IN ({placeholders}) "
            f"  OR ff.rbsa_industry_3 IN ({placeholders}))",
            params,
        ).fetchall()
    return [
        {"code": r[0], "name": r[1], "regime": r[2],
         "rbsa_industry_1": r[3], "rbsa_weight_1": r[4],
         "rbsa_industry_2": r[5], "rbsa_weight_2": r[6],
         "rbsa_industry_3": r[7], "rbsa_weight_3": r[8]}
        for r in rows
    ]


def get_available_sectors() -> list[str]:
    with db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT rbsa_industry_1 FROM fund_features "
            "WHERE rbsa_industry_1 IS NOT NULL AND rbsa_industry_1 != '' "
            "UNION SELECT DISTINCT rbsa_industry_2 FROM fund_features "
            "WHERE rbsa_industry_2 IS NOT NULL AND rbsa_industry_2 != '' "
            "UNION SELECT DISTINCT rbsa_industry_3 FROM fund_features "
            "WHERE rbsa_industry_3 IS NOT NULL AND rbsa_industry_3 != ''"
        ).fetchall()
    return [r[0] for r in rows]


def get_holdings(code: str) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT h.stock_code, h.stock_name, h.weight, i.industry_name "
            "FROM fund_holdings h "
            "LEFT JOIN stock_industry_map i ON h.stock_code = i.stock_code "
            "WHERE h.code = ? AND h.report_date = ("
            "  SELECT MAX(report_date) FROM fund_holdings WHERE code = ?) "
            "ORDER BY h.weight DESC LIMIT 10",
            (code, code),
        ).fetchall()
    return [{"code": r[0], "name": r[1], "weight": r[2], "industry": r[3] or ""} for r in rows]


# ============================================================
# 推荐记录
# ============================================================

def get_latest_recommendations(limit: int = 2) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT r.id, r.code, fb.name, r.score, r.combo, r.regime, r.buy_reason, r.status, "
            "r.recommend_date, r.return_rate, fb.type "
            "FROM recommend_log r LEFT JOIN fund_basic fb ON fb.code = r.code "
            "ORDER BY r.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        {"id": r[0], "code": r[1], "name": r[2], "score": r[3], "combo": r[4],
         "regime": r[5] or "NEUTRAL", "reason": (r[6] or "").split(" | 否决记录:")[0].strip(),
         "status": r[7], "date": r[8] or "", "return": r[9], "type": r[10] or ""}
        for r in rows
    ]


def get_tracking_list() -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT r.code, fb.name, MIN(r.recommend_date) AS first_date, "
            "COUNT(*) AS rec_count, MAX(r.status) AS status, MAX(r.exit_date) AS exit_date "
            "FROM recommend_log r LEFT JOIN fund_basic fb ON fb.code = r.code "
            "GROUP BY r.code ORDER BY MAX(r.recommend_date) DESC"
        ).fetchall()
    return [
        {"code": r[0], "name": r[1] or "", "first_date": r[2] or "",
         "rec_count": r[3], "status": r[4] or "HOLD", "exit_date": r[5] or ""}
        for r in rows
    ]


def get_first_reco_date() -> str | None:
    with db() as conn:
        row = conn.execute("SELECT MIN(recommend_date) FROM recommend_log").fetchone()
    return row[0] if row else None


def get_entry_nav(code: str, date: str) -> float | None:
    with db() as conn:
        row = conn.execute(
            "SELECT entry_nav FROM recommend_log WHERE code = ? AND recommend_date = ? ORDER BY id ASC LIMIT 1",
            (code, date),
        ).fetchone()
    return row[0] if row else None


def get_latest_reco_id() -> tuple[int, str]:
    with db() as conn:
        row = conn.execute("SELECT id, created_at FROM recommend_log ORDER BY id DESC LIMIT 1").fetchone()
    return (row[0], row[1]) if row else (0, None)


def get_fund_detail(code: str) -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT r.recommend_date, r.buy_reason, r.score, r.combo, r.regime, "
            "r.entry_nav, r.status, fb.name, fb.type, "
            "(SELECT MIN(r2.recommend_date) FROM recommend_log r2 WHERE r2.code = r.code) AS first_date "
            "FROM recommend_log r LEFT JOIN fund_basic fb ON fb.code = r.code "
            "WHERE r.code = ? ORDER BY r.recommend_date DESC LIMIT 1",
            (code,),
        ).fetchone()
    if not row:
        return None
    return {
        "code": code, "name": row[7] or code, "type": row[8] or "",
        "first_date": row[9] or row[0] or "",
        "entry_nav": round(row[5], 4) if row[5] else None,
        "buy_reason": (row[1] or "").split(" | 否决记录:")[0].strip(),
        "score": row[2], "combo": row[3], "regime": row[4] or "NEUTRAL", "status": row[6] or "HOLD",
    }


# ============================================================
# 净值 / 指数
# ============================================================

def get_nav_history(code: str, limit: int = 60) -> list[tuple[str, float]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT date, cum_nav FROM fund_nav WHERE code = ? ORDER BY date ASC",
            (code,),
        ).fetchall()
    if len(rows) > limit:
        rows = rows[-limit:]
    return [(r[0], r[1]) for r in rows]


def get_index_close(code: str, date: str | None = None) -> float | None:
    with db() as conn:
        if date:
            row = conn.execute(
                "SELECT close FROM index_daily WHERE code = ? AND date <= ? ORDER BY date DESC LIMIT 1",
                (code, date),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT close FROM index_daily WHERE code = ? ORDER BY date DESC LIMIT 1",
                (code,),
            ).fetchone()
    return row[0] if row else None


def get_index_momentum(code: str = "sh000300", days: int = 21) -> float:
    with db() as conn:
        idx = conn.execute(
            "SELECT close FROM index_daily WHERE code = ? ORDER BY date DESC LIMIT ?",
            (code, days),
        ).fetchall()
    return (idx[0][0] / idx[-1][0] - 1) * 100 if len(idx) >= days else 0.0


# ============================================================
# 进化 / 洞察
# ============================================================

def get_sector_insights(limit: int = 5) -> str:
    with db() as conn:
        rows = conn.execute(
            "SELECT insight FROM evolution_insights "
            "WHERE insight_type = 'sector' AND active = 1 AND confidence > 0.3 "
            "ORDER BY created_date DESC LIMIT ?",
            (limit,),
        ).fetchall()
    if not rows:
        return ""
    return "\n".join(f"  - {r[0]}" for r in rows)


# ============================================================
# 元数据
# ============================================================

def get_meta(key: str) -> str | None:
    with db() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(key: str, value: str) -> None:
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


def get_uptime_days() -> int:
    start = get_meta("uptime_start")
    if start:
        return (datetime.now() - datetime.strptime(start, "%Y-%m-%d")).days
    return 365


# ============================================================
# 向后兼容：旧 class-based interface（计划在 C4 重构 monitor 后移除）
# ============================================================

class NavRepo:
    @staticmethod
    def get_nav_since(code: str, since_date: str) -> list[tuple]:
        with db() as conn:
            rows = conn.execute(
                "SELECT date, cum_nav FROM fund_nav WHERE code = ? AND date >= ? ORDER BY date ASC",
                (code, since_date),
            ).fetchall()
        return rows

    @staticmethod
    def get_latest_nav(code: str) -> float | None:
        with db() as conn:
            row = conn.execute(
                "SELECT cum_nav FROM fund_nav WHERE code = ? ORDER BY date DESC LIMIT 1",
                (code,),
            ).fetchone()
        return row[0] if row else None


class FeatureRepo:
    @staticmethod
    def get_latest(code: str) -> dict | None:
        with db() as conn:
            row = conn.execute(
                "SELECT hurst_60d, momentum_20d, calmar, downside_vol, capture_up, capture_down, "
                "bias_60d, rbsa_industry_1, rbsa_weight_1, rbsa_industry_2, rbsa_weight_2, "
                "rbsa_industry_3, rbsa_weight_3, date "
                "FROM fund_features WHERE code = ? ORDER BY date DESC LIMIT 1",
                (code,),
            ).fetchone()
        if not row:
            return None
        return {
            "hurst_60d": row[0], "momentum_20d": row[1], "calmar": row[2],
            "downside_vol": row[3], "capture_up": row[4], "capture_down": row[5],
            "bias_60d": row[6], "rbsa_industry_1": row[7], "rbsa_weight_1": row[8] or 0,
            "rbsa_industry_2": row[9], "rbsa_weight_2": row[10] or 0,
            "rbsa_industry_3": row[11], "rbsa_weight_3": row[12] or 0,
            "date": row[13],
        }

    @staticmethod
    def get_momentum_in_sector(sector: str, date: str) -> list[float]:
        with db() as conn:
            rows = conn.execute(
                "SELECT momentum_20d FROM fund_features "
                "WHERE rbsa_industry_1 = ? AND date = ? AND momentum_20d IS NOT NULL",
                (sector, date),
            ).fetchall()
        return [r[0] for r in rows]


class RecommendLogRepo:
    @staticmethod
    def get_reco_date_of(code: str, statuses: tuple[str, ...] = ("HOLD", "BUY_MORE", "WARNING")) -> str | None:
        placeholders = ",".join("?" * len(statuses))
        with db() as conn:
            row = conn.execute(
                f"SELECT recommend_date FROM recommend_log WHERE code = ? AND status IN ({placeholders}) "
                f"ORDER BY id DESC LIMIT 1",
                (code, *statuses),
            ).fetchone()
        return row[0] if row else None

    @staticmethod
    def get_entry(code: str, statuses: tuple[str, ...] = ("HOLD", "BUY_MORE", "WARNING")) -> dict | None:
        placeholders = ",".join("?" * len(statuses))
        with db() as conn:
            row = conn.execute(
                f"SELECT id, code, recommend_date, entry_nav, status FROM recommend_log "
                f"WHERE code = ? AND status IN ({placeholders}) ORDER BY id DESC LIMIT 1",
                (code, *statuses),
            ).fetchone()
        return {
            "id": row[0], "code": row[1], "recommend_date": row[2],
            "entry_nav": row[3], "status": row[4],
            "entry_nav_val": row[3],  # 向后兼容: 旧代码用 entry[1] 访问 entry_nav
        } if row else None

    @staticmethod
    def get_holding_codes(statuses: tuple[str, ...] = ("HOLD", "BUY_MORE", "WARNING")) -> list[tuple]:
        placeholders = ",".join("?" * len(statuses))
        with db() as conn:
            rows = conn.execute(
                f"SELECT r.code, fb.name, r.recommend_date, r.buy_reason, ff.rbsa_industry_1 "
                f"FROM recommend_log r "
                f"LEFT JOIN fund_basic fb ON fb.code = r.code "
                f"LEFT JOIN fund_features ff ON ff.code = r.code "
                f"WHERE r.status IN ({placeholders}) "
                f"GROUP BY r.code ORDER BY MAX(r.id) DESC",
                statuses,
            ).fetchall()
        return rows

    @staticmethod
    def update_status(code: str, signal: str, statuses: tuple[str, ...]) -> None:
        placeholders = ",".join("?" * len(statuses))
        with db() as conn:
            conn.execute(
                f"UPDATE recommend_log SET status = ? WHERE code = ? AND status IN ({placeholders})",
                (signal, code, *statuses),
            )

    @staticmethod
    def update_highest_nav(code: str, highest: float, statuses: tuple[str, ...]) -> None:
        placeholders = ",".join("?" * len(statuses))
        with db() as conn:
            conn.execute(
                f"UPDATE recommend_log SET highest_nav = ? WHERE code = ? AND status IN ({placeholders})",
                (highest, code, *statuses),
            )

"""数据库仓库层：封装所有 SQL 查询，统一数据访问 seam。"""

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import json as _json

from app.database import db_conn
from app.utils.log import get_logger

logger = get_logger("repo")


@contextmanager
def db():
    """统一连接 seam：复用 database.db_conn（含 WAL + schema 初始化 + 迁移）。"""
    with db_conn() as conn:
        yield conn


# 模型特征列清单（fund_features 表列名，单一来源；recommend/backtest 均从此导入）
FEATURE_COLS = [
    "hurst_60d", "momentum_20d", "calmar", "downside_vol",
    "capture_up", "capture_down", "bias_60d",
]

# 推荐模型前向预测窗口（交易日），训练与回测共用
FORWARD_WINDOW = 20


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


def get_fund_pool_stats() -> tuple[int, list[dict]]:
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM fund_basic WHERE is_buyable = 1").fetchone()[0]
        by_type = conn.execute(
            "SELECT type, COUNT(*) FROM fund_basic WHERE is_buyable = 1 GROUP BY type ORDER BY COUNT(*) DESC"
        ).fetchall()
    return total, [{"type": t[0] or "其他", "count": t[1]} for t in by_type]


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


def get_holdings(code: str, limit: int = 10) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT h.stock_code, h.stock_name, h.weight, i.industry_name "
            "FROM fund_holdings h "
            "LEFT JOIN stock_industry_map i ON h.stock_code = i.stock_code "
            "WHERE h.code = ? AND h.report_date = ("
            "  SELECT MAX(report_date) FROM fund_holdings WHERE code = ?) "
            "ORDER BY h.weight DESC LIMIT ?",
            (code, code, limit),
        ).fetchall()
    return [{"stock_code": r[0], "stock_name": r[1], "weight": r[2], "industry": r[3] or ""} for r in rows]


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


def get_nav_history(code: str, limit: int = 60) -> list[tuple[str, float]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT date, cum_nav FROM fund_nav WHERE code = ? ORDER BY date ASC",
            (code,),
        ).fetchall()
    if len(rows) > limit:
        rows = rows[-limit:]
    return [(r[0], r[1]) for r in rows]


def get_nav_at_date(code: str, date: str) -> float | None:
    """指定日期的累计净值（追踪列表首次净值回退用）。"""
    with db() as conn:
        row = conn.execute(
            "SELECT cum_nav FROM fund_nav WHERE code = ? AND date = ?",
            (code, date),
        ).fetchone()
    return row[0] if row else None


def get_nav_at_or_before(code: str, date: str) -> float | None:
    """截至指定日期最近一条净值（已平仓基金持有期截断用）。"""
    with db() as conn:
        row = conn.execute(
            "SELECT cum_nav FROM fund_nav WHERE code = ? AND date <= ? ORDER BY date DESC LIMIT 1",
            (code, date),
        ).fetchone()
    return row[0] if row else None


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


def get_uptime_days() -> int:
    with db() as conn:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'uptime_start'"
        ).fetchone()
    start = row[0] if row else None
    if start:
        return (datetime.now() - datetime.strptime(start, "%Y-%m-%d")).days
    return 365


# ============================================================
# 监控引擎专用（原 NavRepo/FeatureRepo/RecommendLogRepo）
# ============================================================

def get_nav_since(code: str, since_date: str) -> list[tuple]:
    with db() as conn:
        rows = conn.execute(
            "SELECT date, cum_nav FROM fund_nav WHERE code = ? AND date >= ? ORDER BY date ASC",
            (code, since_date),
        ).fetchall()
    return rows


def get_latest_nav(code: str) -> float | None:
    with db() as conn:
        row = conn.execute(
            "SELECT cum_nav FROM fund_nav WHERE code = ? ORDER BY date DESC LIMIT 1",
            (code,),
        ).fetchone()
    return row[0] if row else None


def get_latest_features(code: str) -> dict | None:
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


def get_momentum_in_sector(sector: str, date: str) -> list[float]:
    with db() as conn:
        rows = conn.execute(
            "SELECT momentum_20d FROM fund_features "
            "WHERE rbsa_industry_1 = ? AND date = ? AND momentum_20d IS NOT NULL",
            (sector, date),
        ).fetchall()
    return [r[0] for r in rows]


def get_reco_date_of(code: str, statuses: tuple[str, ...] = ("HOLD", "BUY_MORE", "WARNING")) -> str | None:
    placeholders = ",".join("?" * len(statuses))
    with db() as conn:
        row = conn.execute(
            f"SELECT recommend_date FROM recommend_log WHERE code = ? AND status IN ({placeholders}) "
            f"ORDER BY id DESC LIMIT 1",
            (code, *statuses),
        ).fetchone()
    return row[0] if row else None


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
    } if row else None


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


def update_status(code: str, signal: str, statuses: tuple[str, ...]) -> None:
    placeholders = ",".join("?" * len(statuses))
    with db() as conn:
        conn.execute(
            f"UPDATE recommend_log SET status = ? WHERE code = ? AND status IN ({placeholders})",
            (signal, code, *statuses),
        )


def update_highest_nav(code: str, highest: float, statuses: tuple[str, ...]) -> None:
    placeholders = ",".join("?" * len(statuses))
    with db() as conn:
        conn.execute(
            f"UPDATE recommend_log SET highest_nav = ? WHERE code = ? AND status IN ({placeholders})",
            (highest, code, *statuses),
        )


# ============================================================
# 引擎/推荐/进化域 数据访问（深化 seam，收编各引擎内联 SQL）
# ============================================================

def get_index_series(code: str = "sh000300",
                     columns: tuple[str, ...] = ("date", "close", "volume", "ma60")) -> list[tuple]:
    """宽基指数日线序列（按日期升序），供特征/训练/回测共用。"""
    cols = ", ".join(columns)
    with db() as conn:
        rows = conn.execute(
            f"SELECT {cols} FROM index_daily WHERE code = ? ORDER BY date ASC",
            (code,),
        ).fetchall()
    return rows


def get_market_regime() -> str:
    """沪深300 close vs ma60 → BULL/BEAR/NEUTRAL（大盘状态机单一来源）。"""
    with db() as conn:
        row = conn.execute(
            "SELECT close, ma60 FROM index_daily WHERE code='sh000300' "
            "AND close IS NOT NULL AND ma60 IS NOT NULL ORDER BY date DESC LIMIT 1"
        ).fetchone()
    if not row or not row[0] or not row[1] or row[1] <= 0:
        return "NEUTRAL"
    return "BULL" if row[0] > row[1] else "BEAR"


def get_rbsa_weight_at_date(code: str, date: str) -> float | None:
    """指定日期的 rbsa_weight_1（监控风格漂移对比买入时点用）。"""
    with db() as conn:
        row = conn.execute(
            "SELECT rbsa_weight_1 FROM fund_features WHERE code = ? AND date = ?",
            (code, date),
        ).fetchone()
    return row[0] if row else None


def get_holding_log_id(code: str, statuses: tuple[str, ...]) -> int | None:
    placeholders = ",".join("?" * len(statuses))
    with db() as conn:
        row = conn.execute(
            f"SELECT id FROM recommend_log WHERE code = ? AND status IN ({placeholders}) "
            f"ORDER BY id DESC LIMIT 1",
            (code, *statuses),
        ).fetchone()
    return row[0] if row else None


def insert_monitor_event(code: str, date: str, signal: str, trailing: bool, drift: bool,
                         sector_adv: bool, logic_verdict: str, sector_risk: bool,
                         holding_risk: bool, detail: str, log_id: int | None) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO monitor_events "
            "(code, date, signal, trigger_trailing, trigger_drift, trigger_sector_adv, "
            "logic_verdict, sector_risk, holding_risk, detail, recommend_log_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (code, date, signal, trailing, drift, sector_adv,
             logic_verdict, sector_risk, holding_risk, detail, log_id),
        )


def exit_position(code: str, sell_reason: str, return_rate: float | None,
                  statuses: tuple[str, ...], today: str) -> None:
    placeholders = ",".join("?" * len(statuses))
    with db() as conn:
        conn.execute(
            "UPDATE recommend_log SET status='EXIT', sell_reason=?, exit_date=?, return_rate=? "
            f"WHERE code=? AND status IN ({placeholders})",
            (sell_reason, today, return_rate, code, *statuses),
        )


def get_active_insights(limit: int = 8) -> list[str]:
    """活跃进化洞察（推荐终选定论用）。"""
    with db() as conn:
        rows = conn.execute(
            "SELECT insight FROM evolution_insights "
            "WHERE active = 1 AND confidence > 0.3 "
            "ORDER BY created_date DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [r[0] for r in rows]


def get_ranking_cfg() -> dict:
    """读取排序权重（meta 表），与默认值合并（推荐/回测共用）。"""
    defaults = {
        "model_weight": 0.5, "rel_strength_weight": 0.15,
        "calmar_weight": 0.1, "hurst_weight": 0.1,
        "momentum_guard_pct": -15.0,
    }
    with db() as conn:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'ranking_cfg'"
        ).fetchone()
    if row:
        try:
            defaults.update({k: v for k, v in _json.loads(row[0]).items() if k in defaults})
        except Exception:
            pass
    return defaults


def save_ranking_cfg(weights: dict) -> None:
    """写入排序权重（进化自纠偏用）。"""
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('ranking_cfg', ?)",
            (_json.dumps(weights),),
        )


def get_sector_candidates(sectors: list[str]) -> list[dict]:
    """赛道内候选基金：fund_features 三行业匹配 + 全部特征列（推荐排序用）。"""
    if not sectors:
        return []
    placeholders = ",".join("?" * len(sectors))
    feat_cols = ", ".join("ff." + c for c in FEATURE_COLS)
    with db() as conn:
        rows = conn.execute(
            f"SELECT ff.code, fb.name, ff.regime, "
            f"ff.rbsa_industry_1, ff.rbsa_weight_1, "
            f"ff.rbsa_industry_2, ff.rbsa_weight_2, "
            f"ff.rbsa_industry_3, ff.rbsa_weight_3, {feat_cols} "
            f"FROM fund_features ff JOIN fund_basic fb ON fb.code = ff.code "
            f"WHERE fb.is_buyable = 1 "
            f"AND (ff.rbsa_industry_1 IN ({placeholders}) "
            f"  OR ff.rbsa_industry_2 IN ({placeholders}) "
            f"  OR ff.rbsa_industry_3 IN ({placeholders}))",
            sectors + sectors + sectors,
        ).fetchall()
    names = (["code", "name", "regime",
              "rbsa_industry_1", "rbsa_weight_1",
              "rbsa_industry_2", "rbsa_weight_2",
              "rbsa_industry_3", "rbsa_weight_3"] + FEATURE_COLS)
    return [dict(zip(names, r)) for r in rows]


def get_all_ranking_rows() -> list[dict]:
    """全市场可投基金特征（推荐降级路径用）。"""
    feat_cols = ", ".join("ff." + c for c in FEATURE_COLS)
    with db() as conn:
        rows = conn.execute(
            f"SELECT ff.code, fb.name, ff.regime, ff.rbsa_industry_1, ff.rbsa_weight_1, {feat_cols} "
            f"FROM fund_features ff JOIN fund_basic fb ON fb.code = ff.code "
            f"WHERE fb.is_buyable = 1 "
            f"AND ff.rbsa_industry_1 IS NOT NULL AND ff.rbsa_industry_1 != ''"
        ).fetchall()
    names = ["code", "name", "regime", "rbsa_industry_1", "rbsa_weight_1"] + FEATURE_COLS
    return [dict(zip(names, r)) for r in rows]


def get_fund_name(code: str) -> str | None:
    with db() as conn:
        row = conn.execute("SELECT name FROM fund_basic WHERE code = ?", (code,)).fetchone()
    return row[0] if row else None


def get_latest_feature_date() -> str | None:
    """fund_features 最新特征日期（赛道中位动量对齐用）。"""
    with db() as conn:
        row = conn.execute("SELECT MAX(date) FROM fund_features").fetchone()
    return row[0] if row else None


def get_latest_holdings_date(code: str) -> str | None:
    """基金最新季报披露日期。"""
    with db() as conn:
        row = conn.execute(
            "SELECT MAX(report_date) FROM fund_holdings WHERE code = ?", (code,)
        ).fetchone()
    return row[0] if row else None


# ============================================================
# 清除推荐决策域
# ============================================================

def clear_recommendations() -> dict:
    """清空推荐决策域：推荐记录、赛道选择、监控事件、进化洞察及推荐结果文件。

    保留底层数据（fund_basic/fund_nav/fund_features 等）与 meta 配置。
    返回各表删除的行数。
    """
    counts: dict[str, int] = {}
    with db() as conn:
        for table in ("recommend_log", "sector_selections", "monitor_events", "evolution_insights"):
            cur = conn.execute(f"DELETE FROM {table}")
            counts[table] = cur.rowcount
    last_reco = Path("data/last_recommendation.txt")
    if last_reco.exists():
        last_reco.unlink()
        counts["last_recommendation.txt"] = 1
    logger.info("清除推荐决策域: %s", counts)
    return counts

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


def get_fund_nav_rows(code: str) -> list[tuple[str, float]]:
    """单只基金全部净值序列（训练样本面板构建用）。"""
    with db() as conn:
        rows = conn.execute(
            "SELECT date, cum_nav FROM fund_nav WHERE code = ? ORDER BY date ASC",
            (code,),
        ).fetchall()
    return [(r[0], r[1]) for r in rows]


def get_train_fund_codes(min_bars: int, limit: int) -> list[str]:
    """随机采样满足最小净值条数的基金代码（训练集构建）。"""
    with db() as conn:
        rows = conn.execute(
            "SELECT code FROM fund_nav GROUP BY code "
            "HAVING COUNT(*) >= ? ORDER BY RANDOM() LIMIT ?",
            (min_bars, limit),
        ).fetchall()
    return [r[0] for r in rows]


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
                     columns: tuple[str, ...] = ("date", "close", "volume", "ma60"),
                     since: str | None = None) -> list[tuple]:
    """宽基指数日线序列（按日期升序），供特征/训练/回测/Web 共用。"""
    cols = ", ".join(columns)
    sql = f"SELECT {cols} FROM index_daily WHERE code = ?"
    params: tuple = (code,)
    if since:
        sql += " AND date >= ?"
        params = (code, since)
    with db() as conn:
        rows = conn.execute(sql + " ORDER BY date ASC", params).fetchall()
    return rows


def get_nav_rows_for_codes(codes: list[str]) -> list[tuple]:
    """多只基金净值行 (code, date, cum_nav)，按日期升序（组合估值用）。"""
    if not codes:
        return []
    placeholders = ",".join("?" * len(codes))
    with db() as conn:
        rows = conn.execute(
            f"SELECT code, date, cum_nav FROM fund_nav "
            f"WHERE code IN ({placeholders}) ORDER BY date ASC",
            tuple(codes),
        ).fetchall()
    return rows


def get_sector_heatmap(limit: int = 6) -> list[tuple]:
    """行业热力图：平均 RBSA 权重与平均动量的 Top 行业。"""
    with db() as conn:
        rows = conn.execute(
            "SELECT rbsa_industry_1, AVG(rbsa_weight_1), AVG(momentum_20d) "
            "FROM fund_features "
            "WHERE rbsa_industry_1 IS NOT NULL AND rbsa_industry_1 != '' "
            "GROUP BY rbsa_industry_1 ORDER BY AVG(rbsa_weight_1) DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return rows


def count_recommendation_domain() -> dict[str, int]:
    """推荐决策域各表行数（清除确认 dry-run 用）。"""
    with db() as conn:
        counts = {
            "recommend_log": conn.execute("SELECT COUNT(*) FROM recommend_log").fetchone()[0],
            "sector_selections": conn.execute("SELECT COUNT(*) FROM sector_selections").fetchone()[0],
            "monitor_events": conn.execute("SELECT COUNT(*) FROM monitor_events").fetchone()[0],
            "evolution_insights": conn.execute("SELECT COUNT(*) FROM evolution_insights").fetchone()[0],
            "quality_metrics": conn.execute("SELECT COUNT(*) FROM quality_metrics").fetchone()[0],
        }
    return counts


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


def get_market_technical_summary() -> str:
    """沪深300最新技术面快照（收盘/涨跌/EMA60/趋势），供 LLM regime 判定注入 prompt。

    返回空串表示数据不足，调用方据此跳过技术面段落。
    """
    with db() as conn:
        rows = conn.execute(
            "SELECT date, close, ma60 FROM index_daily WHERE code='sh000300' "
            "AND close IS NOT NULL ORDER BY date DESC LIMIT 6"
        ).fetchall()
    if not rows or not rows[0][2]:
        return ""
    latest_date, close, ma60 = rows[0]
    prev_close = rows[1][1] if len(rows) > 1 and rows[1][1] else close
    chg = (close - prev_close) / prev_close * 100 if prev_close else 0.0
    pos = "上方" if close > ma60 else "下方"
    trend = " / ".join(f"{r[1]:,.0f}" for r in reversed(rows))
    return (
        f"最新交易日 {latest_date} 沪深300：收盘 {close:,.2f} 点（较上交易日 {chg:+.2f}%），"
        f"EMA60={ma60:,.2f} 点，收盘价位于 EMA60 {pos}；"
        f"近6个交易日收盘点 {trend}"
    )


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


def get_latest_signal(code: str) -> str | None:
    """持仓基金最新信号（监控事件读取 seam，Web 面板共用）。"""
    with db() as conn:
        row = conn.execute(
            "SELECT signal FROM monitor_events WHERE code = ? ORDER BY date DESC, id DESC LIMIT 1",
            (code,),
        ).fetchone()
    return row[0] if row else None


def get_latest_monitor_event(code: str) -> tuple | None:
    """持仓基金最新监控事件完整行 (signal, logic_verdict, sector_risk, holding_risk, detail, date)。"""
    with db() as conn:
        row = conn.execute(
            "SELECT signal, logic_verdict, sector_risk, holding_risk, detail, date "
            "FROM monitor_events WHERE code=? ORDER BY date DESC, id DESC LIMIT 1",
            (code,),
        ).fetchone()
    return row if row else None


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


def get_all_insights() -> list[str]:
    """全部洞察文本（去重冲突判断用）。"""
    with db() as conn:
        rows = conn.execute("SELECT insight FROM evolution_insights").fetchall()
    return [r[0] for r in rows]


def insert_insight(insight: str, insight_type: str, created_date: str, active: int = 1) -> None:
    """写入一条进化洞察。"""
    with db() as conn:
        conn.execute(
            "INSERT INTO evolution_insights (insight, insight_type, created_date, active) "
            "VALUES (?, ?, ?, ?)",
            (insight, insight_type, created_date, active),
        )


def list_active_insights() -> list[tuple]:
    """活跃洞察（置信度衰减用），返回 (id, confidence, apply_count)。"""
    with db() as conn:
        rows = conn.execute(
            "SELECT id, confidence, apply_count FROM evolution_insights WHERE active = 1"
        ).fetchall()
    return list(rows)


def update_insight_confidence(insight_id: int, confidence: float, active: int) -> None:
    """更新洞察置信度与活跃状态。"""
    with db() as conn:
        conn.execute(
            "UPDATE evolution_insights SET confidence = ?, active = ? WHERE id = ?",
            (confidence, active, insight_id),
        )


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


def get_buyable_feature_stats() -> list[tuple]:
    """可投基金核心特征快照（进化引擎排分自纠偏用）。"""
    with db() as conn:
        rows = conn.execute(
            "SELECT ff.code, ff.momentum_20d, ff.hurst_60d, ff.calmar "
            "FROM fund_features ff JOIN fund_basic fb ON fb.code=ff.code "
            "WHERE fb.is_buyable=1"
        ).fetchall()
    return list(rows)


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
# 空推荐日
# ============================================================

def record_empty_recommendation(date_str: str, reasoning: str) -> None:
    """记录一个空推荐日：宏观分析判定当天无合适机会（每天一条，可回溯历史）。"""
    with db() as conn:
        conn.execute(
            "INSERT INTO empty_recommendations (date, reasoning) VALUES (?, ?) "
            "ON CONFLICT(date) DO UPDATE SET reasoning = excluded.reasoning",
            (date_str, reasoning),
        )


def get_empty_recommendation(date_str: str | None = None) -> dict | None:
    """读取空推荐日记录；date_str 为空时返回最近一条，无则返回 None。"""
    with db() as conn:
        if date_str:
            row = conn.execute(
                "SELECT date, reasoning FROM empty_recommendations WHERE date = ?",
                (date_str,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT date, reasoning FROM empty_recommendations "
                "ORDER BY date DESC LIMIT 1"
            ).fetchone()
    if not row:
        return None
    return {"date": row[0], "reasoning": row[1] or ""}


def insert_recommendation(date_str: str, code: str, name: str, rank: int, score: float,
                          combo: float, regime: str, buy_reason: str, status: str = "HOLD",
                          feature_snapshot: str | None = None, entry_nav: float | None = None) -> int:
    """写入推荐记录，返回新行 id。status 覆盖 HOLD（正常）/REJECT（风控拦截）。"""
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO recommend_log "
            "(recommend_date, code, name, rank, score, combo, regime, buy_reason, status, "
            "feature_snapshot, entry_nav) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (date_str, code, name, rank, score, combo, regime, buy_reason, status,
             feature_snapshot, entry_nav),
        )
        return cur.lastrowid


def insert_sector_selection(date_str: str, log_id: int, recommended_sectors: list,
                            risk_sectors: list, sector_reasoning: str, regime_label: str) -> None:
    """写入当日赛道选择快照。"""
    with db() as conn:
        conn.execute(
            "INSERT INTO sector_selections (date, recommend_log_id, recommended_sectors, "
            "risk_sectors, sector_reasoning, regime_label) VALUES (?, ?, ?, ?, ?, ?)",
            (date_str, log_id,
             _json.dumps(recommended_sectors, ensure_ascii=False),
             _json.dumps(risk_sectors, ensure_ascii=False),
             sector_reasoning, regime_label),
        )


def get_pending_sector_selections(month: str) -> list[tuple]:
    """当月待结算的赛道选择，返回 (id, recommend_log_id)。"""
    with db() as conn:
        rows = conn.execute(
            "SELECT id, recommend_log_id FROM sector_selections "
            "WHERE date LIKE ? AND (outcome = '待定' OR outcome IS NULL)",
            (f"{month}%",),
        ).fetchall()
    return list(rows)


def get_recommendation_by_id(log_id: int) -> tuple | None:
    """按 id 读取推荐记录 (status, return_rate, recommend_date)。"""
    with db() as conn:
        row = conn.execute(
            "SELECT status, return_rate, recommend_date FROM recommend_log WHERE id = ?",
            (log_id,),
        ).fetchone()
    return row if row else None


def update_sector_selection_outcome(ss_id: int, outcome: str, date: str, note: str) -> None:
    """回填赛道选择的结算结果。"""
    with db() as conn:
        conn.execute(
            "UPDATE sector_selections SET outcome=?, outcome_date=?, outcome_note=? WHERE id=?",
            (outcome, date, note, ss_id),
        )


def get_monthly_cases(month: str) -> list[tuple]:
    """当月推荐案例（赛道选择 + 推荐记录 + 监控信号链），供 LLM 元分析。"""
    with db() as conn:
        rows = conn.execute(
            "SELECT ss.id, ss.recommend_log_id, ss.recommended_sectors, ss.sector_reasoning, "
            "ss.regime_label, ss.outcome, ss.outcome_note, rl.buy_reason, rl.code, rl.name, "
            "me.signal, me.trigger_trailing, me.trigger_drift, me.trigger_sector_adv, "
            "me.logic_verdict, me.sector_risk, me.holding_risk, me.detail "
            "FROM sector_selections ss "
            "LEFT JOIN recommend_log rl ON rl.id = ss.recommend_log_id "
            "LEFT JOIN monitor_events me ON me.recommend_log_id = rl.id "
            "WHERE ss.date LIKE ? AND ss.outcome != '待定' "
            "ORDER BY ss.date DESC LIMIT 20",
            (f"{month}%",),
        ).fetchall()
    return list(rows)


# ============================================================
# 模型训练时间（每周自动重训用）
# ============================================================

def get_model_last_trained() -> str | None:
    """读取最近一次模型训练日期（meta 表），无则返回 None。"""
    with db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'model_last_trained'"
        ).fetchone()
    return row[0] if row else None


def set_model_last_trained(date_str: str) -> None:
    """记录最近一次模型训练日期。"""
    with db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('model_last_trained', ?)",
            (date_str,),
        )


# ============================================================
# 推荐质量度量
# ============================================================

def save_quality_metrics(m: dict) -> None:
    """保存一次质量度量结果（同区间幂等：重复运行覆盖）。"""
    points_json = _json.dumps(m.get("points", []), ensure_ascii=False)
    with db() as conn:
        conn.execute(
            "INSERT INTO quality_metrics "
            "(computed_date, period_start, period_end, ic, excess_win_rate, "
            "mean_excess, cum_excess, sample_count, points_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(period_start, period_end) DO UPDATE SET "
            "computed_date = excluded.computed_date, ic = excluded.ic, "
            "excess_win_rate = excluded.excess_win_rate, mean_excess = excluded.mean_excess, "
            "cum_excess = excluded.cum_excess, sample_count = excluded.sample_count, "
            "points_json = excluded.points_json",
            (m["computed_date"], m.get("period_start"), m.get("period_end"),
             m.get("ic"), m.get("excess_win_rate"), m.get("mean_excess"),
             m.get("cum_excess"), m.get("sample_count", 0), points_json),
        )


def get_quality_metrics(limit: int = 6) -> list[dict]:
    """读取最近 N 次质量度量（新→旧），含累计超额曲线点。"""
    with db() as conn:
        rows = conn.execute(
            "SELECT computed_date, period_start, period_end, ic, excess_win_rate, "
            "mean_excess, cum_excess, sample_count, points_json FROM quality_metrics "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        points = []
        if r[8]:
            try:
                points = _json.loads(r[8])
            except Exception:
                points = []
        out.append({
            "computed_date": r[0], "period_start": r[1], "period_end": r[2],
            "ic": r[3], "excess_win_rate": r[4], "mean_excess": r[5],
            "cum_excess": r[6], "sample_count": r[7], "points": points,
        })
    return out


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
        # quality_metrics 同步清空：数据源 recommend_log 已删，历史质量度量失去统计依据
        for table in ("recommend_log", "sector_selections", "monitor_events",
                      "evolution_insights", "quality_metrics"):
            cur = conn.execute(f"DELETE FROM {table}")
            counts[table] = cur.rowcount
    last_reco = Path("data/last_recommendation.txt")
    if last_reco.exists():
        last_reco.unlink()
        counts["last_recommendation.txt"] = 1
    logger.info("清除推荐决策域: %s", counts)
    return counts

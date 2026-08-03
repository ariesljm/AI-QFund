"""底层数据 seam：fund/nav/index/holdings/features/meta 等可重建的底层数据只读与写入。"""

from datetime import datetime
from pathlib import Path
import json as _json

from app.database import db, meta_get, meta_set
from app import domain
from app.utils.log import get_logger

logger = get_logger("repo")


# 模型特征列清单（fund_features 表列名，单一来源；repo 拼 SQL / 特征计算 / 回测均从此导入）
FEATURE_COLS = domain.FEATURE_COLS

# 推荐模型前向预测窗口（交易日），训练与回测共用（领域常量单一来源）
FORWARD_WINDOW = domain.FORWARD_DAYS
def get_all_nav_rows() -> list[tuple]:
    """全量净值行 (code, date, cum_nav)，按 code/date 升序（回测用）。"""
    with db() as conn:
        rows = conn.execute('SELECT code, date, cum_nav FROM fund_nav ORDER BY code, date ASC').fetchall()
    return list(rows)

def get_all_ranking_rows() -> list[dict]:
    """全市场可投基金特征（推荐降级路径用）。"""
    feat_cols = ', '.join(('ff.' + c for c in FEATURE_COLS))
    with db() as conn:
        rows = conn.execute(f"SELECT ff.code, fb.name, ff.regime, ff.rbsa_industry_1, ff.rbsa_weight_1, {feat_cols} FROM fund_features ff JOIN fund_basic fb ON fb.code = ff.code WHERE fb.is_buyable = 1 AND ff.rbsa_industry_1 IS NOT NULL AND ff.rbsa_industry_1 != ''").fetchall()
    names = ['code', 'name', 'regime', 'rbsa_industry_1', 'rbsa_weight_1'] + FEATURE_COLS
    return [dict(zip(names, r)) for r in rows]

def get_available_sectors() -> list[str]:
    with db() as conn:
        rows = conn.execute("SELECT DISTINCT rbsa_industry_1 FROM fund_features WHERE rbsa_industry_1 IS NOT NULL AND rbsa_industry_1 != '' UNION SELECT DISTINCT rbsa_industry_2 FROM fund_features WHERE rbsa_industry_2 IS NOT NULL AND rbsa_industry_2 != '' UNION SELECT DISTINCT rbsa_industry_3 FROM fund_features WHERE rbsa_industry_3 IS NOT NULL AND rbsa_industry_3 != ''").fetchall()
    return [r[0] for r in rows]

def get_buyable_codes() -> list[str]:
    """可投基金代码全集（特征批量计算用）。"""
    with db() as conn:
        rows = conn.execute('SELECT code FROM fund_basic WHERE is_buyable = 1').fetchall()
    return [r[0] for r in rows]

def get_buyable_feature_stats() -> list[tuple]:
    """可投基金核心特征快照（进化引擎排分自纠偏用）。"""
    with db() as conn:
        rows = conn.execute('SELECT ff.code, ff.momentum_20d, ff.hurst_60d, ff.calmar FROM fund_features ff JOIN fund_basic fb ON fb.code=ff.code WHERE fb.is_buyable=1').fetchall()
    return list(rows)

def get_cached_context(date_str: str) -> dict | None:
    with db() as conn:
        row = conn.execute('SELECT context_json FROM macro_news WHERE date = ? AND context_json IS NOT NULL', (date_str,)).fetchone()
    if row:
        try:
            return _json.loads(row[0])
        except Exception:
            return None
    return None

def get_codes_missing_rbsa() -> list[str]:
    """RBSA 行业暴露缺失的基金（强制重算 RBSA 用）。"""
    with db() as conn:
        rows = conn.execute("SELECT code FROM fund_features WHERE (rbsa_industry_1 IS NULL OR rbsa_industry_1 = '' OR rbsa_industry_1 = '其他')   OR (rbsa_industry_2 IS NULL OR rbsa_industry_2 = '')   OR (rbsa_industry_3 IS NULL OR rbsa_industry_3 = '')").fetchall()
    return [r[0] for r in rows]

def get_feature_codes_before(date: str) -> list[str]:
    """特征日期早于指定日期的基金（行业映射更新后强制重算用）。"""
    with db() as conn:
        rows = conn.execute('SELECT code FROM fund_features WHERE date < ?', (date,)).fetchall()
    return [r[0] for r in rows]

def get_feature_dates_map() -> dict[str, str]:
    """code → 最近特征日期 映射（批量计算跳过判断用）。"""
    with db() as conn:
        rows = conn.execute('SELECT code, date FROM fund_features').fetchall()
    return dict(rows)

def get_fund_name(code: str) -> str | None:
    with db() as conn:
        row = conn.execute('SELECT name FROM fund_basic WHERE code = ?', (code,)).fetchone()
    return row[0] if row else None

def get_fund_nav_rows(code: str, conn=None) -> list[tuple[str, float]]:
    """单只基金全部净值序列（训练样本面板构建用）。

    conn 为内部批量 seam（批量特征计算路径复用连接避免每基金一次连接）；缺省时自开连接。
    """
    sql = 'SELECT date, cum_nav FROM fund_nav WHERE code = ? ORDER BY date ASC'
    if conn is not None:
        rows = conn.execute(sql, (code,)).fetchall()
    else:
        with db() as conn:
            rows = conn.execute(sql, (code,)).fetchall()
    return [(r[0], r[1]) for r in rows]

def get_fund_pool_stats() -> tuple[int, list[dict]]:
    with db() as conn:
        total = conn.execute('SELECT COUNT(*) FROM fund_basic WHERE is_buyable = 1').fetchone()[0]
        by_type = conn.execute('SELECT type, COUNT(*) FROM fund_basic WHERE is_buyable = 1 GROUP BY type ORDER BY COUNT(*) DESC').fetchall()
    return (total, [{'type': t[0] or '其他', 'count': t[1]} for t in by_type])

def get_holdings(code: str, limit: int=10) -> list[dict]:
    with db() as conn:
        rows = conn.execute('SELECT h.stock_code, h.stock_name, h.weight, i.industry_name FROM fund_holdings h LEFT JOIN stock_industry_map i ON h.stock_code = i.stock_code WHERE h.code = ? AND h.report_date = (  SELECT MAX(report_date) FROM fund_holdings WHERE code = ?) ORDER BY h.weight DESC LIMIT ?', (code, code, limit)).fetchall()
    return [{'stock_code': r[0], 'stock_name': r[1], 'weight': r[2], 'industry': r[3] or ''} for r in rows]

def get_index_close(code: str, date: str | None=None) -> float | None:
    with db() as conn:
        if date:
            row = conn.execute('SELECT close FROM index_daily WHERE code = ? AND date <= ? ORDER BY date DESC LIMIT 1', (code, date)).fetchone()
        else:
            row = conn.execute('SELECT close FROM index_daily WHERE code = ? ORDER BY date DESC LIMIT 1', (code,)).fetchone()
    return row[0] if row else None

def get_index_close_on(code: str, date: str) -> float | None:
    """指定日期收盘价（质量度量基准收益用；与 get_index_close 的'最近一条'语义不同）。"""
    with db() as conn:
        row = conn.execute('SELECT close FROM index_daily WHERE code = ? AND date = ?', (code, date)).fetchone()
    return row[0] if row else None

def get_index_momentum(code: str='sh000300', days: int=21) -> float:
    with db() as conn:
        idx = conn.execute('SELECT close FROM index_daily WHERE code = ? ORDER BY date DESC LIMIT ?', (code, days)).fetchall()
    return (idx[0][0] / idx[-1][0] - 1) * 100 if len(idx) >= days else 0.0

def get_index_rows(code: str='sh000300', conn=None) -> list[tuple]:
    """宽基指数日线行 (date, close, volume)，按日期升序（特征计算/回测共用）。

    conn 为内部批量 seam（批量特征计算路径复用连接）；缺省时自开连接。
    """
    sql = 'SELECT date, close, volume FROM index_daily WHERE code = ? ORDER BY date ASC'
    if conn is not None:
        return conn.execute(sql, (code,)).fetchall()
    with db() as conn:
        return conn.execute(sql, (code,)).fetchall()

def get_index_series(code: str='sh000300', columns: tuple[str, ...]=('date', 'close', 'volume', 'ma60'), since: str | None=None) -> list[tuple]:
    """宽基指数日线序列（按日期升序），供特征/训练/回测/Web 共用。"""
    cols = ', '.join(columns)
    sql = f'SELECT {cols} FROM index_daily WHERE code = ?'
    params: tuple = (code,)
    if since:
        sql += ' AND date >= ?'
        params = (code, since)
    with db() as conn:
        rows = conn.execute(sql + ' ORDER BY date ASC', params).fetchall()
    return rows

def get_industry_map() -> dict[str, str]:
    """stock_code → industry_name 全量映射（RBSA 聚合用）。"""
    with db() as conn:
        rows = conn.execute('SELECT stock_code, industry_name FROM stock_industry_map').fetchall()
    return dict(rows)

def get_latest_feature_date() -> str | None:
    """fund_features 最新特征日期（赛道中位动量对齐用）。"""
    with db() as conn:
        row = conn.execute('SELECT MAX(date) FROM fund_features').fetchone()
    return row[0] if row else None

def get_latest_features(code: str) -> dict | None:
    with db() as conn:
        row = conn.execute('SELECT hurst_60d, momentum_20d, calmar, downside_vol, capture_up, capture_down, bias_60d, rbsa_industry_1, rbsa_weight_1, rbsa_industry_2, rbsa_weight_2, rbsa_industry_3, rbsa_weight_3, date FROM fund_features WHERE code = ? ORDER BY date DESC LIMIT 1', (code,)).fetchone()
    if not row:
        return None
    return {'hurst_60d': row[0], 'momentum_20d': row[1], 'calmar': row[2], 'downside_vol': row[3], 'capture_up': row[4], 'capture_down': row[5], 'bias_60d': row[6], 'rbsa_industry_1': row[7], 'rbsa_weight_1': row[8] or 0, 'rbsa_industry_2': row[9], 'rbsa_weight_2': row[10] or 0, 'rbsa_industry_3': row[11], 'rbsa_weight_3': row[12] or 0, 'date': row[13]}

def get_latest_holdings_date(code: str) -> str | None:
    """基金最新季报披露日期。"""
    with db() as conn:
        row = conn.execute('SELECT MAX(report_date) FROM fund_holdings WHERE code = ?', (code,)).fetchone()
    return row[0] if row else None

def get_latest_holdings_rows() -> list[tuple]:
    """全部基金最新报告期持仓行 (code, stock_code, stock_name, weight)（RBSA 预加载用）。"""
    with db() as conn:
        rows = conn.execute('SELECT code, stock_code, stock_name, weight FROM fund_holdings WHERE report_date IN (SELECT MAX(report_date) FROM fund_holdings GROUP BY code)').fetchall()
    return list(rows)

def get_latest_macro_news() -> dict | None:
    with db() as conn:
        row = conn.execute('SELECT news_summary, top_gainers, top_losers, etf_net_flow, flow_json, context_json FROM macro_news ORDER BY date DESC LIMIT 1').fetchone()
    if not row:
        return None
    flow = _json.loads(row[4]) if row[4] else {}
    ctx = _json.loads(row[5]) if row[5] else {}
    return {'news_summary': row[0] or '', 'top_gainers': row[1] or '', 'top_losers': row[2] or '', 'etf_net_flow': row[3] or '', 'flow_inflows': flow.get('top_flows', []), 'flow_outflows': flow.get('top_outflows', []), 'recommended_sectors': ctx.get('recommended_sectors', []), 'risk_sectors': ctx.get('risk_sectors', []), 'sector_reasoning': ctx.get('sector_reasoning', ''), 'regime_label': ctx.get('regime_label', 'NEUTRAL')}

def get_latest_nav(code: str) -> float | None:
    with db() as conn:
        row = conn.execute('SELECT cum_nav FROM fund_nav WHERE code = ? ORDER BY date DESC LIMIT 1', (code,)).fetchone()
    return row[0] if row else None

def get_market_regime() -> str:
    """沪深300 close vs ma60 → BULL/BEAR/NEUTRAL（大盘状态机单一来源）。"""
    with db() as conn:
        row = conn.execute("SELECT close, ma60 FROM index_daily WHERE code='sh000300' AND close IS NOT NULL AND ma60 IS NOT NULL ORDER BY date DESC LIMIT 1").fetchone()
    return domain.regime_from_close_ma60(row[0] if row else None, row[1] if row else None)

def get_market_technical_summary() -> str:
    """沪深300最新技术面快照（收盘/涨跌/EMA60/趋势），供 LLM regime 判定注入 prompt。

    返回空串表示数据不足，调用方据此跳过技术面段落。
    """
    with db() as conn:
        rows = conn.execute("SELECT date, close, ma60 FROM index_daily WHERE code='sh000300' AND close IS NOT NULL ORDER BY date DESC LIMIT 6").fetchall()
    if not rows or not rows[0][2]:
        return ''
    latest_date, close, ma60 = rows[0]
    prev_close = rows[1][1] if len(rows) > 1 and rows[1][1] else close
    chg = (close - prev_close) / prev_close * 100 if prev_close else 0.0
    pos = '上方' if close > ma60 else '下方'
    trend = ' / '.join((f'{r[1]:,.0f}' for r in reversed(rows)))
    return f'最新交易日 {latest_date} 沪深300：收盘 {close:,.2f} 点（较上交易日 {chg:+.2f}%），EMA60={ma60:,.2f} 点，收盘价位于 EMA60 {pos}；近6个交易日收盘点 {trend}'

def get_meta(key: str) -> str | None:
    """读取 meta 配置值（行业映射更新时间等）。"""
    with db() as conn:
        return meta_get(conn, key)


def get_model_last_trained() -> str | None:
    """读取最近一次模型训练日期（meta 表），无则返回 None。"""
    with db() as conn:
        return meta_get(conn, "model_last_trained")

def get_momentum_in_sector(sector: str, date: str) -> list[float]:
    with db() as conn:
        rows = conn.execute('SELECT momentum_20d FROM fund_features WHERE rbsa_industry_1 = ? AND date = ? AND momentum_20d IS NOT NULL', (sector, date)).fetchall()
    return [r[0] for r in rows]

def get_nav_at_date(code: str, date: str) -> float | None:
    """指定日期的累计净值（追踪列表首次净值回退用）。"""
    with db() as conn:
        row = conn.execute('SELECT cum_nav FROM fund_nav WHERE code = ? AND date = ?', (code, date)).fetchone()
    return row[0] if row else None

def get_nav_at_or_before(code: str, date: str) -> float | None:
    """截至指定日期最近一条净值（已平仓基金持有期截断用）。"""
    with db() as conn:
        row = conn.execute('SELECT cum_nav FROM fund_nav WHERE code = ? AND date <= ? ORDER BY date DESC LIMIT 1', (code, date)).fetchone()
    return row[0] if row else None

def get_nav_history(code: str, limit: int=60) -> list[tuple[str, float]]:
    with db() as conn:
        rows = conn.execute('SELECT date, cum_nav FROM fund_nav WHERE code = ? ORDER BY date ASC', (code,)).fetchall()
    if len(rows) > limit:
        rows = rows[-limit:]
    return [(r[0], r[1]) for r in rows]

def get_nav_latest_dates() -> dict[str, str]:
    """code → 最新净值日期 映射（批量计算跳过判断用）。"""
    with db() as conn:
        rows = conn.execute('SELECT code, MAX(date) FROM fund_nav GROUP BY code').fetchall()
    return dict(rows)

def get_nav_rows_for_codes(codes: list[str]) -> list[tuple]:
    """多只基金净值行 (code, date, cum_nav)，按日期升序（组合估值用）。"""
    if not codes:
        return []
    placeholders = ','.join('?' * len(codes))
    with db() as conn:
        rows = conn.execute(f'SELECT code, date, cum_nav FROM fund_nav WHERE code IN ({placeholders}) ORDER BY date ASC', tuple(codes)).fetchall()
    return rows

def get_nav_rows_since(code: str, since_date: str, limit: int) -> list[tuple]:
    """自某日起最近 limit 条净值行 (date, cum_nav)（质量度量前向窗口用）。"""
    with db() as conn:
        rows = conn.execute('SELECT date, cum_nav FROM fund_nav WHERE code = ? AND date >= ? ORDER BY date ASC LIMIT ?', (code, since_date, limit)).fetchall()
    return list(rows)

def get_nav_since(code: str, since_date: str) -> list[tuple]:
    with db() as conn:
        rows = conn.execute('SELECT date, cum_nav FROM fund_nav WHERE code = ? AND date >= ? ORDER BY date ASC', (code, since_date)).fetchall()
    return rows

def get_rbsa_weight_at_date(code: str, date: str) -> float | None:
    """指定日期的 rbsa_weight_1（监控风格漂移对比买入时点用）。"""
    with db() as conn:
        row = conn.execute('SELECT rbsa_weight_1 FROM fund_features WHERE code = ? AND date = ?', (code, date)).fetchone()
    return row[0] if row else None

def get_sector_candidates(sectors: list[str]) -> list[dict]:
    """赛道内候选基金：fund_features 三行业匹配 + 全部特征列（推荐排序用）。"""
    if not sectors:
        return []
    placeholders = ','.join('?' * len(sectors))
    feat_cols = ', '.join(('ff.' + c for c in FEATURE_COLS))
    with db() as conn:
        rows = conn.execute(f'SELECT ff.code, fb.name, ff.regime, ff.rbsa_industry_1, ff.rbsa_weight_1, ff.rbsa_industry_2, ff.rbsa_weight_2, ff.rbsa_industry_3, ff.rbsa_weight_3, {feat_cols} FROM fund_features ff JOIN fund_basic fb ON fb.code = ff.code WHERE fb.is_buyable = 1 AND (ff.rbsa_industry_1 IN ({placeholders})   OR ff.rbsa_industry_2 IN ({placeholders})   OR ff.rbsa_industry_3 IN ({placeholders}))', sectors + sectors + sectors).fetchall()
    names = ['code', 'name', 'regime', 'rbsa_industry_1', 'rbsa_weight_1', 'rbsa_industry_2', 'rbsa_weight_2', 'rbsa_industry_3', 'rbsa_weight_3'] + FEATURE_COLS
    return [dict(zip(names, r)) for r in rows]

def get_sector_heatmap(limit: int=6) -> list[tuple]:
    """行业热力图：平均 RBSA 权重与平均动量的 Top 行业。"""
    with db() as conn:
        rows = conn.execute("SELECT rbsa_industry_1, AVG(rbsa_weight_1), AVG(momentum_20d) FROM fund_features WHERE rbsa_industry_1 IS NOT NULL AND rbsa_industry_1 != '' GROUP BY rbsa_industry_1 ORDER BY AVG(rbsa_weight_1) DESC LIMIT ?", (limit,)).fetchall()
    return rows

def get_train_fund_codes(min_bars: int, limit: int) -> list[str]:
    """随机采样满足最小净值条数的基金代码（训练集构建）。"""
    with db() as conn:
        rows = conn.execute('SELECT code FROM fund_nav GROUP BY code HAVING COUNT(*) >= ? ORDER BY RANDOM() LIMIT ?', (min_bars, limit)).fetchall()
    return [r[0] for r in rows]

def get_system_logs(lines: int = 200, after: int = 0) -> tuple[list[tuple], int, int]:
    """系统日志读取（Web /api/logs 用，避免绕过 repo seam 内联 SQL）。

    after 为上次读取的最大 id（增量游标，轮转/清理后仍可靠）。
    返回 (rows, total, last_id)。
    """
    from app.utils.log import SYSTEM_LOG_TABLE_SQL
    with db() as conn:
        conn.execute(SYSTEM_LOG_TABLE_SQL)
        total = conn.execute("SELECT COUNT(*) FROM system_logs").fetchone()[0]
        if after <= 0:
            rows = conn.execute(
                "SELECT id, ts, level, logger, event, message, correlation_id "
                "FROM system_logs ORDER BY id DESC LIMIT ?",
                (lines,),
            ).fetchall()
            rows.reverse()
        else:
            rows = conn.execute(
                "SELECT id, ts, level, logger, event, message, correlation_id "
                "FROM system_logs WHERE id > ? ORDER BY id LIMIT ?",
                (after, lines),
            ).fetchall()
    last_id = after
    for r in rows:
        last_id = r[0]
    return rows, total, last_id


def get_uptime_days() -> int:
    with db() as conn:
        start = meta_get(conn, "uptime_start")
    if start:
        return (datetime.now() - datetime.strptime(start, '%Y-%m-%d')).days
    return 365

def save_context(date_str: str, context_json: dict) -> None:
    with db() as conn:
        conn.execute('INSERT INTO macro_news (date, context_json) VALUES (?, ?) ON CONFLICT(date) DO UPDATE SET context_json = excluded.context_json', (date_str, _json.dumps(context_json, ensure_ascii=False)))

def save_flow_data(date_str: str, flow_json: dict) -> None:
    with db() as conn:
        conn.execute('INSERT INTO macro_news (date, flow_json) VALUES (?, ?) ON CONFLICT(date) DO UPDATE SET flow_json = excluded.flow_json', (date_str, _json.dumps(flow_json, ensure_ascii=False)))

def save_fund_features(features: dict, conn=None) -> None:
    """写入一条基金特征快照（INSERT OR REPLACE）。

    ``conn`` 为内部批量 seam：特征全量计算（calc_all_features）复用连接避免逐条重开；
    缺省时自开连接。普通调用不需要也不应传 conn。
    """
    sql = 'INSERT OR REPLACE INTO fund_features (code, date, regime, hurst_60d, momentum_20d, calmar, downside_vol, capture_up, capture_down, bias_60d, rbsa_industry_1, rbsa_weight_1, rbsa_industry_2, rbsa_weight_2, rbsa_industry_3, rbsa_weight_3) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)'
    params = (features['code'], features['date'], features['regime'], features.get('hurst_60d'), features.get('momentum_20d'), features.get('calmar'), features.get('downside_vol'), features.get('capture_up'), features.get('capture_down'), features.get('bias_60d'), features.get('rbsa_industry_1', ''), features.get('rbsa_weight_1', 0.0), features.get('rbsa_industry_2', ''), features.get('rbsa_weight_2', 0.0), features.get('rbsa_industry_3', ''), features.get('rbsa_weight_3', 0.0))
    if conn is not None:
        conn.execute(sql, params)
    else:
        with db() as conn:
            conn.execute(sql, params)

def save_macro_news(date_str: str, news: str, top_gainers: str, top_losers: str, etf_net_flow: str) -> None:
    with db() as conn:
        conn.execute('INSERT INTO macro_news (date, news_summary, top_gainers, top_losers, etf_net_flow) VALUES (?, ?, ?, ?, ?) ON CONFLICT(date) DO UPDATE SET news_summary=excluded.news_summary, top_gainers=excluded.top_gainers, top_losers=excluded.top_losers, etf_net_flow=excluded.etf_net_flow', (date_str, news, top_gainers, top_losers, etf_net_flow))

def set_model_last_trained(date_str: str) -> None:
    """记录最近一次模型训练日期。"""
    with db() as conn:
        meta_set(conn, "model_last_trained", date_str)

def trim_fund_features(retention: int, conn=None) -> None:
    """修剪每只基金特征快照至最近 retention 行（防历史快照无限累积）。

    ``conn`` 为内部批量 seam：特征全量计算路径复用连接；缺省时自开连接。
    """
    sql = 'DELETE FROM fund_features WHERE rowid IN (  SELECT rowid FROM (    SELECT rowid, ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) rk    FROM fund_features) WHERE rk > ?)'
    if conn is not None:
        conn.execute(sql, (retention,))
    else:
        with db() as conn:
            conn.execute(sql, (retention,))


__all__ = ["db", "FEATURE_COLS", "FORWARD_WINDOW", "get_all_nav_rows", "get_all_ranking_rows", "get_available_sectors", "get_buyable_codes", "get_buyable_feature_stats", "get_cached_context", "get_codes_missing_rbsa", "get_feature_codes_before", "get_feature_dates_map", "get_fund_name", "get_fund_nav_rows", "get_fund_pool_stats", "get_holdings", "get_index_close", "get_index_close_on", "get_index_momentum", "get_index_rows", "get_index_series", "get_industry_map", "get_latest_feature_date", "get_latest_features", "get_latest_holdings_date", "get_latest_holdings_rows", "get_latest_macro_news", "get_latest_nav", "get_market_regime", "get_market_technical_summary", "get_meta", "get_model_last_trained", "get_momentum_in_sector", "get_nav_at_date", "get_nav_at_or_before", "get_nav_history", "get_nav_latest_dates", "get_nav_rows_for_codes", "get_nav_rows_since", "get_nav_since", "get_rbsa_weight_at_date", "get_sector_candidates", "get_sector_heatmap", "get_system_logs", "get_train_fund_codes", "get_uptime_days", "save_context", "save_flow_data", "save_fund_features", "save_macro_news", "set_model_last_trained", "trim_fund_features"]

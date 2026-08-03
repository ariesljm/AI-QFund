"""推荐决策域 seam：recommend_log/sector_selections/monitor_events/evolution_insights/quality_metrics/empty_recommendations 读写。"""

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import json as _json

from app.database import db_conn
from app import domain
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

# 推荐模型前向预测窗口（交易日），训练与回测共用（领域常量单一来源）
FORWARD_WINDOW = domain.FORWARD_DAYS
def clear_recommendations() -> dict:
    """清空推荐决策域：推荐记录、赛道选择、监控事件、进化洞察及推荐结果文件。

    保留底层数据（fund_basic/fund_nav/fund_features 等）与 meta 配置。
    返回各表删除的行数。
    """
    counts: dict[str, int] = {}
    with db() as conn:
        for table in ('recommend_log', 'sector_selections', 'monitor_events', 'evolution_insights', 'quality_metrics'):
            cur = conn.execute(f'DELETE FROM {table}')
            counts[table] = cur.rowcount
    last_reco = Path('data/last_recommendation.txt')
    if last_reco.exists():
        last_reco.unlink()
        counts['last_recommendation.txt'] = 1
    logger.info('清除推荐决策域: %s', counts)
    return counts

def count_recommendation_domain() -> dict[str, int]:
    """推荐决策域各表行数（清除确认 dry-run 用）。"""
    with db() as conn:
        counts = {'recommend_log': conn.execute('SELECT COUNT(*) FROM recommend_log').fetchone()[0], 'sector_selections': conn.execute('SELECT COUNT(*) FROM sector_selections').fetchone()[0], 'monitor_events': conn.execute('SELECT COUNT(*) FROM monitor_events').fetchone()[0], 'evolution_insights': conn.execute('SELECT COUNT(*) FROM evolution_insights').fetchone()[0], 'quality_metrics': conn.execute('SELECT COUNT(*) FROM quality_metrics').fetchone()[0]}
    return counts

def exit_position(code: str, sell_reason: str, return_rate: float | None, statuses: tuple[str, ...], today: str) -> None:
    placeholders = ','.join('?' * len(statuses))
    with db() as conn:
        conn.execute(f"UPDATE recommend_log SET status='EXIT', sell_reason=?, exit_date=?, return_rate=? WHERE code=? AND status IN ({placeholders})", (sell_reason, today, return_rate, code, *statuses))

def get_active_insights(limit: int=8) -> list[str]:
    """活跃进化洞察（推荐终选定论用）。"""
    with db() as conn:
        rows = conn.execute('SELECT insight FROM evolution_insights WHERE active = 1 AND confidence > 0.3 ORDER BY created_date DESC LIMIT ?', (limit,)).fetchall()
    return [r[0] for r in rows]

def get_all_insights() -> list[str]:
    """全部洞察文本（去重冲突判断用）。"""
    with db() as conn:
        rows = conn.execute('SELECT insight FROM evolution_insights').fetchall()
    return [r[0] for r in rows]

def get_empty_recommendation(date_str: str | None=None) -> dict | None:
    """读取空推荐日记录；date_str 为空时返回最近一条，无则返回 None。"""
    with db() as conn:
        if date_str:
            row = conn.execute('SELECT date, reasoning FROM empty_recommendations WHERE date = ?', (date_str,)).fetchone()
        else:
            row = conn.execute('SELECT date, reasoning FROM empty_recommendations ORDER BY date DESC LIMIT 1').fetchone()
    if not row:
        return None
    return {'date': row[0], 'reasoning': row[1] or ''}

def get_entry(code: str, statuses: tuple[str, ...]=('HOLD', 'BUY_MORE', 'WARNING')) -> dict | None:
    placeholders = ','.join('?' * len(statuses))
    with db() as conn:
        row = conn.execute(f'SELECT id, code, recommend_date, entry_nav, status FROM recommend_log WHERE code = ? AND status IN ({placeholders}) ORDER BY id DESC LIMIT 1', (code, *statuses)).fetchone()
    return {'id': row[0], 'code': row[1], 'recommend_date': row[2], 'entry_nav': row[3], 'status': row[4]} if row else None

def get_entry_nav(code: str, date: str) -> float | None:
    with db() as conn:
        row = conn.execute('SELECT entry_nav FROM recommend_log WHERE code = ? AND recommend_date = ? ORDER BY id ASC LIMIT 1', (code, date)).fetchone()
    return row[0] if row else None

def get_first_reco_date() -> str | None:
    with db() as conn:
        row = conn.execute('SELECT MIN(recommend_date) FROM recommend_log').fetchone()
    return row[0] if row else None

def get_fund_detail(code: str) -> dict | None:
    with db() as conn:
        row = conn.execute('SELECT r.recommend_date, r.buy_reason, r.score, r.combo, r.regime, r.entry_nav, r.status, fb.name, fb.type, (SELECT MIN(r2.recommend_date) FROM recommend_log r2 WHERE r2.code = r.code) AS first_date FROM recommend_log r LEFT JOIN fund_basic fb ON fb.code = r.code WHERE r.code = ? ORDER BY r.recommend_date DESC LIMIT 1', (code,)).fetchone()
    if not row:
        return None
    return {'code': code, 'name': row[7] or code, 'type': row[8] or '', 'first_date': row[9] or row[0] or '', 'entry_nav': round(row[5], 4) if row[5] else None, 'buy_reason': (row[1] or '').split(' | 否决记录:')[0].strip(), 'score': row[2], 'combo': row[3], 'regime': row[4] or 'NEUTRAL', 'status': row[6] or 'HOLD'}

def get_holding_codes(statuses: tuple[str, ...]=('HOLD', 'BUY_MORE', 'WARNING')) -> list[tuple]:
    """持仓基金列表 (code, name, reco_date, buy_reason, sector)。

    sector 优先取推荐入库时的赛道归属（feature_snapshot.sector），
    回退当前 RBSA 第一行业——保证监控证伪与推荐使用同一赛道判定。
    """
    placeholders = ','.join('?' * len(statuses))
    with db() as conn:
        rows = conn.execute(
            f'SELECT r.code, fb.name, r.recommend_date, r.buy_reason, '
            f'COALESCE(NULLIF(json_extract(r.feature_snapshot, \'$.sector\'), \'\'), ff.rbsa_industry_1) AS sector '
            f'FROM recommend_log r LEFT JOIN fund_basic fb ON fb.code = r.code '
            f'LEFT JOIN fund_features ff ON ff.code = r.code '
            f'WHERE r.status IN ({placeholders}) GROUP BY r.code ORDER BY MAX(r.id) DESC',
            statuses,
        ).fetchall()
    return rows

def get_holding_log_id(code: str, statuses: tuple[str, ...]) -> int | None:
    placeholders = ','.join('?' * len(statuses))
    with db() as conn:
        row = conn.execute(f'SELECT id FROM recommend_log WHERE code = ? AND status IN ({placeholders}) ORDER BY id DESC LIMIT 1', (code, *statuses)).fetchone()
    return row[0] if row else None

def get_latest_monitor_event(code: str) -> tuple | None:
    """持仓基金最新监控事件完整行 (signal, logic_verdict, sector_risk, holding_risk, detail, date)。"""
    with db() as conn:
        row = conn.execute('SELECT signal, logic_verdict, sector_risk, holding_risk, detail, date FROM monitor_events WHERE code=? ORDER BY date DESC, id DESC LIMIT 1', (code,)).fetchone()
    return row if row else None

def get_latest_reco_id() -> tuple[int, str]:
    with db() as conn:
        row = conn.execute('SELECT id, created_at FROM recommend_log ORDER BY id DESC LIMIT 1').fetchone()
    return (row[0], row[1]) if row else (0, None)

def get_latest_recommendations(limit: int=2) -> list[dict]:
    with db() as conn:
        rows = conn.execute('SELECT r.id, r.code, fb.name, r.score, r.combo, r.regime, r.buy_reason, r.status, r.recommend_date, r.return_rate, fb.type FROM recommend_log r LEFT JOIN fund_basic fb ON fb.code = r.code ORDER BY r.id DESC LIMIT ?', (limit,)).fetchall()
    return [{'id': r[0], 'code': r[1], 'name': r[2], 'score': r[3], 'combo': r[4], 'regime': r[5] or 'NEUTRAL', 'reason': (r[6] or '').split(' | 否决记录:')[0].strip(), 'status': r[7], 'date': r[8] or '', 'return': r[9], 'type': r[10] or ''} for r in rows]

def get_latest_signal(code: str) -> str | None:
    """持仓基金最新信号（监控事件读取 seam，Web 面板共用）。"""
    with db() as conn:
        row = conn.execute('SELECT signal FROM monitor_events WHERE code = ? ORDER BY date DESC, id DESC LIMIT 1', (code,)).fetchone()
    return row[0] if row else None

def get_monthly_cases(month: str) -> list[tuple]:
    """当月推荐案例（赛道选择 + 推荐记录 + 监控信号链），供 LLM 元分析。"""
    with db() as conn:
        rows = conn.execute("SELECT ss.id, ss.recommend_log_id, ss.recommended_sectors, ss.sector_reasoning, ss.regime_label, ss.outcome, ss.outcome_note, rl.buy_reason, rl.code, rl.name, me.signal, me.trigger_trailing, me.trigger_drift, me.trigger_sector_adv, me.logic_verdict, me.sector_risk, me.holding_risk, me.detail FROM sector_selections ss LEFT JOIN recommend_log rl ON rl.id = ss.recommend_log_id LEFT JOIN monitor_events me ON me.recommend_log_id = rl.id WHERE ss.date LIKE ? AND ss.outcome != '待定' ORDER BY ss.date DESC LIMIT 20", (f'{month}%',)).fetchall()
    return list(rows)

def get_pending_sector_selections(month: str) -> list[tuple]:
    """当月待结算的赛道选择，返回 (id, recommend_log_id)。"""
    with db() as conn:
        rows = conn.execute("SELECT id, recommend_log_id FROM sector_selections WHERE date LIKE ? AND (outcome = '待定' OR outcome IS NULL)", (f'{month}%',)).fetchall()
    return list(rows)

def get_quality_metrics(limit: int=6) -> list[dict]:
    """读取最近 N 次质量度量（新→旧），含累计超额曲线点。"""
    with db() as conn:
        rows = conn.execute('SELECT computed_date, period_start, period_end, ic, excess_win_rate, mean_excess, cum_excess, sample_count, points_json FROM quality_metrics ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
    out = []
    for r in rows:
        points = []
        if r[8]:
            try:
                points = _json.loads(r[8])
            except Exception:
                points = []
        out.append({'computed_date': r[0], 'period_start': r[1], 'period_end': r[2], 'ic': r[3], 'excess_win_rate': r[4], 'mean_excess': r[5], 'cum_excess': r[6], 'sample_count': r[7], 'points': points})
    return out

def get_quality_sample_rows(period_start: str, period_end: str) -> list[tuple]:
    """区间内有效推荐样本 (code, recommend_date, score)，供质量度量（推荐决策域 read）。"""
    with db() as conn:
        rows = conn.execute('SELECT code, recommend_date, score FROM recommend_log WHERE recommend_date >= ? AND recommend_date <= ? AND score IS NOT NULL AND status != ? ORDER BY recommend_date ASC, code ASC', (period_start, period_end, domain.SIGNAL_REJECT)).fetchall()
    return list(rows)

def get_ranking_cfg() -> dict:
    """读取排序权重（meta 表），与默认值合并（推荐/回测共用）。"""
    defaults = dict(domain.DEFAULT_RANKING_CFG)
    with db() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = 'ranking_cfg'").fetchone()
    if row:
        try:
            defaults.update({k: v for k, v in _json.loads(row[0]).items() if k in defaults})
        except Exception:
            pass
    return defaults

def get_reco_date_of(code: str, statuses: tuple[str, ...]=('HOLD', 'BUY_MORE', 'WARNING')) -> str | None:
    placeholders = ','.join('?' * len(statuses))
    with db() as conn:
        row = conn.execute(f'SELECT recommend_date FROM recommend_log WHERE code = ? AND status IN ({placeholders}) ORDER BY id DESC LIMIT 1', (code, *statuses)).fetchone()
    return row[0] if row else None

def get_recommendation_by_id(log_id: int) -> tuple | None:
    """按 id 读取推荐记录 (status, return_rate, recommend_date)。"""
    with db() as conn:
        row = conn.execute('SELECT status, return_rate, recommend_date FROM recommend_log WHERE id = ?', (log_id,)).fetchone()
    return row if row else None

def get_sector_insights(limit: int=5) -> str:
    with db() as conn:
        rows = conn.execute("SELECT insight FROM evolution_insights WHERE insight_type = 'sector' AND active = 1 AND confidence > 0.3 ORDER BY created_date DESC LIMIT ?", (limit,)).fetchall()
    if not rows:
        return ''
    return '\n'.join((f'  - {r[0]}' for r in rows))

def get_tracking_list() -> list[dict]:
    with db() as conn:
        rows = conn.execute('SELECT r.code, fb.name, MIN(r.recommend_date) AS first_date, COUNT(*) AS rec_count, MAX(r.status) AS status, MAX(r.exit_date) AS exit_date FROM recommend_log r LEFT JOIN fund_basic fb ON fb.code = r.code GROUP BY r.code ORDER BY MAX(r.recommend_date) DESC').fetchall()
    return [{'code': r[0], 'name': r[1] or '', 'first_date': r[2] or '', 'rec_count': r[3], 'status': r[4] or 'HOLD', 'exit_date': r[5] or ''} for r in rows]

def insert_insight(insight: str, insight_type: str, created_date: str, active: int=1) -> None:
    """写入一条进化洞察。"""
    with db() as conn:
        conn.execute('INSERT INTO evolution_insights (insight, insight_type, created_date, active) VALUES (?, ?, ?, ?)', (insight, insight_type, created_date, active))

def insert_monitor_event(code: str, date: str, signal: str, trailing: bool, drift: bool, sector_adv: bool, logic_verdict: str, sector_risk: bool, holding_risk: bool, detail: str, log_id: int | None) -> None:
    with db() as conn:
        conn.execute('INSERT INTO monitor_events (code, date, signal, trigger_trailing, trigger_drift, trigger_sector_adv, logic_verdict, sector_risk, holding_risk, detail, recommend_log_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (code, date, signal, trailing, drift, sector_adv, logic_verdict, sector_risk, holding_risk, detail, log_id))

def insert_recommendation(date_str: str, code: str, name: str, rank: int, score: float, combo: float, regime: str, buy_reason: str, status: str='HOLD', feature_snapshot: str | None=None, entry_nav: float | None=None) -> int:
    """写入推荐记录，返回新行 id。status 覆盖 HOLD（正常）/REJECT（风控拦截）。"""
    with db() as conn:
        cur = conn.execute('INSERT INTO recommend_log (recommend_date, code, name, rank, score, combo, regime, buy_reason, status, feature_snapshot, entry_nav) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (date_str, code, name, rank, score, combo, regime, buy_reason, status, feature_snapshot, entry_nav))
        return cur.lastrowid

def insert_sector_selection(date_str: str, log_id: int, recommended_sectors: list, risk_sectors: list, sector_reasoning: str, regime_label: str) -> None:
    """写入当日赛道选择快照。"""
    with db() as conn:
        conn.execute('INSERT INTO sector_selections (date, recommend_log_id, recommended_sectors, risk_sectors, sector_reasoning, regime_label) VALUES (?, ?, ?, ?, ?, ?)', (date_str, log_id, _json.dumps(recommended_sectors, ensure_ascii=False), _json.dumps(risk_sectors, ensure_ascii=False), sector_reasoning, regime_label))

def list_active_insights() -> list[tuple]:
    """活跃洞察（置信度衰减用），返回 (id, confidence, apply_count)。"""
    with db() as conn:
        rows = conn.execute('SELECT id, confidence, apply_count FROM evolution_insights WHERE active = 1').fetchall()
    return list(rows)

def record_empty_recommendation(date_str: str, reasoning: str) -> None:
    """记录一个空推荐日：宏观分析判定当天无合适机会（每天一条，可回溯历史）。"""
    with db() as conn:
        conn.execute('INSERT INTO empty_recommendations (date, reasoning) VALUES (?, ?) ON CONFLICT(date) DO UPDATE SET reasoning = excluded.reasoning', (date_str, reasoning))

def save_quality_metrics(m: dict) -> None:
    """保存一次质量度量结果（同区间幂等：重复运行覆盖）。"""
    points_json = _json.dumps(m.get('points', []), ensure_ascii=False)
    with db() as conn:
        conn.execute('INSERT INTO quality_metrics (computed_date, period_start, period_end, ic, excess_win_rate, mean_excess, cum_excess, sample_count, points_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(period_start, period_end) DO UPDATE SET computed_date = excluded.computed_date, ic = excluded.ic, excess_win_rate = excluded.excess_win_rate, mean_excess = excluded.mean_excess, cum_excess = excluded.cum_excess, sample_count = excluded.sample_count, points_json = excluded.points_json', (m['computed_date'], m.get('period_start'), m.get('period_end'), m.get('ic'), m.get('excess_win_rate'), m.get('mean_excess'), m.get('cum_excess'), m.get('sample_count', 0), points_json))

def save_ranking_cfg(weights: dict) -> None:
    """写入排序权重（进化自纠偏用）。"""
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('ranking_cfg', ?)", (_json.dumps(weights),))

def update_highest_nav(code: str, highest: float, statuses: tuple[str, ...]) -> None:
    placeholders = ','.join('?' * len(statuses))
    with db() as conn:
        conn.execute(f'UPDATE recommend_log SET highest_nav = ? WHERE code = ? AND status IN ({placeholders})', (highest, code, *statuses))

def update_insight_confidence(insight_id: int, confidence: float, active: int) -> None:
    """更新洞察置信度与活跃状态。"""
    with db() as conn:
        conn.execute('UPDATE evolution_insights SET confidence = ?, active = ? WHERE id = ?', (confidence, active, insight_id))

def update_sector_selection_outcome(ss_id: int, outcome: str, date: str, note: str) -> None:
    """回填赛道选择的结算结果。"""
    with db() as conn:
        conn.execute('UPDATE sector_selections SET outcome=?, outcome_date=?, outcome_note=? WHERE id=?', (outcome, date, note, ss_id))

def update_status(code: str, signal: str, statuses: tuple[str, ...]) -> None:
    placeholders = ','.join('?' * len(statuses))
    with db() as conn:
        conn.execute(f'UPDATE recommend_log SET status = ? WHERE code = ? AND status IN ({placeholders})', (signal, code, *statuses))


__all__ = ["db", "FEATURE_COLS", "FORWARD_WINDOW", "clear_recommendations", "count_recommendation_domain", "exit_position", "get_active_insights", "get_all_insights", "get_empty_recommendation", "get_entry", "get_entry_nav", "get_first_reco_date", "get_fund_detail", "get_holding_codes", "get_holding_log_id", "get_latest_monitor_event", "get_latest_reco_id", "get_latest_recommendations", "get_latest_signal", "get_monthly_cases", "get_pending_sector_selections", "get_quality_metrics", "get_quality_sample_rows", "get_ranking_cfg", "get_reco_date_of", "get_recommendation_by_id", "get_sector_insights", "get_tracking_list", "insert_insight", "insert_monitor_event", "insert_recommendation", "insert_sector_selection", "list_active_insights", "record_empty_recommendation", "save_quality_metrics", "save_ranking_cfg", "update_highest_nav", "update_insight_confidence", "update_sector_selection_outcome", "update_status"]

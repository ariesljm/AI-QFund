"""推荐决策域 seam：recommend_log/sector_selections/monitor_events/evolution_insights/quality_metrics/empty_recommendations 读写。"""

from app.repo import meta_keys as META
from pathlib import Path
import json as _json

from app.database import db_conn, meta_get, meta_set
from app import domain
from app.utils.log import get_logger

logger = get_logger("repo")


def clear_recommendations() -> dict:
    """清空推荐决策域：推荐记录、赛道选择、监控事件、进化洞察、每日宏观摘要及推荐结果文件。

    保留底层数据（fund_basic/fund_nav/fund_features 等）与 meta 配置。
    返回各表删除的行数。
    """
    counts: dict[str, int] = {}
    with db_conn() as conn:
        for table in ('recommend_log', 'sector_selections', 'monitor_events', 'evolution_insights', 'quality_metrics', 'macro_news', 'empty_recommendations'):
            cur = conn.execute(f'DELETE FROM {table}')
            counts[table] = cur.rowcount
    # llm_audit 是技术审计记录（P0-3），不随决策域清除，保留历史供排查
    last_reco = Path('data/last_recommendation.txt')
    if last_reco.exists():
        last_reco.unlink()
        counts['last_recommendation.txt'] = 1
    logger.info('清除推荐决策域: %s', counts)
    return counts

def count_recommendation_domain() -> dict[str, int]:
    """推荐决策域各表行数（清除确认 dry-run 用）。"""
    with db_conn() as conn:
        counts = {'recommend_log': conn.execute('SELECT COUNT(*) FROM recommend_log').fetchone()[0], 'sector_selections': conn.execute('SELECT COUNT(*) FROM sector_selections').fetchone()[0], 'monitor_events': conn.execute('SELECT COUNT(*) FROM monitor_events').fetchone()[0], 'evolution_insights': conn.execute('SELECT COUNT(*) FROM evolution_insights').fetchone()[0], 'quality_metrics': conn.execute('SELECT COUNT(*) FROM quality_metrics').fetchone()[0], 'macro_news': conn.execute('SELECT COUNT(*) FROM macro_news').fetchone()[0], 'empty_recommendations': conn.execute('SELECT COUNT(*) FROM empty_recommendations').fetchone()[0]}
    return counts

def exit_position(code: str, sell_reason: str, return_rate: float | None, statuses: tuple[str, ...], today: str) -> None:
    placeholders = ','.join('?' * len(statuses))
    with db_conn() as conn:
        conn.execute(f"UPDATE recommend_log SET status='EXIT', sell_reason=?, exit_date=?, return_rate=? WHERE code=? AND status IN ({placeholders})", (sell_reason, today, return_rate, code, *statuses))

def get_active_insights(limit: int = 8, exclude_ranking: bool = True) -> list[tuple[int, str]]:
    """活跃进化洞察（推荐终选定论用），返回 (id, insight) 列表。

    exclude_ranking=True（默认）：过滤排分自纠偏报告——量化诊断文本不混入
    LLM 定论 prompt 的"历史教训"（Q7 共识：诊断与投资教训语义分离）。
    """
    sql = ("SELECT id, insight FROM evolution_insights "
           "WHERE active = 1 AND confidence > 0.3")
    if exclude_ranking:
        sql += " AND insight_type != 'ranking'"
    sql += " ORDER BY created_date DESC LIMIT ?"
    with db_conn() as conn:
        rows = conn.execute(sql, (limit,)).fetchall()
    return [(r[0], r[1]) for r in rows]

def get_all_insights() -> list[str]:
    """全部洞察文本（去重冲突判断用）。"""
    with db_conn() as conn:
        rows = conn.execute('SELECT insight FROM evolution_insights').fetchall()
    return [r[0] for r in rows]

def get_empty_recommendation(date_str: str | None=None) -> dict | None:
    """读取空推荐日记录；date_str 为空时返回最近一条，无则返回 None。

    若指定日期当天已存在实际推荐（recommend_log 有条目），空推荐记录视为残留，
    返回 None——修复同日先记录空推荐、后又推荐成功导致的矛盾显示。
    """
    with db_conn() as conn:
        if date_str:
            has_reco = conn.execute(
                'SELECT 1 FROM recommend_log WHERE recommend_date = ? LIMIT 1',
                (date_str,),
            ).fetchone()
            if has_reco:
                return None
            row = conn.execute(
                'SELECT date, reasoning FROM empty_recommendations WHERE date = ?',
                (date_str,),
            ).fetchone()
        else:
            row = conn.execute(
                'SELECT date, reasoning FROM empty_recommendations ORDER BY date DESC LIMIT 1',
            ).fetchone()
    if not row:
        return None
    return {'date': row[0], 'reasoning': row[1] or ''}


def clear_empty_recommendation(date_str: str) -> None:
    """清除指定日期的空推荐记录（同日推荐成功入库后调用，避免与成功推荐并存）。"""
    with db_conn() as conn:
        conn.execute('DELETE FROM empty_recommendations WHERE date = ?', (date_str,))

def get_entry(code: str, statuses: tuple[str, ...]=('HOLD', 'BUY_MORE', 'WARNING')) -> dict | None:
    placeholders = ','.join('?' * len(statuses))
    with db_conn() as conn:
        row = conn.execute(f'SELECT id, code, recommend_date, entry_nav, status FROM recommend_log WHERE code = ? AND status IN ({placeholders}) ORDER BY id DESC LIMIT 1', (code, *statuses)).fetchone()
    return {'id': row[0], 'code': row[1], 'recommend_date': row[2], 'entry_nav': row[3], 'status': row[4]} if row else None

def get_entry_nav(code: str, date: str) -> float | None:
    with db_conn() as conn:
        row = conn.execute('SELECT entry_nav FROM recommend_log WHERE code = ? AND recommend_date = ? ORDER BY id ASC LIMIT 1', (code, date)).fetchone()
    return row[0] if row else None

def get_first_reco_date() -> str | None:
    with db_conn() as conn:
        row = conn.execute('SELECT MIN(recommend_date) FROM recommend_log').fetchone()
    return row[0] if row else None

def get_fund_detail(code: str) -> dict | None:
    with db_conn() as conn:
        row = conn.execute('SELECT r.recommend_date, r.buy_reason, r.score, r.combo, r.regime, r.entry_nav, r.status, fb.name, fb.type, (SELECT MIN(r2.recommend_date) FROM recommend_log r2 WHERE r2.code = r.code) AS first_date FROM recommend_log r LEFT JOIN fund_basic fb ON fb.code = r.code WHERE r.code = ? ORDER BY r.recommend_date DESC LIMIT 1', (code,)).fetchone()
    if not row:
        return None
    return {'code': code, 'name': row[7] or code, 'type': row[8] or '', 'first_date': row[9] or row[0] or '', 'entry_nav': round(row[5], 4) if row[5] else None, 'buy_reason': (row[1] or '').split(' | 否决记录:')[0].strip(), 'score': row[2], 'combo': row[3], 'regime': row[4] or 'NEUTRAL', 'status': row[6] or 'HOLD'}

def get_holding_codes(statuses: tuple[str, ...]=('HOLD', 'BUY_MORE', 'WARNING')) -> list[dict]:
    """持仓基金列表，每行 dict：code/name/reco_date/buy_reason/sector。

    架构深化 B：废止位置元组契约（列序曾是隐式 interface），消费方按名取值。
    sector 优先取推荐入库时的赛道归属（feature_snapshot.sector），
    回退当前 RBSA 第一行业——保证监控证伪与推荐使用同一赛道判定。
    修复：窗口函数取每组最新行（与 get_reco_date_of 的 ORDER BY id DESC LIMIT 1 同口径）；
    fund_features 按每组最新 date 取 RBSA，避免多行 HOLD 并存时取任意行。
    """
    placeholders = ','.join('?' * len(statuses))
    with db_conn() as conn:
        rows = conn.execute(
            f'SELECT r.code, fb.name, r.recommend_date, r.buy_reason, '
            f'COALESCE(NULLIF(json_extract(r.feature_snapshot, \'$.sector\'), \'\'), ff.rbsa_industry_1) AS sector '
            f'FROM ('
            f'  SELECT *, ROW_NUMBER() OVER (PARTITION BY code ORDER BY id DESC) AS rn '
            f'  FROM recommend_log WHERE status IN ({placeholders})'
            f') r '
            f'LEFT JOIN fund_basic fb ON fb.code = r.code '
            f'LEFT JOIN ('
            f'  SELECT code, rbsa_industry_1, '
            f'         ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) AS rn2 '
            f'  FROM fund_features'
            f') ff ON ff.code = r.code AND ff.rn2 = 1 '
            f'WHERE r.rn = 1 ORDER BY r.id DESC',
            statuses,
        ).fetchall()
    return [{'code': r[0], 'name': r[1], 'reco_date': r[2], 'buy_reason': r[3], 'sector': r[4]} for r in rows]


def get_entry_score(code: str) -> float | None:
    """买入时模型分数（recommend_log.score），无记录返回 None。

    架构深化 B：展示副作用隔离——原实现借道 get_fund_detail（连带 buy_reason 切片），
    监控装配只取 score，现经窄读直达，不再消费展示层副作用。
    """
    with db_conn() as conn:
        row = conn.execute('SELECT score FROM recommend_log WHERE code = ? ORDER BY id DESC LIMIT 1', (code,)).fetchone()
    return row[0] if row else None


def get_entry_sector_anchor(code: str,
                            statuses: tuple[str, ...]=('HOLD', 'BUY_MORE', 'WARNING')) -> tuple[list[str], list[str], str] | None:
    """买入时推荐赛道锚点 (recommended_sectors, risk_sectors, sector_reasoning)。

    读 sector_selections 中与最新持仓推荐关联的持久化赛道判断——监控赛道锚点
    （R3a）以此为基准，避免用实时重建的宏观上下文（会随时间漂移）。
    无关联记录返回 None（该基金无赛道锚点，跳过 R3a）。
    """
    placeholders = ','.join('?' * len(statuses))
    with db_conn() as conn:
        row = conn.execute(
            f'SELECT ss.recommended_sectors, ss.risk_sectors, ss.sector_reasoning '
            f'FROM sector_selections ss '
            f'JOIN recommend_log rl ON rl.id = ss.recommend_log_id '
            f'WHERE rl.code = ? AND rl.status IN ({placeholders}) '
            f'ORDER BY ss.id DESC LIMIT 1',
            (code, *statuses),
        ).fetchone()
    if not row:
        return None
    return (_json.loads(row[0] or '[]'), _json.loads(row[1] or '[]'), row[2] or '')

def get_entry_feature_snapshot(code: str, statuses: tuple[str, ...]=('HOLD', 'BUY_MORE', 'WARNING')) -> dict | None:
    """最近一条持仓状态推荐的 feature_snapshot（解析后的 dict）；无记录/无快照返回 None。

    监控风格漂移防线的买入基准读取：推荐时已把完整 RBSA 持久化到快照。
    """
    with db_conn() as conn:
        row = conn.execute(
            'SELECT feature_snapshot FROM recommend_log '
            'WHERE code = ? AND status IN (%s) AND feature_snapshot IS NOT NULL '
            'ORDER BY id DESC LIMIT 1' % ','.join('?' * len(statuses)),
            (code, *statuses)).fetchone()
    if not row or not row[0]:
        return None
    try:
        parsed = _json.loads(row[0])
        return parsed if isinstance(parsed, dict) else None
    except (TypeError, ValueError):
        return None


def get_holding_log_id(code: str, statuses: tuple[str, ...]) -> int | None:
    placeholders = ','.join('?' * len(statuses))
    with db_conn() as conn:
        row = conn.execute(f'SELECT id FROM recommend_log WHERE code = ? AND status IN ({placeholders}) ORDER BY id DESC LIMIT 1', (code, *statuses)).fetchone()
    return row[0] if row else None

def get_latest_macro_news() -> dict | None:
    """读取最近一条宏观摘要（Web 面板展示用，推荐决策域的一部分）。"""
    with db_conn() as conn:
        row = conn.execute('SELECT news_summary, top_gainers, top_losers, etf_net_flow, flow_json, context_json, date FROM macro_news ORDER BY date DESC LIMIT 1').fetchone()
    if not row:
        return None
    flow = _json.loads(row[4]) if row[4] else {}
    ctx = _json.loads(row[5]) if row[5] else {}
    return {'news_summary': row[0] or '', 'top_gainers': row[1] or '', 'top_losers': row[2] or '', 'etf_net_flow': row[3] or '', 'flow_inflows': flow.get('top_flows', []), 'flow_outflows': flow.get('top_outflows', []), 'flow_net_total': flow.get('total_net'), 'recommended_sectors': ctx.get('recommended_sectors', []), 'risk_sectors': ctx.get('risk_sectors', []), 'sector_reasoning': ctx.get('sector_reasoning', ''), 'regime_label': ctx.get('regime_label', 'NEUTRAL'), 'date': row[6] or ''}


def get_latest_monitor_event(code: str) -> dict | None:
    """持仓基金最新监控事件（结构化行，调用方按键取，不再按位置解包裸元组）。"""
    with db_conn() as conn:
        row = conn.execute('SELECT signal, logic_verdict, sector_risk, holding_risk, detail, date, is_stale FROM monitor_events WHERE code=? ORDER BY date DESC, id DESC LIMIT 1', (code,)).fetchone()
    if not row:
        return None
    keys = ["signal", "logic_verdict", "sector_risk", "holding_risk", "detail", "date", "is_stale"]
    return dict(zip(keys, row))

def get_latest_reco_id() -> tuple[int, str]:
    with db_conn() as conn:
        row = conn.execute('SELECT id, created_at FROM recommend_log ORDER BY id DESC LIMIT 1').fetchone()
    return (row[0], row[1]) if row else (0, None)

def get_latest_recommendations(limit: int=2) -> list[dict]:
    """最新推荐：仅取最新推荐日期，同日按基金代码去重。

    修复（8-04）：多行业展开可能使同一基金在同日被重复推荐，旧实现取
    "最新 N 条" 会把同基金的多条记录一起喂给 UI，导致今日推荐显示两只
    相同基金。这里先锁定最新推荐日期，再按 code 去重，历史脏数据也不再进 UI。
    排序按 created_at DESC（最近一次推荐/更新在前——同日幂等更新会刷新
    created_at），保证同日重跑推荐后“最后一次运行推荐的基金”优先展示，
    而非按插入 id 把本次重跑更新过的旧行挤到后面（曾导致本次推荐的基金
    被上次运行的结果顶掉）。
    """
    with db_conn() as conn:
        rows = conn.execute(
            'SELECT r.id, r.code, fb.name, r.score, r.combo, r.regime, r.buy_reason,'
            ' r.status, r.recommend_date, r.return_rate, fb.type'
            ' FROM recommend_log r LEFT JOIN fund_basic fb ON fb.code = r.code'
            ' WHERE r.recommend_date = (SELECT MAX(recommend_date) FROM recommend_log)'
            ' ORDER BY r.created_at DESC, r.id DESC').fetchall()
    seen: set[str] = set()
    out = []
    for r in rows:
        if r[1] in seen:
            continue
        seen.add(r[1])
        out.append({'id': r[0], 'code': r[1], 'name': r[2], 'score': r[3], 'combo': r[4], 'regime': r[5] or 'NEUTRAL', 'reason': (r[6] or '').split(' | 否决记录:')[0].strip(), 'status': r[7], 'date': r[8] or '', 'return': r[9], 'type': r[10] or ''})
        if len(out) >= limit:
            break
    return out

def get_settled_cases_after(ss_id: int, limit: int = 300) -> list[dict]:
    """已结算（outcome 非待定）且 id > ss_id 的推荐案例（元分析增量收集）。

    与历史按月收集同列结构，但不按月过滤：结算由 20 日净值窗口决定，
    晚满窗的案例在下次元分析时按 id 游标自然补入——修复「月 1 号未满窗、
    下月按月查不到」导致月中推荐永久丢失的时间窗错位（get_monthly_cases 已删除）。
    """
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT ss.id, ss.recommend_log_id, ss.recommended_sectors, ss.sector_reasoning, "
            "ss.regime_label, ss.outcome, ss.outcome_note, rl.buy_reason, rl.code, rl.name, "
            "me.signal, me.trigger_trailing, me.trigger_drift, me.trigger_sector_adv, "
            "me.logic_verdict, me.sector_risk, me.holding_risk, me.detail "
            "FROM sector_selections ss "
            "LEFT JOIN recommend_log rl ON rl.id = ss.recommend_log_id "
            "LEFT JOIN monitor_events me ON me.recommend_log_id = rl.id "
            "WHERE ss.id > ? AND ss.outcome != '待定' ORDER BY ss.id LIMIT ?",
            (ss_id, limit)).fetchall()
    cols = ["id", "recommend_log_id", "recommended_sectors", "sector_reasoning",
            "regime_label", "outcome", "outcome_note", "buy_reason", "code", "name",
            "signal", "trigger_trailing", "trigger_drift", "trigger_sector_adv",
            "logic_verdict", "sector_risk", "holding_risk", "detail"]
    return [dict(zip(cols, r)) for r in rows]

def get_pending_sector_selections() -> list[tuple]:
    """全部待结算的赛道选择，返回 (id, recommend_log_id, used_insight_ids, pool_sectors)。

    不按月过滤：进化引擎每月结算"全部待定"，幂等且防跨月遗漏
    （某月漏跑进化时，遗留的待定记录仍会在下次结算中被处理）。
    pool_sectors（P1-5）：量化池内候选赛道 JSON，结算时逐赛道回看 20 日收益。
    """
    with db_conn() as conn:
        rows = conn.execute("SELECT id, recommend_log_id, used_insight_ids, pool_sectors FROM sector_selections WHERE (outcome = '待定' OR outcome IS NULL)").fetchall()
    return list(rows)

def get_quality_metrics(limit: int=6) -> list[dict]:
    """读取最近 N 次质量度量（新→旧），含累计超额曲线点。"""
    with db_conn() as conn:
        rows = conn.execute('SELECT computed_date, period_start, period_end, ic, excess_win_rate, mean_excess, cum_excess, profit_rate, mean_abs_ret, payoff_ratio, sample_count, decision_loss, decision_gap_best, points_json FROM quality_metrics ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
    out = []
    for r in rows:
        points = []
        if r[13]:
            try:
                points = _json.loads(r[13])
            except Exception:
                points = []
        out.append({'computed_date': r[0], 'period_start': r[1], 'period_end': r[2], 'ic': r[3], 'excess_win_rate': r[4], 'mean_excess': r[5], 'cum_excess': r[6], 'profit_rate': r[7], 'mean_abs_ret': r[8], 'payoff_ratio': r[9], 'sample_count': r[10], 'decision_loss': r[11], 'decision_gap_best': r[12], 'points': points})
    return out

def get_quality_sample_rows(period_start: str, period_end: str) -> list[tuple]:
    """区间内有效推荐样本 (code, recommend_date, score, candidate_codes)，供质量度量。"""
    with db_conn() as conn:
        rows = conn.execute('SELECT code, recommend_date, score, candidate_codes FROM recommend_log WHERE recommend_date >= ? AND recommend_date <= ? AND score IS NOT NULL AND status != ? ORDER BY recommend_date ASC, code ASC', (period_start, period_end, domain.SIGNAL_REJECT)).fetchall()
    return list(rows)

def get_ranking_cfg() -> domain.RankingConfig:
    """读取排序权重（meta 表），与默认值合并（推荐/回测/GA 共用单一入口）。

    返回不可变 RankingConfig；meta 中未知字段忽略（字段漂移防护）。
    """
    cfg = domain.RankingConfig()
    with db_conn() as conn:
        raw = meta_get(conn, META.RANKING_CFG)
    if raw:
        try:
            # 仅取 dataclass 字段（方法/类属性如 to_dict/QUALITY_RATIO 不参与构造）
            data = {k: v for k, v in _json.loads(raw).items() if k in cfg.__dataclass_fields__}
            if data:
                cfg = domain.RankingConfig(**data)
        except Exception:
            pass
    return cfg

def get_reco_date_of(code: str, statuses: tuple[str, ...]=('HOLD', 'BUY_MORE', 'WARNING')) -> str | None:
    placeholders = ','.join('?' * len(statuses))
    with db_conn() as conn:
        row = conn.execute(f'SELECT recommend_date FROM recommend_log WHERE code = ? AND status IN ({placeholders}) ORDER BY id DESC LIMIT 1', (code, *statuses)).fetchone()
    return row[0] if row else None

def get_recommendation_by_id(log_id: int) -> tuple | None:
    """按 id 读取推荐记录 (code, status, return_rate, recommend_date, entry_nav)。"""
    with db_conn() as conn:
        row = conn.execute('SELECT code, status, return_rate, recommend_date, entry_nav FROM recommend_log WHERE id = ?', (log_id,)).fetchone()
    return row if row else None

def get_sector_insights(limit: int=5) -> list[tuple[int, str]]:
    """活跃赛道洞察（选赛道 LLM prompt 用），返回 (id, insight) 列表。"""
    with db_conn() as conn:
        rows = conn.execute("SELECT id, insight FROM evolution_insights WHERE insight_type = 'sector' AND active = 1 AND confidence > 0.3 ORDER BY created_date DESC LIMIT ?", (limit,)).fetchall()
    return [(r[0], r[1]) for r in rows]

def get_tracking_list() -> list[dict]:
    """追踪监控列表：每基金一行，rec_count = 被推荐引擎选中的累计运行次数。

    同日幂等更新时 rec_count+1（见 insert_recommendation），跨日多行累加——
    推荐次数语义为“每次运行推荐选中该基金计一次”，而非行数
    （旧实现 COUNT(*) 在同日重跑场景下漏计）。
    """
    with db_conn() as conn:
        rows = conn.execute('SELECT r.code, fb.name, MIN(r.recommend_date) AS first_date, SUM(COALESCE(r.rec_count, 1)) AS rec_count, MAX(r.status) AS status, MAX(r.exit_date) AS exit_date FROM recommend_log r LEFT JOIN fund_basic fb ON fb.code = r.code GROUP BY r.code ORDER BY MAX(r.recommend_date) DESC').fetchall()
    return [{'code': r[0], 'name': r[1] or '', 'first_date': r[2] or '', 'rec_count': r[3], 'status': r[4] or 'HOLD', 'exit_date': r[5] or ''} for r in rows]

def insert_insight(insight: str, insight_type: str, created_date: str, active: int=1,
                   confidence: float | None = None, condition: str | None = None) -> None:
    """写入一条进化洞察（confidence/condition 供元分析透传；旧调用不带则存 NULL）。"""
    with db_conn() as conn:
        conn.execute('INSERT INTO evolution_insights (insight, insight_type, created_date, active, confidence, condition) VALUES (?, ?, ?, ?, ?, ?)',
                     (insight, insight_type, created_date, active, confidence, condition))

def insert_monitor_event(code: str, date: str, signal: str, trailing: bool, drift: bool, sector_adv: bool, logic_verdict: str, sector_risk: bool, holding_risk: bool, detail: str, log_id: int | None, is_stale: bool = False) -> None:
    with db_conn() as conn:
        conn.execute('INSERT INTO monitor_events (code, date, signal, trigger_trailing, trigger_drift, trigger_sector_adv, logic_verdict, sector_risk, holding_risk, detail, recommend_log_id, is_stale) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (code, date, signal, trailing, drift, sector_adv, logic_verdict, sector_risk, holding_risk, detail, log_id, 1 if is_stale else 0))


def insert_monitor_score(code: str, date: str, score: float, model_version: str = "") -> None:
    """写入当日模型预测分（幂等：同 code+date 覆盖）。R1 模型序列确认期的数据源。"""
    with db_conn() as conn:
        conn.execute('INSERT OR REPLACE INTO monitor_scores (code, date, score, model_version) VALUES (?, ?, ?, ?)',
                     (code, date, score, model_version))


def get_recent_scores(code: str, limit: int = 5) -> list[tuple]:
    """最近 limit 条预测序列 (date, score, model_version)，按日期倒序。"""
    with db_conn() as conn:
        return conn.execute(
            'SELECT date, score, model_version FROM monitor_scores WHERE code = ? ORDER BY date DESC LIMIT ?',
            (code, limit)).fetchall()


def get_recent_monitor_signals(code: str, limit: int = 25,
                                include_stale: bool = False) -> list[tuple]:
    """最近 limit 条监控信号 (date, signal)，按日期倒序（WARNING 升级用）。

    include_stale=False（默认）：排除数据告警事件（净值陈旧是数据问题，
    不计入信号升级序列——数据问题与信号问题语义分离）。
    """
    sql = "SELECT date, signal FROM monitor_events WHERE code = ?"
    if not include_stale:
        sql += " AND is_stale = 0"
    sql += " ORDER BY date DESC LIMIT ?"
    with db_conn() as conn:
        return conn.execute(sql, (code, limit)).fetchall()

def insert_recommendation(date_str: str, code: str, name: str, rank: int, score: float, combo: float, regime: str, buy_reason: str, status: str='HOLD', feature_snapshot: str | None=None, entry_nav: float | None=None, candidate_codes: list | None=None) -> int:
    """写入推荐记录，返回新行 id。status 覆盖 HOLD（正常）/REJECT（风控拦截）。

    candidate_codes：当日该赛道候选池代码列表（Q5 裁决损耗观测：LLM 选中 vs 候选池）。
    （同日幂等）同日多次运行推荐引擎（重试/手动重跑）时，同 (recommend_date, code)
    更新原行而非追加——id 保持稳定，避免 monitor_events/sector_selections 引用悬空，
    也杜绝同日同一基金重复推荐记录；同时刷新 created_at 为本次运行时间，
    供 get_latest_recommendations 按“最近一次推荐”排序（UI 今日精选），
    并把 rec_count +1（该基金被推荐引擎选中的运行次数，追踪监控“推荐次数”列）。
    """
    cand_json = _json.dumps(candidate_codes or [], ensure_ascii=False) if candidate_codes is not None else None
    with db_conn() as conn:
        row = conn.execute(
            'SELECT id FROM recommend_log WHERE recommend_date = ? AND code = ?',
            (date_str, code)).fetchone()
        if row:
            conn.execute(
                'UPDATE recommend_log SET name=?, rank=?, score=?, combo=?, regime=?, '
                'buy_reason=?, status=?, feature_snapshot=?, entry_nav=?, candidate_codes=?, '
                'rec_count=COALESCE(rec_count, 0) + 1, created_at=datetime(\'now\') '
                'WHERE id=?',
                (name, rank, score, combo, regime, buy_reason, status, feature_snapshot,
                 entry_nav, cand_json, row[0]))
            return row[0]
        cur = conn.execute('INSERT INTO recommend_log (recommend_date, code, name, rank, score, combo, regime, buy_reason, status, feature_snapshot, entry_nav, candidate_codes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (date_str, code, name, rank, score, combo, regime, buy_reason, status, feature_snapshot, entry_nav, cand_json))
        return cur.lastrowid

def insert_sector_selection(date_str: str, log_id: int, recommended_sectors: list, risk_sectors: list, sector_reasoning: str, regime_label: str, used_insight_ids: list | None = None, pool_sectors: list | None = None) -> None:
    """写入当日赛道选择快照。used_insight_ids：选赛道时实际携带的 sector 洞察 id（Q4 反馈回路关联）。

    pool_sectors（P1-5）：量化池内全部候选赛道——结算时逐赛道回看 20 日收益，
    度量 LLM 否决/未选是否系统性错过上涨赛道（否决反事实）。
    """
    used_json = _json.dumps(used_insight_ids or [], ensure_ascii=False)
    with db_conn() as conn:
        conn.execute('INSERT INTO sector_selections (date, recommend_log_id, recommended_sectors, risk_sectors, sector_reasoning, regime_label, used_insight_ids, pool_sectors) VALUES (?, ?, ?, ?, ?, ?, ?, ?)', (date_str, log_id, _json.dumps(recommended_sectors, ensure_ascii=False), _json.dumps(risk_sectors, ensure_ascii=False), sector_reasoning, regime_label, used_json, _json.dumps(pool_sectors or [], ensure_ascii=False)))


def get_empty_reco_dates(days: int = 60) -> list[str]:
    """近 N 天的空推荐日日期（P1-5 空仓率监控）。"""
    from datetime import datetime as _dt, timedelta as _td
    since = (_dt.now() - _td(days=days)).strftime("%Y-%m-%d")
    with db_conn() as conn:
        rows = conn.execute("SELECT date FROM empty_recommendations WHERE date >= ?", (since,)).fetchall()
    return [r[0] for r in rows]


def get_reco_dates(days: int = 60) -> list[str]:
    """近 N 天有实际推荐入库的日期（P1-5 空仓率监控分母）。"""
    from datetime import datetime as _dt, timedelta as _td
    since = (_dt.now() - _td(days=days)).strftime("%Y-%m-%d")
    with db_conn() as conn:
        rows = conn.execute("SELECT DISTINCT recommend_date FROM recommend_log WHERE recommend_date >= ?", (since,)).fetchall()
    return [r[0] for r in rows]


def get_pool_outcomes_rows() -> list[tuple]:
    """已结算且带池内反事实收益的赛道选择行 (id, date, recommended_sectors, pool_sectors, pool_outcomes)。"""
    with db_conn() as conn:
        return conn.execute(
            "SELECT id, date, recommended_sectors, pool_sectors, pool_outcomes "
            "FROM sector_selections WHERE outcome != '待定' AND pool_outcomes IS NOT NULL"
        ).fetchall()


def mark_insights_applied(insight_ids: list[int], date: str) -> None:
    """标记洞察进入 prompt 使用（apply_count+1、last_applied_date 更新，Q4 反馈回路）。"""
    if not insight_ids:
        return
    ph = ','.join('?' * len(insight_ids))
    with db_conn() as conn:
        conn.execute(f'UPDATE evolution_insights SET apply_count = apply_count + 1, last_applied_date = ? WHERE id IN ({ph})', (date, *insight_ids))


def adjust_insight_confidence(insight_id: int, delta: float) -> None:
    """按采纳结果调整洞察置信度，clamp 到 [0,1]（Q4 反馈回路：胜 +0.05、负 -0.05）。"""
    with db_conn() as conn:
        conn.execute('UPDATE evolution_insights SET confidence = MIN(MAX(confidence + ?, 0.0), 1.0) WHERE id = ?', (delta, insight_id))

def list_active_insights() -> list[tuple]:
    """活跃洞察（置信度衰减用），返回 (id, confidence, apply_count)。"""
    with db_conn() as conn:
        rows = conn.execute('SELECT id, confidence, apply_count FROM evolution_insights WHERE active = 1').fetchall()
    return list(rows)

def record_empty_recommendation(date_str: str, reasoning: str) -> None:
    """记录一个空推荐日：宏观分析判定当天无合适机会（每天一条，可回溯历史）。"""
    with db_conn() as conn:
        conn.execute('INSERT INTO empty_recommendations (date, reasoning) VALUES (?, ?) ON CONFLICT(date) DO UPDATE SET reasoning = excluded.reasoning', (date_str, reasoning))

def save_context(date_str: str, context_json: dict) -> None:
    """写入当日宏观上下文快照（Web 面板"AI赛道分析"展示数据源；覆盖式，非缓存）。"""
    with db_conn() as conn:
        conn.execute('INSERT INTO macro_news (date, context_json) VALUES (?, ?) ON CONFLICT(date) DO UPDATE SET context_json = excluded.context_json', (date_str, _json.dumps(context_json, ensure_ascii=False)))

def save_flow_data(date_str: str, flow_json: dict) -> None:
    """写入当日资金流数据（推荐决策域的一部分）。"""
    with db_conn() as conn:
        conn.execute('INSERT INTO macro_news (date, flow_json) VALUES (?, ?) ON CONFLICT(date) DO UPDATE SET flow_json = excluded.flow_json', (date_str, _json.dumps(flow_json, ensure_ascii=False)))


def save_sector_snapshot(date_str: str, sectors: list[dict]) -> None:
    """持久化当日全行业板块快照（涨跌幅+主力净流入），量化定池面板数据源。

    sectors 为东财板块行（n=名称, c=代码, u=涨跌幅%, zjl=主力净流入万元）；
    覆盖式 upsert，同一 (date, sector_code) 只保留最新一次抓取结果。
    """
    rows = [(
        date_str,
        str(d.get('c') or ''),
        str(d.get('n') or ''),
        float(d.get('u') or 0) if d.get('u') not in (None, '') else None,
        float(d.get('zjl') or 0) if d.get('zjl') not in (None, '') else None,
    ) for d in sectors if d.get('c') and d.get('n')]
    if not rows:
        return
    with db_conn() as conn:
        conn.executemany(
            'INSERT INTO sector_daily_snapshot (date, sector_code, sector_name, pct_chg, net_flow) '
            'VALUES (?, ?, ?, ?, ?) '
            'ON CONFLICT(date, sector_code) DO UPDATE SET '
            'sector_name = excluded.sector_name, pct_chg = excluded.pct_chg, net_flow = excluded.net_flow',
            rows)

def save_macro_news(date_str: str, news: str, top_gainers: str, top_losers: str, etf_net_flow: str) -> None:
    """写入当日宏观摘要（新闻/领涨领跌/资金流，推荐决策域的一部分）。"""
    with db_conn() as conn:
        conn.execute('INSERT INTO macro_news (date, news_summary, top_gainers, top_losers, etf_net_flow) VALUES (?, ?, ?, ?, ?) ON CONFLICT(date) DO UPDATE SET news_summary=excluded.news_summary, top_gainers=excluded.top_gainers, top_losers=excluded.top_losers, etf_net_flow=excluded.etf_net_flow', (date_str, news, top_gainers, top_losers, etf_net_flow))

def save_quality_metrics(m: dict) -> None:
    """保存一次质量度量结果（同区间幂等：重复运行覆盖）。"""
    points_json = _json.dumps(m.get('points', []), ensure_ascii=False)
    with db_conn() as conn:
        conn.execute('INSERT INTO quality_metrics (computed_date, period_start, period_end, ic, excess_win_rate, mean_excess, cum_excess, profit_rate, mean_abs_ret, payoff_ratio, sample_count, decision_loss, decision_gap_best, points_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(period_start, period_end) DO UPDATE SET computed_date = excluded.computed_date, ic = excluded.ic, excess_win_rate = excluded.excess_win_rate, mean_excess = excluded.mean_excess, cum_excess = excluded.cum_excess, profit_rate = excluded.profit_rate, mean_abs_ret = excluded.mean_abs_ret, payoff_ratio = excluded.payoff_ratio, sample_count = excluded.sample_count, decision_loss = excluded.decision_loss, decision_gap_best = excluded.decision_gap_best, points_json = excluded.points_json', (m['computed_date'], m.get('period_start'), m.get('period_end'), m.get('ic'), m.get('excess_win_rate'), m.get('mean_excess'), m.get('cum_excess'), m.get('profit_rate'), m.get('mean_abs_ret'), m.get('payoff_ratio'), m.get('sample_count', 0), m.get('decision_loss'), m.get('decision_gap_best'), points_json))

def save_ranking_cfg(weights: dict) -> None:
    """写入排序权重（进化自纠偏用）。"""
    with db_conn() as conn:
        meta_set(conn, META.RANKING_CFG, _json.dumps(weights))

def update_highest_nav(code: str, highest: float, statuses: tuple[str, ...]) -> None:
    placeholders = ','.join('?' * len(statuses))
    with db_conn() as conn:
        conn.execute(f'UPDATE recommend_log SET highest_nav = ? WHERE code = ? AND status IN ({placeholders})', (highest, code, *statuses))

def update_insight_confidence(insight_id: int, confidence: float, active: int) -> None:
    """更新洞察置信度与活跃状态。"""
    with db_conn() as conn:
        conn.execute('UPDATE evolution_insights SET confidence = ?, active = ? WHERE id = ?', (confidence, active, insight_id))

def update_sector_selection_outcome(ss_id: int, outcome: str, date: str, note: str, pool_outcomes: dict | None = None) -> None:
    """回填赛道选择的结算结果。pool_outcomes（P1-5）：池内各赛道代表基金 20 日收益映射。"""
    with db_conn() as conn:
        if pool_outcomes is not None:
            conn.execute('UPDATE sector_selections SET outcome=?, outcome_date=?, outcome_note=?, pool_outcomes=? WHERE id=?',
                         (outcome, date, note, _json.dumps(pool_outcomes, ensure_ascii=False), ss_id))
        else:
            conn.execute('UPDATE sector_selections SET outcome=?, outcome_date=?, outcome_note=? WHERE id=?',
                         (outcome, date, note, ss_id))

def update_status(code: str, signal: str, statuses: tuple[str, ...]) -> None:
    placeholders = ','.join('?' * len(statuses))
    with db_conn() as conn:
        conn.execute(f'UPDATE recommend_log SET status = ? WHERE code = ? AND status IN ({placeholders})', (signal, code, *statuses))


__all__ = ["clear_recommendations", "clear_empty_recommendation", "count_recommendation_domain", "exit_position", "get_active_insights", "get_all_insights", "get_empty_recommendation", "get_entry", "get_entry_nav", "get_entry_score", "get_first_reco_date", "get_fund_detail", "get_holding_codes", "get_holding_log_id", "get_entry_feature_snapshot", "get_entry_sector_anchor", "get_latest_macro_news", "get_latest_monitor_event", "get_latest_reco_id", "get_latest_recommendations", "get_settled_cases_after", "get_pending_sector_selections", "get_pool_outcomes_rows", "get_quality_metrics", "get_quality_sample_rows", "get_ranking_cfg", "get_reco_date_of", "get_recommendation_by_id", "get_sector_insights", "get_tracking_list", "insert_insight", "insert_monitor_event", "insert_monitor_score", "get_recent_scores", "get_recent_monitor_signals", "insert_recommendation", "insert_sector_selection", "get_empty_reco_dates", "get_reco_dates", "list_active_insights", "record_empty_recommendation", "save_flow_data", "save_macro_news", "save_quality_metrics", "save_context", "save_ranking_cfg", "save_sector_snapshot", "update_highest_nav", "update_insight_confidence", "update_sector_selection_outcome", "update_status", "mark_insights_applied", "adjust_insight_confidence"]

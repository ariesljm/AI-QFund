"""推荐记录生命周期 seam：recommend_log 读写 + 候选批量汇总（跨表，主键 recommend_log）+ 质量度量样本抽取。
"""

import json as _json

from app import domain
from app.database import db_conn
from app.repo import meta_keys as META
from app.repo.base import get_meta
from app.utils.log import get_logger

logger = get_logger("repo")



def clear_recommendations() -> dict:
    """清空推荐决策域：推荐记录、赛道选择、监控事件、进化洞察、每日宏观摘要及推荐结果文件。

    保留底层数据（fund_basic/fund_nav/fund_features 等）与 meta 配置。
    返回各表删除的行数。
    """
    counts: dict[str, int] = {}
    with db_conn() as conn:
        for table in ('recommend_log', 'quality_metrics', 'macro_news'):
            cur = conn.execute(f'DELETE FROM {table}')
            counts[table] = cur.rowcount
    # llm_audit 是技术审计记录（P0-3），不随决策域清除，保留历史供排查
    logger.info('清除推荐决策域: %s', counts)
    return counts


def count_recommendation_domain() -> dict[str, int]:
    """推荐决策域各表行数（清除确认 dry-run 用）。"""
    with db_conn() as conn:
        counts = {'recommend_log': conn.execute('SELECT COUNT(*) FROM recommend_log').fetchone()[0], 'quality_metrics': conn.execute('SELECT COUNT(*) FROM quality_metrics').fetchone()[0], 'macro_news': conn.execute('SELECT COUNT(*) FROM macro_news').fetchone()[0]}
    return counts


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
    # split 仅为历史行兼容：旧数据 buy_reason 曾拼 "| 否决记录:"/"| 决策逻辑:" 尾巴，
    # 新写入已解耦（P2-7 完成态），新行无分隔符时 split 原样返回
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


def get_latest_reco_id() -> tuple[int, str | None]:
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


def get_ranking_cfg() -> domain.RankingConfig:
    """读取排序权重（meta 表），与默认值合并（推荐/回测/GA 共用单一入口）。

    返回不可变 RankingConfig；meta 中未知字段忽略（字段漂移防护）。
    """
    cfg = domain.RankingConfig()
    raw = get_meta(META.RANKING_CFG)
    if raw:
        try:
            # 仅取 dataclass 字段（方法/类属性如 to_dict/QUALITY_RATIO 不参与构造）
            data = {k: v for k, v in _json.loads(raw).items() if k in cfg.__dataclass_fields__}
            if data:
                cfg = domain.RankingConfig(**data)
        except Exception:
            pass
    return cfg


def get_tracking_list() -> list[dict]:
    """追踪监控列表：每基金一行，rec_count = 被推荐引擎选中的累计运行次数。

    同日幂等更新时 rec_count+1（见 insert_recommendation），跨日多行累加——
    推荐次数语义为“每次运行推荐选中该基金计一次”，而非行数
    （旧实现 COUNT(*) 在同日重跑场景下漏计）。
    """
    with db_conn() as conn:
        rows = conn.execute('SELECT r.code, fb.name, MIN(r.recommend_date) AS first_date, SUM(COALESCE(r.rec_count, 1)) AS rec_count, MAX(r.status) AS status, MAX(r.exit_date) AS exit_date FROM recommend_log r LEFT JOIN fund_basic fb ON fb.code = r.code GROUP BY r.code ORDER BY MAX(r.recommend_date) DESC').fetchall()
    return [{'code': r[0], 'name': r[1] or '', 'first_date': r[2] or '', 'rec_count': r[3], 'status': r[4] or 'HOLD', 'exit_date': r[5] or ''} for r in rows]


def insert_recommendation(date_str: str, code: str, name: str, rank: int, score: float, combo: float, regime: str, buy_reason: str, status: str='HOLD', feature_snapshot: str | None=None, entry_nav: float | None=None, candidate_codes: list | None=None, vetoed: list | None=None, reco_path: str='sector', decision_logic: str = '') -> int:
    """写入推荐记录，返回新行 id。status 覆盖 HOLD（正常）/REJECT（风控拦截）。

    candidate_codes：当日该赛道候选池代码列表（Q5 裁决损耗观测：LLM 选中 vs 候选池）。
    vetoed（T07）：LLM 否决记录列表 [{code,name,reason}]——结构化落库供否决审计。
    reco_path（D+分口径）：推荐来源路径 sector/degrade，供质量度量分口径评估。
    decision_logic（P2-7 决策与文案解耦）：内部决策依据独立列，审计用，
    buy_reason 只存展示文案（不再拼否决/决策尾巴，展示层魔法分隔符退役）。
    （同日幂等）同日多次运行推荐引擎（重试/手动重跑）时，同 (recommend_date, code)
    更新原行而非追加——同日同基金幂等（id 稳定），
    也杜绝同日同一基金重复推荐记录；同时刷新 created_at 为本次运行时间，
    供 get_latest_recommendations 按“最近一次推荐”排序（UI 今日精选），
    并把 rec_count +1（该基金被推荐引擎选中的运行次数，追踪监控“推荐次数”列）。
    """
    cand_json = _json.dumps(candidate_codes or [], ensure_ascii=False) if candidate_codes is not None else None
    veto_json = _json.dumps(vetoed or [], ensure_ascii=False) if vetoed is not None else None
    with db_conn() as conn:
        row = conn.execute(
            'SELECT id FROM recommend_log WHERE recommend_date = ? AND code = ?',
            (date_str, code)).fetchone()
        if row:
            conn.execute(
                'UPDATE recommend_log SET name=?, rank=?, score=?, combo=?, regime=?, '
                'buy_reason=?, status=?, feature_snapshot=?, entry_nav=?, candidate_codes=?, '
                'vetoed_json=?, reco_path=?, decision_logic=?, '
                'rec_count=COALESCE(rec_count, 0) + 1, created_at=datetime(\'now\') '
                'WHERE id=?',
                (name, rank, score, combo, regime, buy_reason, status, feature_snapshot,
                 entry_nav, cand_json, veto_json, reco_path, decision_logic, row[0]))
            return row[0]
        cur = conn.execute('INSERT INTO recommend_log (recommend_date, code, name, rank, score, combo, regime, buy_reason, status, feature_snapshot, entry_nav, candidate_codes, vetoed_json, reco_path, decision_logic) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (date_str, code, name, rank, score, combo, regime, buy_reason, status, feature_snapshot, entry_nav, cand_json, veto_json, reco_path, decision_logic))
        return cur.lastrowid or 0


def get_candidate_nav_summaries(items: list[tuple[str, str]]) -> dict[str, dict]:
    """候选列表批量汇总（_candidate_summary N+1 收敛为 4 次查询）。

    items 为 [(code, first_date), ...]；返回 {code: {"entry_nav", "nav_at_first",
    "latest_nav", "signal"}}，无记录字段为 None。
    """
    if not items:
        return {}
    codes = [c for c, _ in items]
    out = {c: {"entry_nav": None, "nav_at_first": None, "latest_nav": None, "signal": None}
           for c, _ in items}
    code_ph = ",".join("?" for _ in codes)
    pair_ph = ",".join("(?,?)" for _ in items)
    pairs = [x for c, d in items for x in (c, d)]
    with db_conn() as conn:
        # 最新净值（窗口函数取每 code 最新一行）
        for code, nav in conn.execute(
            f"SELECT code, cum_nav FROM (SELECT code, cum_nav, "
            f"ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) rk "
            f"FROM fund_nav WHERE code IN ({code_ph})) WHERE rk = 1", codes).fetchall():
            out[code]["latest_nav"] = nav
        # 首次推荐日净值 / entry_nav（(code, date) 成对匹配）
        for code, nav in conn.execute(
            f"SELECT code, cum_nav FROM fund_nav WHERE (code, date) IN ({pair_ph})",
            pairs).fetchall():
            out[code]["nav_at_first"] = nav
        for code, nav in conn.execute(
            f"SELECT code, entry_nav FROM recommend_log WHERE (code, recommend_date) IN ({pair_ph})",
            pairs).fetchall():
            out[code]["entry_nav"] = nav
        # 2.0 状态机状态（tracked_states；无记录时 dashboard 回退推荐状态）
        for code, st in conn.execute(
            f"SELECT object_id, state FROM tracked_states "
            f"WHERE object_type = 'fund' AND object_id IN ({code_ph})",
            codes).fetchall():
            out[code]["signal"] = st
    return out


def get_quality_sample_rows(period_start: str, period_end: str) -> list[tuple]:
    """区间内有效推荐样本 (code, recommend_date, score, candidate_codes, reco_path)，供质量度量。"""
    with db_conn() as conn:
        rows = conn.execute('SELECT code, recommend_date, score, candidate_codes, reco_path FROM recommend_log WHERE recommend_date >= ? AND recommend_date <= ? AND score IS NOT NULL AND status != ? ORDER BY recommend_date ASC, code ASC', (period_start, period_end, domain.SIGNAL_REJECT)).fetchall()
    return list(rows)


def get_e2e_sample_rows(period_start: str, period_end: str) -> list[tuple]:
    """区间内推荐的端到端 P&L 样本 (code, recommend_date, entry_nav, status, exit_date, return_rate)。

    供 compute_e2e_metrics 按实际退出日期算净收益（对比 40 日理论收益）。
    """
    with db_conn() as conn:
        rows = conn.execute(
            'SELECT code, recommend_date, entry_nav, status, exit_date, return_rate '
            'FROM recommend_log WHERE recommend_date >= ? AND recommend_date <= ? '
            'AND status != ? ORDER BY recommend_date ASC, code ASC',
            (period_start, period_end, domain.SIGNAL_REJECT)).fetchall()
    return list(rows)


__all__ = ["clear_recommendations", "count_recommendation_domain", "get_entry_nav", "get_first_reco_date", "get_fund_detail", "get_holding_codes", "get_latest_reco_id", "get_latest_recommendations", "get_ranking_cfg", "get_tracking_list", "insert_recommendation", "get_candidate_nav_summaries", "get_quality_sample_rows", "get_e2e_sample_rows"]

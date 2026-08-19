"""底层数据 seam：fund/nav/index/holdings/features/meta 等可重建的底层数据只读与写入。"""

from datetime import datetime

from app.database import db_conn, meta_get, meta_set
from app import domain
from app.repo import meta_keys as META
from app.utils.log import get_logger

logger = get_logger("repo")


# 模型特征列清单（fund_features 表列名，单一来源；repo 拼 SQL / 特征计算 / 回测均从此导入）
FEATURE_COLS = domain.FEATURE_COLS

# 市场状态列（R1 绝对收益目标配套）：不进 fund_features 表，训练/打分时从指数现算注入
MARKET_COLS = domain.MARKET_COLS

# 推荐模型前向预测窗口（交易日），训练与回测共用（领域常量单一来源）
FORWARD_WINDOW = domain.FORWARD_DAYS
def get_all_ranking_rows() -> list[dict]:
    """全市场可投基金特征（推荐降级路径用）。"""
    feat_cols = ', '.join(('ff.' + c for c in FEATURE_COLS))
    with db_conn() as conn:
        rows = conn.execute(f"SELECT ff.code, fb.name, ff.regime, ff.rbsa_industry_1, ff.rbsa_weight_1, {feat_cols} FROM fund_features ff JOIN fund_basic fb ON fb.code = ff.code WHERE fb.is_buyable = 1 AND ff.rbsa_industry_1 IS NOT NULL AND ff.rbsa_industry_1 != ''").fetchall()
    names = ['code', 'name', 'regime', 'rbsa_industry_1', 'rbsa_weight_1'] + FEATURE_COLS
    return [dict(zip(names, r)) for r in rows]

def get_available_sectors() -> list[str]:
    """可用赛道清单（真实 RBSA 行业，排除空值与兜底'其他'）。

    '其他'是行业映射缺失时的兜底值，不是可投行业；若混入清单，LLM 可能
    从['其他']里选中它，导致无真实行业却降级推荐基金。
    """
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT rbsa_industry_1 FROM fund_features "
            "WHERE rbsa_industry_1 IS NOT NULL AND rbsa_industry_1 NOT IN ('', '其他') "
            "UNION SELECT DISTINCT rbsa_industry_2 FROM fund_features "
            "WHERE rbsa_industry_2 IS NOT NULL AND rbsa_industry_2 NOT IN ('', '其他') "
            "UNION SELECT DISTINCT rbsa_industry_3 FROM fund_features "
            "WHERE rbsa_industry_3 IS NOT NULL AND rbsa_industry_3 NOT IN ('', '其他')"
        ).fetchall()
    return [r[0] for r in rows]

def get_buyable_codes(conn=None) -> list[str]:
    """可投基金代码全集（特征批量计算用）。"""
    with conn or db_conn() as conn:
        rows = conn.execute('SELECT code FROM fund_basic WHERE is_buyable = 1').fetchall()
    return [r[0] for r in rows]

def get_buyable_feature_stats() -> list[tuple]:
    """可投基金核心特征快照（进化引擎排分自纠偏用）。

    只取最新特征日期：fund_features 每基金保留 250 行历史快照，混入旧快照会
    稀释动量/相关性信号导致误报（8-08 数据停摆期间曾报 corr=-1.000 / spread=1.0pp，
    最新日期口径下为 +0.08 / 53pp）。"""
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT ff.code, ff.momentum_20d, ff.hurst_60d, ff.calmar "
            "FROM fund_features ff JOIN fund_basic fb ON fb.code=ff.code "
            "WHERE fb.is_buyable=1 "
            "AND ff.date = (SELECT MAX(date) FROM fund_features)").fetchall()
    return list(rows)

def get_codes_missing_rbsa(conn=None) -> list[str]:
    """RBSA 行业暴露缺失的基金（强制重算 RBSA 用）。"""
    with conn or db_conn() as conn:
        rows = conn.execute("SELECT code FROM fund_features WHERE (rbsa_industry_1 IS NULL OR rbsa_industry_1 = '' OR rbsa_industry_1 = '其他')   OR (rbsa_industry_2 IS NULL OR rbsa_industry_2 = '')   OR (rbsa_industry_3 IS NULL OR rbsa_industry_3 = '')").fetchall()
    return [r[0] for r in rows]

def get_feature_codes_before(date: str, conn=None) -> list[str]:
    """特征日期早于指定日期的基金（行业映射更新后强制重算用）。"""
    with conn or db_conn() as conn:
        rows = conn.execute('SELECT code FROM fund_features WHERE date < ?', (date,)).fetchall()
    return [r[0] for r in rows]

def get_feature_dates_map(conn=None) -> dict[str, str]:
    """code → 最近特征日期 映射（批量计算跳过判断用）。"""
    with conn or db_conn() as conn:
        rows = conn.execute('SELECT code, date FROM fund_features').fetchall()
    return dict(rows)

def get_fund_name(code: str) -> str | None:
    with db_conn() as conn:
        row = conn.execute('SELECT name FROM fund_basic WHERE code = ?', (code,)).fetchone()
    return row[0] if row else None

def get_fund_pool_stats() -> tuple[int, list[dict]]:
    with db_conn() as conn:
        total = conn.execute('SELECT COUNT(*) FROM fund_basic WHERE is_buyable = 1').fetchone()[0]
        by_type = conn.execute('SELECT type, COUNT(*) FROM fund_basic WHERE is_buyable = 1 GROUP BY type ORDER BY COUNT(*) DESC').fetchall()
    return (total, [{'type': t[0] or '其他', 'count': t[1]} for t in by_type])

def get_holdings(code: str, limit: int=10) -> list[dict]:
    with db_conn() as conn:
        rows = conn.execute('SELECT h.stock_code, h.stock_name, h.weight, i.industry_name FROM fund_holdings h LEFT JOIN stock_industry_map i ON h.stock_code = i.stock_code WHERE h.code = ? AND h.report_date = (  SELECT MAX(report_date) FROM fund_holdings WHERE code = ?) ORDER BY h.weight DESC LIMIT ?', (code, code, limit)).fetchall()
    return [{'stock_code': r[0], 'stock_name': r[1], 'weight': r[2], 'industry': r[3] or ''} for r in rows]

def get_holdings_at_report(code: str, report_date: str, limit: int=10) -> list[dict]:
    """按报告期取持仓（R4 对称切片：锚点报告期前 N 大，与最新前 N 大对称比较）。

    历史报告期数据随季报追加保留；无该报告期数据返回空列表（调用方回退快照）。
    """
    with db_conn() as conn:
        rows = conn.execute(
            'SELECT h.stock_code, h.stock_name, h.weight, i.industry_name '
            'FROM fund_holdings h LEFT JOIN stock_industry_map i ON h.stock_code = i.stock_code '
            'WHERE h.code = ? AND h.report_date = ? ORDER BY h.weight DESC LIMIT ?',
            (code, report_date, limit)).fetchall()
    return [{'stock_code': r[0], 'stock_name': r[1], 'weight': r[2], 'industry': r[3] or ''} for r in rows]

def get_index_close(code: str, date: str | None=None) -> float | None:
    with db_conn() as conn:
        if date:
            row = conn.execute('SELECT close FROM index_daily WHERE code = ? AND date <= ? ORDER BY date DESC LIMIT 1', (code, date)).fetchone()
        else:
            row = conn.execute('SELECT close FROM index_daily WHERE code = ? ORDER BY date DESC LIMIT 1', (code,)).fetchone()
    return row[0] if row else None

def get_index_momentum(code: str='sh000300', days: int=21) -> float:
    with db_conn() as conn:
        idx = conn.execute('SELECT close FROM index_daily WHERE code = ? ORDER BY date DESC LIMIT ?', (code, days)).fetchall()
    return (idx[0][0] / idx[-1][0] - 1) * 100 if len(idx) >= days else 0.0

def get_index_rows(code: str='sh000300', conn=None) -> list[tuple]:
    """宽基指数日线行 (date, close, volume)，按日期升序（特征计算/回测共用）。

    conn 为内部批量 seam（批量特征计算路径复用连接）；缺省时自开连接。
    """
    sql = 'SELECT date, close, volume FROM index_daily WHERE code = ? ORDER BY date ASC'
    if conn is not None:
        return conn.execute(sql, (code,)).fetchall()
    with db_conn() as conn:
        return conn.execute(sql, (code,)).fetchall()

def get_index_series(code: str='sh000300', columns: tuple[str, ...]=('date', 'close', 'volume', 'ema60'), since: str | None=None) -> list[tuple]:
    """宽基指数日线序列（按日期升序），供特征/训练/回测/Web 共用。"""
    cols = ', '.join(columns)
    sql = f'SELECT {cols} FROM index_daily WHERE code = ?'
    params: tuple = (code,)
    if since:
        sql += ' AND date >= ?'
        params = (code, since)
    with db_conn() as conn:
        rows = conn.execute(sql + ' ORDER BY date ASC', params).fetchall()
    return rows

def get_industry_map(conn=None) -> dict[str, str]:
    """stock_code → industry_name 全量映射（RBSA 聚合用）。"""
    with conn or db_conn() as conn:
        rows = conn.execute('SELECT stock_code, industry_name FROM stock_industry_map').fetchall()
    return dict(rows)

def get_latest_feature_date() -> str | None:
    """fund_features 最新特征日期（赛道中位动量对齐用）。"""
    with db_conn() as conn:
        row = conn.execute('SELECT MAX(date) FROM fund_features').fetchone()
    return row[0] if row else None

def get_latest_feature_date_before(date_str: str) -> str | None:
    """<= 指定日期的最近特征日（跨日/盘前运行时定池回退用，避免空池误判）。"""
    with db_conn() as conn:
        row = conn.execute(
            'SELECT MAX(date) FROM fund_features WHERE date <= ?', (date_str,)).fetchone()
    return row[0] if row and row[0] else None

def get_latest_features(code: str) -> dict | None:
    with db_conn() as conn:
        row = conn.execute('SELECT hurst_60d, momentum_20d, calmar, downside_vol, capture_up, capture_down, bias_60d, drawdown_60d, reversal_20d, mom_5d, mom_60d, vol_20d, rbsa_industry_1, rbsa_weight_1, rbsa_industry_2, rbsa_weight_2, rbsa_industry_3, rbsa_weight_3, date FROM fund_features WHERE code = ? ORDER BY date DESC LIMIT 1', (code,)).fetchone()
    if not row:
        return None
    return {'hurst_60d': row[0], 'momentum_20d': row[1], 'calmar': row[2], 'downside_vol': row[3], 'capture_up': row[4], 'capture_down': row[5], 'bias_60d': row[6], 'drawdown_60d': row[7], 'reversal_20d': row[8], 'mom_5d': row[9], 'mom_60d': row[10], 'vol_20d': row[11], 'rbsa_industry_1': row[12], 'rbsa_weight_1': row[13] or 0, 'rbsa_industry_2': row[14], 'rbsa_weight_2': row[15] or 0, 'rbsa_industry_3': row[16], 'rbsa_weight_3': row[17] or 0, 'date': row[18]}

def get_latest_holdings_date(code: str) -> str | None:
    """基金最新季报披露日期。"""
    with db_conn() as conn:
        row = conn.execute('SELECT MAX(report_date) FROM fund_holdings WHERE code = ?', (code,)).fetchone()
    return row[0] if row else None

def get_latest_holdings_rows(conn=None) -> list[tuple]:
    """全部基金最新报告期持仓行 (code, stock_code, stock_name, weight)（RBSA 预加载用）。"""
    with conn or db_conn() as conn:
        rows = conn.execute('SELECT code, stock_code, stock_name, weight FROM fund_holdings WHERE report_date IN (SELECT MAX(report_date) FROM fund_holdings GROUP BY code)').fetchall()
    return list(rows)

def get_market_regime(conn=None) -> str:
    """沪深300 close vs EMA60 → BULL/BEAR/NEUTRAL（大盘状态机单一来源）。"""
    with conn or db_conn() as conn:
        row = conn.execute("SELECT close, ema60 FROM index_daily WHERE code='sh000300' AND close IS NOT NULL AND ema60 IS NOT NULL ORDER BY date DESC LIMIT 1").fetchone()
    return domain.regime_from_close_ema60(row[0] if row else None, row[1] if row else None)

def get_market_technical() -> dict | None:
    """沪深300最新技术面快照（结构化数据），供 LLM regime 判定注入 prompt；数据不足返回 None。

    返回 {"date", "close", "chg_pct", "ema60", "closes"}；文案拼装由消费方（LLM 装配）负责。
    """
    with db_conn() as conn:
        rows = conn.execute("SELECT date, close, ema60 FROM index_daily WHERE code='sh000300' AND close IS NOT NULL ORDER BY date DESC LIMIT 6").fetchall()
    if not rows or not rows[0][2]:
        return None
    latest_date, close, ema60 = rows[0]
    prev_close = rows[1][1] if len(rows) > 1 and rows[1][1] else close
    chg = (close - prev_close) / prev_close * 100 if prev_close else 0.0
    return {
        "date": latest_date, "close": close, "chg_pct": chg,
        "ema60": ema60, "closes": [r[1] for r in reversed(rows)],
    }

def get_meta(key: str) -> str | None:
    """读取 meta 配置值（行业映射更新时间等）。"""
    with db_conn() as conn:
        return meta_get(conn, key)


def save_meta(key: str, value: str) -> None:
    """写入 meta 配置值（与 get_meta 对称，供进化引擎记录时间戳等）。"""
    with db_conn() as conn:
        meta_set(conn, key, value)


def get_interval_days(key: str) -> int | None:
    """距上次记录（meta 键值 YYYY-MM-DD）的间隔天数；无记录/解析失败返回 None。

    架构深化 I：时间戳解析收敛为窄读（消费方不再各自 strptime/ValueError 兜底），
    冷却/限频口径统一可比。
    """
    raw = get_meta(key)
    if not raw:
        return None
    try:
        last = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None
    return (datetime.now().date() - last).days


def get_int_cursor(key: str) -> int:
    """整数游标（meta 键值整数）；无记录/解析失败返回 0（架构深化 I）。"""
    raw = get_meta(key)
    if not raw:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def get_data_latest_date() -> str | None:
    """核心数据表的最新日期（净值/特征/指数/宏观/监控），无数据返回 None。

    各表 date 均为 YYYY-MM-DD；取跨表最大值即「数据更新到哪天」。
    """
    tables = ("fund_nav", "fund_features", "index_daily", "macro_news", "monitor_events")
    sql = " UNION ALL ".join(f"SELECT MAX(date) AS d FROM {t}" for t in tables)
    with db_conn() as conn:
        row = conn.execute(f"SELECT MAX(d) FROM ({sql})").fetchone()
    return row[0] if row and row[0] else None


def get_model_last_trained() -> str | None:
    """读取最近一次模型训练日期（meta 表），无则返回 None。"""
    with db_conn() as conn:
        return meta_get(conn, META.MODEL_LAST_TRAINED)

def get_sector_momentum_median(sector: str, date: str) -> float | None:
    """赛道 20 日动量中位数（成员 <3 返回 None）：推荐 mom_gap 与监控赛道优势共用单一来源。

    架构深化 J：原 get_momentum_in_sector 浅 helper（仅被本函数使用）内联，
    口径不变（仅 momentum_20d 非空过滤——与三周期 medians 的列集要求不同，保持独立）。
    """
    with db_conn() as conn:
        rows = conn.execute(
            'SELECT momentum_20d FROM fund_features WHERE rbsa_industry_1 = ? AND date = ? '
            'AND momentum_20d IS NOT NULL', (sector, date)).fetchall()
    moms = [r[0] for r in rows]
    if len(moms) < 3:
        return None
    values = sorted(moms)
    n = len(values)
    return values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2


def get_sector_momentum_medians(sector: str, date: str) -> dict | None:
    """赛道 5/20/60 日动量中位数（量化定池信号源；成员 <3 返回 None）。"""
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT mom_5d, momentum_20d, mom_60d FROM fund_features "
            "WHERE rbsa_industry_1 = ? AND date = ? "
            "AND mom_5d IS NOT NULL AND momentum_20d IS NOT NULL AND mom_60d IS NOT NULL",
            (sector, date)).fetchall()
    if len(rows) < 3:
        return None
    med = lambda vals: (lambda v: v[len(v) // 2] if len(v) % 2 else (v[len(v) // 2 - 1] + v[len(v) // 2]) / 2)(sorted(vals))
    return {
        "mom_5d": med([r[0] for r in rows]),
        "mom_20d": med([r[1] for r in rows]),
        "mom_60d": med([r[2] for r in rows]),
        "n": len(rows),
    }

def get_rbsa_at_date(code: str, date: str) -> tuple | None:
    """指定日期快照的 (rbsa_industry_1, rbsa_weight_1)；无记录返回 None。"""
    with db_conn() as conn:
        row = conn.execute(
            'SELECT rbsa_industry_1, rbsa_weight_1 FROM fund_features WHERE code = ? AND date = ?',
            (code, date)).fetchone()
    return row if row else None


def get_first_rbsa_after(code: str, date: str) -> tuple | None:
    """date 之后（含）第一个非空 RBSA 快照 (rbsa_industry_1, rbsa_weight_1)。

    用于买入日处于持仓报告期空窗（当天快照 rbsa 为空）时的基准兑底。
    """
    with db_conn() as conn:
        row = conn.execute(
            'SELECT rbsa_industry_1, rbsa_weight_1 FROM fund_features '
            'WHERE code = ? AND date >= ? AND rbsa_industry_1 IS NOT NULL AND rbsa_industry_1 != "" '
            'ORDER BY date LIMIT 1', (code, date)).fetchone()
    return row if row else None

def get_sector_candidates(sectors: list[str]) -> list[dict]:
    """赛道内候选基金：fund_features 三行业匹配 + 全部特征列（推荐排序用）。"""
    if not sectors:
        return []
    placeholders = ','.join('?' * len(sectors))
    feat_cols = ', '.join(('ff.' + c for c in FEATURE_COLS))
    with db_conn() as conn:
        rows = conn.execute(f'SELECT ff.code, fb.name, ff.regime, ff.rbsa_industry_1, ff.rbsa_weight_1, ff.rbsa_industry_2, ff.rbsa_weight_2, ff.rbsa_industry_3, ff.rbsa_weight_3, {feat_cols} FROM fund_features ff JOIN fund_basic fb ON fb.code = ff.code WHERE fb.is_buyable = 1 AND (ff.rbsa_industry_1 IN ({placeholders})   OR ff.rbsa_industry_2 IN ({placeholders})   OR ff.rbsa_industry_3 IN ({placeholders}))', sectors + sectors + sectors).fetchall()
    names = ['code', 'name', 'regime', 'rbsa_industry_1', 'rbsa_weight_1', 'rbsa_industry_2', 'rbsa_weight_2', 'rbsa_industry_3', 'rbsa_weight_3'] + FEATURE_COLS
    return [dict(zip(names, r)) for r in rows]

def get_sector_heatmap(limit: int=6) -> list[dict]:
    """行业热力图：平均 RBSA 权重与平均动量的 Top 行业（结构化行）。"""
    with db_conn() as conn:
        rows = conn.execute("SELECT rbsa_industry_1, AVG(rbsa_weight_1), AVG(momentum_20d) FROM fund_features WHERE rbsa_industry_1 IS NOT NULL AND rbsa_industry_1 != '' GROUP BY rbsa_industry_1 ORDER BY AVG(rbsa_weight_1) DESC LIMIT ?", (limit,)).fetchall()
    return [{"name": r[0], "weight": r[1], "momentum": r[2]} for r in rows]

def get_train_fund_codes(min_bars: int, limit: int) -> list[str]:
    """随机采样满足最小净值条数的基金代码（训练集构建）。"""
    with db_conn() as conn:
        rows = conn.execute('SELECT code FROM fund_nav GROUP BY code HAVING COUNT(*) >= ? ORDER BY RANDOM() LIMIT ?', (min_bars, limit)).fetchall()
    return [r[0] for r in rows]


def sample_fund_codes_before(date: str, min_bars: int, limit: int) -> list[str]:
    """按截止日期动态随机采样基金（回测按决策日采样，防幸存者偏差）。

    只用 date 当日及之前有 min_bars 条净值的基金（早期回测点无历史数据自动排除）。
    经 repo 统一读 seam——回测不再直连数据库手写 SQL。
    """
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT code FROM fund_nav WHERE date <= ? GROUP BY code "
            "HAVING COUNT(*) >= ? ORDER BY RANDOM() LIMIT ?",
            (date, min_bars, limit)).fetchall()
    return [r[0] for r in rows]


def check_data_ready() -> dict[str, int]:
    """推荐前置数据就绪状态（持仓/行业映射计数）。

    门控（check_holdings_ready）的单一数据来源：引擎/管线不再内嵌裸 SQL 计数，
    查询细节在此下沉到 seam（架构深化候选 2）。
    """
    with db_conn() as conn:
        holdings_cnt = conn.execute("SELECT COUNT(*) FROM fund_holdings").fetchone()[0]
        industry_cnt = conn.execute("SELECT COUNT(*) FROM stock_industry_map").fetchone()[0]
    return {"holdings_cnt": holdings_cnt, "industry_cnt": industry_cnt}


def is_recommend_data_ready() -> bool:
    """推荐数据就绪谓词：异常统一兜底返回 False。

    架构深化：就绪判定语义（持仓>0 且行业映射>0）单一来源；
    DB 瞬时异常不向门控调用方抛穿（避免中断后续槽位），由消费方选择日志细节。
    """
    try:
        status = check_data_ready()
    except Exception:
        return False
    return status["holdings_cnt"] > 0 and status["industry_cnt"] > 0


def get_latest_rbsa_sector_map() -> dict[str, str]:
    """code → 最新 RBSA 第一行业 映射（回测赛道模式用，当前时点快照）。"""
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT code, rbsa_industry_1 FROM fund_features "
            "WHERE date = (SELECT MAX(date) FROM fund_features) AND rbsa_industry_1 != ''").fetchall()
    return dict(rows)


def refresh_nav(code: str) -> int:
    """推荐前净值同步（决策域 → 底层数据的显式窄操作）。

    推荐引擎在落库前需补拉选定基金最新净值；此操作收敛在 repo 数据 seam，
    决策域不再直接 import 底层数据实现（app.data.nav），依赖方向与 CONTEXT
    「底层数据由数据基座负责」一致。返回新增净值条数。
    """
    from app.data.nav import fetch_fund_nav_incremental
    return fetch_fund_nav_incremental(code)

def get_system_logs(lines: int = 200, after: int = 0, before: int = 0) -> tuple[list[tuple], int, int]:
    """系统日志读取（Web /api/logs 用，避免绕过 repo seam 内联 SQL）。

    - after > 0：增量拉新（id > after，轮转/清理后仍可靠）；
    - before > 0：向前翻页拉更早（id < before 的最近 lines 条，按 id 升序返回）；
    - 默认：最新 lines 条（按 id 升序返回，UI 从上到下按时间正序展示）。
    返回 (rows, total, last_id)；last_id 恒为返回行中最大 id。
    """
    from app.utils.log import SYSTEM_LOG_TABLE_SQL
    with db_conn() as conn:
        conn.execute(SYSTEM_LOG_TABLE_SQL)
        total = conn.execute("SELECT COUNT(*) FROM system_logs").fetchone()[0]
        if before > 0:
            rows = conn.execute(
                "SELECT id, ts, level, logger, event, message, correlation_id "
                "FROM system_logs WHERE id < ? ORDER BY id DESC LIMIT ?",
                (before, lines),
            ).fetchall()
            rows.reverse()
        elif after <= 0:
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
    with db_conn() as conn:
        start = meta_get(conn, META.UPTIME_START)
    if start:
        return (datetime.now() - datetime.strptime(start, '%Y-%m-%d')).days
    return 365


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
        # 最新监控信号（排序口径 date DESC, id DESC，与 get_latest_monitor_event 一致）
        for code, sig in conn.execute(
            f"SELECT code, signal FROM (SELECT code, signal, "
            f"ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC, id DESC) rk "
            f"FROM monitor_events WHERE code IN ({code_ph})) WHERE rk = 1", codes).fetchall():
            out[code]["signal"] = sig
    return out


def save_fund_features(features: dict, conn=None) -> None:
    """写入一条基金特征快照（INSERT OR REPLACE）。

    ``conn`` 为内部批量 seam：特征全量计算（calc_all_features）复用连接避免逐条重开；
    缺省时自开连接。普通调用不需要也不应传 conn。
    """
    sql = 'INSERT OR REPLACE INTO fund_features (code, date, regime, hurst_60d, momentum_20d, calmar, downside_vol, capture_up, capture_down, bias_60d, drawdown_60d, reversal_20d, mom_5d, mom_60d, vol_20d, rbsa_industry_1, rbsa_weight_1, rbsa_industry_2, rbsa_weight_2, rbsa_industry_3, rbsa_weight_3) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)'
    params = (features['code'], features['date'], features['regime'], features.get('hurst_60d'), features.get('momentum_20d'), features.get('calmar'), features.get('downside_vol'), features.get('capture_up'), features.get('capture_down'), features.get('bias_60d'), features.get('drawdown_60d'), features.get('reversal_20d'), features.get('mom_5d'), features.get('mom_60d'), features.get('vol_20d'), features.get('rbsa_industry_1', ''), features.get('rbsa_weight_1', 0.0), features.get('rbsa_industry_2', ''), features.get('rbsa_weight_2', 0.0), features.get('rbsa_industry_3', ''), features.get('rbsa_weight_3', 0.0))
    if conn is not None:
        conn.execute(sql, params)
    else:
        with db_conn() as conn:
            conn.execute(sql, params)

def set_model_last_trained(date_str: str) -> None:
    """记录最近一次模型训练日期。"""
    with db_conn() as conn:
        meta_set(conn, META.MODEL_LAST_TRAINED, date_str)

def trim_fund_features(retention: int, conn=None) -> None:
    """修剪每只基金特征快照至最近 retention 行（防历史快照无限累积）。

    ``conn`` 为内部批量 seam：特征全量计算路径复用连接；缺省时自开连接。
    """
    sql = 'DELETE FROM fund_features WHERE rowid IN (  SELECT rowid FROM (    SELECT rowid, ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) rk    FROM fund_features) WHERE rk > ?)'
    if conn is not None:
        conn.execute(sql, (retention,))
    else:
        with db_conn() as conn:
            conn.execute(sql, (retention,))


__all__ = ["FEATURE_COLS", "MARKET_COLS", "FORWARD_WINDOW", "check_data_ready", "is_recommend_data_ready", "get_all_ranking_rows", "get_available_sectors", "get_buyable_codes", "get_buyable_feature_stats", "get_candidate_nav_summaries", "get_codes_missing_rbsa", "get_feature_codes_before", "get_feature_dates_map", "get_fund_name", "get_fund_pool_stats", "get_holdings", "get_holdings_at_report", "get_index_close", "get_index_momentum", "get_index_rows", "get_index_series", "get_industry_map", "get_latest_feature_date", "get_latest_feature_date_before", "get_latest_features", "get_latest_holdings_date", "get_latest_holdings_rows", "get_market_regime", "get_market_technical", "get_meta", "get_model_last_trained", "get_data_latest_date", "get_interval_days", "get_int_cursor", "get_sector_momentum_median", "get_sector_momentum_medians", "get_rbsa_at_date", "get_first_rbsa_after", "get_sector_candidates", "get_sector_heatmap", "get_system_logs", "get_train_fund_codes", "get_uptime_days", "refresh_nav", "sample_fund_codes_before", "save_fund_features", "save_meta", "set_model_last_trained", "trim_fund_features"]

"""净值时间序列 module：基金净值的读取统一入口（深 module）。

领域概念「净值时间序列」集中于此：单基金区间/限量读取、最新净值、
指定日期净值、截至日最近净值、全基金最新日期、多基金批量、全量（回测）。
调用方不再拼装 SQL；旧 base.py 的散落读取函数收敛于此。
"""

from app import domain
from app.database import db_conn


def series(code: str, since: str | None = None, until: str | None = None,
           limit: int | None = None) -> list[tuple[str, float]]:
    """单只基金净值行 (date, cum_nav) 升序；since/until 过滤日期区间，limit 取最近 N 条。"""
    sql = 'SELECT date, cum_nav FROM fund_nav WHERE code = ?'
    params: list = [code]
    if since:
        sql += ' AND date >= ?'
        params.append(since)
    if until:
        sql += ' AND date <= ?'
        params.append(until)
    sql += ' ORDER BY date ASC'
    if limit is not None:
        # 最近 N 条：降序取前 N 再反转回升序
        sql = sql.replace('ORDER BY date ASC', 'ORDER BY date DESC LIMIT ?')
        params.append(limit)
        with db_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [(r[0], r[1]) for r in reversed(rows)]
    with db_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [(r[0], r[1]) for r in rows]


def latest(code: str) -> float | None:
    """最新累计净值。"""
    with db_conn() as conn:
        row = conn.execute('SELECT cum_nav FROM fund_nav WHERE code = ? ORDER BY date DESC LIMIT 1', (code,)).fetchone()
    return row[0] if row else None


def at(code: str, date: str) -> float | None:
    """指定日期的累计净值（追踪列表首次净值回退用）。"""
    with db_conn() as conn:
        row = conn.execute('SELECT cum_nav FROM fund_nav WHERE code = ? AND date = ?', (code, date)).fetchone()
    return row[0] if row else None


def at_or_before(code: str, date: str) -> float | None:
    """截至指定日期最近一条净值（已平仓基金持有期截断用）。"""
    with db_conn() as conn:
        row = conn.execute('SELECT cum_nav FROM fund_nav WHERE code = ? AND date <= ? ORDER BY date DESC LIMIT 1', (code, date)).fetchone()
    return row[0] if row else None


def latest_dates() -> dict[str, str]:
    """code → 最新净值日期 映射（批量计算跳过判断用）。"""
    with db_conn() as conn:
        rows = conn.execute('SELECT code, MAX(date) FROM fund_nav GROUP BY code').fetchall()
    return dict(rows)


def batch_latest(codes: list[str]) -> list[tuple]:
    """多只基金净值行 (code, date, cum_nav)，按日期升序（组合估值用）。"""
    if not codes:
        return []
    placeholders = ','.join('?' * len(codes))
    with db_conn() as conn:
        rows = conn.execute(f'SELECT code, date, cum_nav FROM fund_nav WHERE code IN ({placeholders}) ORDER BY date ASC', tuple(codes)).fetchall()
    return rows


def all_rows() -> list[tuple]:
    """全量净值行 (code, date, cum_nav)，按 code/date 升序（回测用）。"""
    with db_conn() as conn:
        rows = conn.execute('SELECT code, date, cum_nav FROM fund_nav ORDER BY code, date ASC').fetchall()
    return list(rows)


def forward_return_from_navs(navs: list, pos: int, forward: int) -> float | None:
    """净值序列 → 满 forward 个净值日的区间收益（纯计算，单一来源）。

    窗口越界或端点非法（缺失/非正）→ None。生产（forward_return）与引擎研究
    （walk_forward._fwd_ret / backtest_model.y_abs）共用，消除 4 份重复实现
    与末端截断语义分叉（架构审查候选 3）。navs: 升序净值值序列；pos: 决策日索引。
    """
    end = pos + forward
    if end >= len(navs) or pos < 0:
        return None
    start_nav, end_nav = navs[pos], navs[end]
    if not (start_nav and end_nav and start_nav > 0):
        return None
    return float(end_nav) / float(start_nav) - 1.0


def forward_return(code: str, since: str) -> float | None:
    """入场日起满 FORWARD_DAYS 个交易日（含入场日 41 条净值）的绝对收益。

    架构深化 C：结算/反事实/质量度量三处消费同一判定（单一来源）——
    窗口不足或净值异常（缺失/非正）统一返回 None，不再各自拷贝实现。
    纯计算逻辑收敛于 forward_return_from_navs（架构审查候选 3）。
    """
    navs = series(code, since=since, limit=domain.FORWARD_DAYS + 1)
    if len(navs) < domain.FORWARD_DAYS + 1:
        return None
    return forward_return_from_navs([v for _, v in navs], 0, domain.FORWARD_DAYS)

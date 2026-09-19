"""配置/游标/系统日志/数据就绪 底层数据 seam（从 base 拆分：meta/system 域）。"""

from datetime import datetime

from app.database import db_conn, meta_get, meta_set
from app.repo import meta_keys as META


def get_meta(key: str) -> str | None:
    """读取 meta 配置值（行业映射更新时间等）。"""
    with db_conn() as conn:
        return meta_get(conn, key)


def save_meta(key: str, value: str) -> None:
    """写入 meta 配置值（与 get_meta 对称，供进化引擎记录时间戳等）。"""
    with db_conn() as conn:
        meta_set(conn, key, value)


def get_settings_all() -> dict[str, str]:
    """settings: 前缀键批量读（config 运行时持久化，ADR-0005 meta 单一读写 seam）。"""
    with db_conn() as conn:
        return dict(conn.execute(
            "SELECT key, value FROM meta WHERE key LIKE 'settings:%'"
        ).fetchall())


def save_settings_all(items: dict[str, str]) -> None:
    """settings: 前缀键整段替换（先删后插，幂等）；消费方不再持有裸 meta SQL。"""
    with db_conn() as conn:
        conn.execute("DELETE FROM meta WHERE key LIKE 'settings:%'")
        conn.executemany(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            list(items.items()),
        )
        conn.commit()


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


def get_uptime_days() -> int:
    with db_conn() as conn:
        start = meta_get(conn, META.UPTIME_START)
    if start:
        return (datetime.now() - datetime.strptime(start, '%Y-%m-%d')).days
    return 365


def get_data_latest_date() -> str | None:
    """核心数据表的最新日期（净值/特征/指数/宏观），无数据返回 None。

    各表 date 均为 YYYY-MM-DD；取跨表最大值即「数据更新到哪天」。
    """
    tables = ("fund_nav", "fund_features", "index_daily", "macro_news")
    sql = " UNION ALL ".join(f"SELECT MAX(date) AS d FROM {t}" for t in tables)
    with db_conn() as conn:
        row = conn.execute(f"SELECT MAX(d) FROM ({sql})").fetchone()
    return row[0] if row and row[0] else None


def get_sector_heatmap(limit: int=6) -> list[dict]:
    """行业热力图：平均 RBSA 权重与平均动量的 Top 行业（结构化行）。"""
    with db_conn() as conn:
        rows = conn.execute("SELECT rbsa_industry_1, AVG(rbsa_weight_1), AVG(momentum_20d) FROM fund_features WHERE rbsa_industry_1 IS NOT NULL AND rbsa_industry_1 != '' GROUP BY rbsa_industry_1 ORDER BY AVG(rbsa_weight_1) DESC LIMIT ?", (limit,)).fetchall()
    return [{"name": r[0], "weight": r[1], "momentum": r[2]} for r in rows]


def check_data_ready() -> dict[str, int]:
    """推荐前置数据就绪状态（持仓/行业映射/特征计数）。

    门控（check_holdings_ready）的单一数据来源：引擎/管线不再内嵌裸 SQL 计数，
    查询细节在此下沉到 seam（架构深化候选 2）。feature_cnt 为最近特征日有数据的
    基金数——审计 P1-2：特征全缺时不能冒充"空推荐日"。
    """
    with db_conn() as conn:
        holdings_cnt = conn.execute("SELECT COUNT(*) FROM fund_holdings").fetchone()[0]
        industry_cnt = conn.execute("SELECT COUNT(*) FROM stock_industry_map").fetchone()[0]
        feature_cnt = conn.execute(
            "SELECT COUNT(*) FROM fund_features "
            "WHERE date = (SELECT MAX(date) FROM fund_features)").fetchone()[0]
    return {"holdings_cnt": holdings_cnt, "industry_cnt": industry_cnt,
            "feature_cnt": feature_cnt}


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

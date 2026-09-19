"""板块日涨跌幅 seam：sector_daily_snapshot 读写（风格反推/特征共用）。
"""

from app.database import db_conn


def save_sector_history_batch(rows: list[tuple[str, str, str, float]]) -> int:
    """批量写入板块历史日线（成分股合成回填，ticket 02）。

    rows: [(date, sector_code, sector_name, pct_chg)]；net_flow 历史无可用源留空，
    不覆盖已有实时快照的 net_flow（ON CONFLICT 只更新 pct_chg/sector_name）。
    返回写入行数。
    """
    if not rows:
        return 0
    with db_conn() as conn:
        conn.executemany(
            'INSERT INTO sector_daily_snapshot (date, sector_code, sector_name, pct_chg, net_flow) '
            'VALUES (?, ?, ?, ?, NULL) '
            'ON CONFLICT(date, sector_code) DO UPDATE SET '
            'sector_name = excluded.sector_name, pct_chg = excluded.pct_chg',
            rows)
    return len(rows)


def get_sector_pct_series(start: str, end: str) -> list[tuple[str, str, str, float]]:
    """区间内板块日涨跌幅 [(date, sector_code, sector_name, pct_chg)]，供风格反推构造矩阵。"""
    with db_conn() as conn:
        rows = conn.execute(
            'SELECT date, sector_code, sector_name, pct_chg FROM sector_daily_snapshot '
            'WHERE date >= ? AND date <= ? AND pct_chg IS NOT NULL ORDER BY date ASC',
            (start, end)).fetchall()
    return [(r[0], r[1], r[2], float(r[3])) for r in rows]


def get_sector_pct_map(start: str, end: str) -> dict[str, dict[str, float]]:
    """区间内板块日涨跌幅按板块名分组 {sector_name: {date: pct}}（风格反推入参形态）。

    收敛引擎三处手写循环（style_track / monitor / walk_forward）的单一归属；
    消费方配合 features/sector 深模块做 ÷100 与全日期覆盖过滤。
    """
    by_sector: dict[str, dict[str, float]] = {}
    for d, _code, name, pct in get_sector_pct_series(start, end):
        by_sector.setdefault(name, {})[d] = pct
    return by_sector


__all__ = ["save_sector_history_batch", "get_sector_pct_series", "get_sector_pct_map"]

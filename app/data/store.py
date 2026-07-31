"""数据写入层：基金列表、净值、指数、持仓。"""

from app.database import db_conn
from app.utils.log import get_logger

logger = get_logger("data_store")

NAV_RETENTION_DAYS = 250
"""净值保留窗口（交易日）：每只基金仅保留最近 N 条。

覆盖特征计算(60天)、web 展示(250天)、模型训练(80天起步)的全部需求，
避免全量历史无限累积导致数据库膨胀。旧数据在每次写入时自动修剪。
"""


def save_fund_list(funds: list[dict]) -> int:
    with db_conn() as conn:
        conn.execute("DELETE FROM fund_basic")
        count = 0
        for f in funds:
            conn.execute(
                "INSERT OR REPLACE INTO fund_basic (code, name, type, is_buyable) VALUES (?, ?, ?, ?)",
                (f["code"], f["name"], f["type"], f["is_buyable"]),
            )
            count += 1
        return count


def save_nav_batch(conn, code: str, navs: list[dict]) -> int:
    if not navs:
        return 0
    cur = conn.execute("SELECT MAX(date) FROM fund_nav WHERE code = ?", (code,))
    row = cur.fetchone()
    local_max = row[0] if row and row[0] else None
    if local_max:
        navs = [n for n in navs if n["date"] > local_max]
    if not navs:
        return 0
    conn.executemany(
        "INSERT OR IGNORE INTO fund_nav (code, date, cum_nav) VALUES (?, ?, ?)",
        [(code, n["date"], n["cum_nav"]) for n in navs],
    )
    # 修剪：仅保留最近 NAV_RETENTION_DAYS 条，防止历史净值无限累积（特征60/展示250/训练80均满足）
    conn.execute(
        "DELETE FROM fund_nav WHERE code = ? AND date < ("
        "  SELECT date FROM fund_nav WHERE code = ? "
        "  ORDER BY date DESC LIMIT 1 OFFSET ?)",
        (code, code, NAV_RETENTION_DAYS - 1),
    )
    return len(navs)


_EMA_PERIOD = 60
_EMA_K = 2 / (_EMA_PERIOD + 1)


def save_index_daily(index_code: str, data: list[dict]) -> int:
    with db_conn() as conn:
        row = conn.execute(
            "SELECT date, ma60 FROM index_daily WHERE code = ? ORDER BY date DESC LIMIT 1",
            (index_code,),
        ).fetchone()
        local_max_date = row[0] if row else None
        prev_ema = row[1] if row and row[1] is not None else None

        data_sorted = sorted(data, key=lambda d: d["date"])
        if local_max_date:
            new_data = [d for d in data_sorted if d["date"] > local_max_date]
        else:
            new_data = data_sorted

        if not new_data:
            return 0

        seed_closes = [
            r[0] for r in conn.execute(
                "SELECT close FROM index_daily WHERE code = ? ORDER BY date ASC",
                (index_code,),
            ).fetchall()
        ]

        written = 0
        for d in new_data:
            close = d["close"]
            if prev_ema is not None:
                ema = close * _EMA_K + prev_ema * (1 - _EMA_K)
            else:
                seed_closes.append(close)
                if len(seed_closes) >= _EMA_PERIOD:
                    ema = sum(seed_closes[-_EMA_PERIOD:]) / _EMA_PERIOD
                else:
                    ema = None

            conn.execute(
                "INSERT OR REPLACE INTO index_daily (code, date, open, high, low, close, volume, ma60) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (index_code, d["date"], d["open"], d["high"], d["low"], d["close"], d["volume"], ema),
            )
            if ema is not None:
                prev_ema = ema
            written += 1

        return written


def backfill_guard(failed, total, label, threshold=0.5):
    if not failed:
        return False
    fail_rate = len(failed) / total
    if fail_rate > threshold:
        logger.warning("%s 补查失败率 %.0f%% (%d/%d), 跳过", label, fail_rate * 100, len(failed), total)
        return False
    logger.info("开始补查 %d 只%s", len(failed), label)
    return True

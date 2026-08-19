"""数据写入层：基金列表、净值、指数、持仓。"""

import time
from datetime import datetime, timedelta, timezone

from app.database import db_conn
from app.utils.log import get_logger

logger = get_logger("data_store")
NAV_RETENTION_DAYS = 1500
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


def save_index_daily(index_code: str, data: list[dict], full_refresh: bool = False) -> int:
    """写入指数日线（ema60 用 60 日 EMA 递推，增量写入）。

    full_refresh=True 时删除该 code 旧数据后按全量重算（用于补拉历史）；
    否则增量：插入本地最新日期之后的新数据，并补齐接口窗口内本地缺失的历史
    缺口（指数表行数小，用日期集合判断，成本可忽略）。补齐缺口后自动全量
    重算 ema60，保持 EMA 递推连续（缺口插入会破坏增量递推的连续性）。
    """
    with db_conn() as conn:
        local_dates = {
            r[0] for r in conn.execute(
                "SELECT date FROM index_daily WHERE code = ?", (index_code,)
            ).fetchall()
        }
        if full_refresh:
            conn.execute("DELETE FROM index_daily WHERE code = ?", (index_code,))
            conn.commit()
            row = None
            prev_ema = None
            local_max_date = None
        else:
            row = conn.execute(
                "SELECT date, ema60 FROM index_daily WHERE code = ? ORDER BY date DESC LIMIT 1",
                (index_code,),
            ).fetchone()
            prev_ema = row[1] if row and row[1] is not None else None
            local_max_date = row[0] if row else None

        data_sorted = sorted(data, key=lambda d: d["date"])
        if local_max_date and not full_refresh:
            # 增量：本地最新之后的新数据 + 本地缺失的缺口日期（停摆恢复时补回）
            new_data = [
                d for d in data_sorted
                if d["date"] > local_max_date or d["date"] not in local_dates
            ]
        else:
            new_data = data_sorted

        if not new_data:
            return 0
        backfilled = local_max_date is not None and any(
            d["date"] < local_max_date for d in new_data
        )

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
                "INSERT OR REPLACE INTO index_daily (code, date, open, high, low, close, volume, ema60) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (index_code, d["date"], d["open"], d["high"], d["low"], d["close"], d["volume"], ema),
            )
            if ema is not None:
                prev_ema = ema
            written += 1

        if backfilled:
            # 缺口插入破坏了 EMA 递推连续性：按完整序列重算
            _recompute_ema60(conn, index_code)

        return written


def _recompute_ema60(conn, index_code: str) -> None:
    """按完整日期序列重算该指数 ema60（EMA60），修复缺口插入导致的递推错位。"""
    rows = conn.execute(
        "SELECT date, close FROM index_daily WHERE code = ? ORDER BY date ASC",
        (index_code,),
    ).fetchall()
    closes: list[float] = []
    ema: float | None = None
    for date, close in rows:
        if ema is not None:
            ema = close * _EMA_K + ema * (1 - _EMA_K)
        else:
            closes.append(close)
            if len(closes) >= _EMA_PERIOD:
                ema = sum(closes[-_EMA_PERIOD:]) / _EMA_PERIOD
        if ema is not None:
            conn.execute(
                "UPDATE index_daily SET ema60 = ? WHERE code = ? AND date = ?",
                (ema, index_code, date),
            )


def backfill_guard(failed: list, total: int, label: str, threshold: float = 0.5) -> bool:
    if not failed:
        return False
    fail_rate = len(failed) / total
    if fail_rate > threshold:
        logger.warning("%s 补查失败率 %.0f%% (%d/%d), 跳过", label, fail_rate * 100, len(failed), total)
        return False
    logger.info("开始补查 %d 只%s", len(failed), label)
    return True


# ── 数据拉取失败记录 ──


def record_failure(fetch_type: str, target: str, error: str = "",
                   stage: str = "", count_attempt: bool = True) -> None:
    """记录一次数据拉取失败（幂等：同一 (fetch_type, target) 累积更新，不重复插入）。

    ``attempts`` 表示"连续失败的运行周期数"（主循环失败时 +1，补查阶段失败不 +1，
    避免把同一次运行内的多轮补查算作多次失败）；恢复后再次失败则重置为 1。
    """
    with db_conn() as conn:
        row = conn.execute(
            "SELECT status, attempts FROM data_fetch_failures WHERE fetch_type = ? AND target = ?",
            (fetch_type, target),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO data_fetch_failures "
                "(fetch_type, target, stage, error, attempts, status, last_failed_at) "
                "VALUES (?, ?, ?, ?, 1, 'failed', datetime('now'))",
                (fetch_type, target, stage, error),
            )
        elif row[0] == "failed":
            attempts = row[1] + 1 if count_attempt else row[1]
            conn.execute(
                "UPDATE data_fetch_failures SET error = ?, stage = ?, attempts = ?, "
                "last_failed_at = datetime('now') WHERE fetch_type = ? AND target = ?",
                (error, stage, attempts, fetch_type, target),
            )
        else:  # 曾恢复后再次失败：重置为 failed，attempts 从 1 重新计数
            conn.execute(
                "UPDATE data_fetch_failures SET status = 'failed', error = ?, stage = ?, "
                "attempts = 1, first_failed_at = datetime('now'), "
                "last_failed_at = datetime('now'), recovered_at = NULL "
                "WHERE fetch_type = ? AND target = ?",
                (error, stage, fetch_type, target),
            )


def mark_recovered(fetch_type: str, target: str, note: str = "") -> None:
    """标记失败已恢复（补查成功或确认无需重试的基金/股票）。"""
    with db_conn() as conn:
        conn.execute(
            "UPDATE data_fetch_failures SET status = 'recovered', "
            "recovered_at = datetime('now'), error = COALESCE(?, error) "
            "WHERE fetch_type = ? AND target = ?",
            (note or None, fetch_type, target),
        )


def mark_recovered_batch(fetch_type: str, targets: list[str]) -> None:
    """批量标记失败已恢复（主循环成功拉取后调用，避免冷却逻辑误判本次已成功的基金）。

    对失败表中仍为 failed 的目标更新为 recovered；无记录或已 recovered 的目标为无害空操作。
    """
    if not targets:
        return
    with db_conn() as conn:
        conn.executemany(
            "UPDATE data_fetch_failures SET status = 'recovered', "
            "recovered_at = datetime('now') "
            "WHERE fetch_type = ? AND target = ? AND status = 'failed'",
            [(fetch_type, t) for t in targets],
        )


def cooldown_targets(fetch_type: str, min_attempts: int = 3,
                     cooldown_days: int = 1,
                     stage_cooldown_days: dict[str, int] | None = None) -> set[str]:
    """返回需冷却跳过的目标集合：连续失败次数达到阈值，且最近失败仍在冷却期内。

    冷却期默认 1 天：连续失败 3 个下载周期后，暂停重试一天，避免对注定失败的
    基金反复请求，又不至于长期搁置。冷却期判断以 SQLite 存储的 UTC 时间为准
    （datetime('now')），与 Python 侧 UTC 对齐。

    stage_cooldown_days 可按失败类型（stage）覆盖冷却期：确认无新数据的
    （stage="no_update"）基金是接口端就无数据，用更长冷却减少反复请求；
    临时拉取失败（stage="primary"）保持短冷却，尽快重试恢复。
    """
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT target, stage, attempts, last_failed_at FROM data_fetch_failures "
            "WHERE fetch_type = ? AND status = 'failed'",
            (fetch_type,),
        ).fetchall()
    if not rows:
        return set()
    stage_days = stage_cooldown_days or {}
    now_utc = datetime.now(timezone.utc)
    out: set[str] = set()
    for target, stage, attempts, last_failed_at in rows:
        if attempts < min_attempts or not last_failed_at:
            continue
        try:
            last = datetime.strptime(last_failed_at[:19], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc)
        except ValueError:
            continue
        days = stage_days.get(stage or "", cooldown_days)
        if last >= now_utc - timedelta(days=days):
            out.add(target)
    return out


def list_failures(fetch_type: str | None = None, status: str | None = None,
                  limit: int = 100) -> list[dict]:
    """查询失败记录（调试/观察用），按最近失败时间倒序。"""
    sql = (
        "SELECT fetch_type, target, stage, error, attempts, status, "
        "first_failed_at, last_failed_at, recovered_at FROM data_fetch_failures WHERE 1 = 1"
    )
    params: list = []
    if fetch_type:
        sql += " AND fetch_type = ?"
        params.append(fetch_type)
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY last_failed_at DESC LIMIT ?"
    params.append(limit)
    cols = ("fetch_type", "target", "stage", "error", "attempts", "status",
            "first_failed_at", "last_failed_at", "recovered_at")
    with db_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def run_backfill_rounds(fetch_type: str, failed: list, backfill_one, total: int,
                        label: str = "", rounds: int = 2, delay: float = 30.0) -> list:
    """对失败项执行多轮补查，并持久化失败/恢复记录。

    - ``backfill_one(item)``：回调执行一次补查；正常返回视为处理完成（无论是否拿到数据，
      由 mark_recovered 收尾），抛异常视为仍需重试（记录失败并保留）。
    - 每轮前用 backfill_guard 做失败率保护（超阈值中止，避免加重数据源压力）。
    - 同步函数；在异步全量下载中于补查阶段调用，该阶段无并发任务，阻塞可接受。
    - 返回最终仍失败的目标列表（保持 failed 状态记录）。
    """
    remaining = list(failed)
    for rnd in range(1, rounds + 1):
        if not remaining:
            break
        if not backfill_guard(remaining, total, f"{label}第{rnd}轮"):
            break
        logger.info("%s补查第 %d 轮: %d 只", label, rnd, len(remaining))
        still_failed = []
        for item in remaining:
            try:
                backfill_one(item)
                mark_recovered(fetch_type, item)
            except Exception as e:
                record_failure(fetch_type, item, str(e)[:200], stage=f"backfill{rnd}",
                               count_attempt=False)
                still_failed.append(item)
        remaining = still_failed
        if rnd < rounds and remaining:
            logger.info("%s仍有 %d 只失败，%.0f 秒后进入下一轮", label, len(remaining), delay)
            time.sleep(delay)
    if remaining:
        logger.warning("%s补查 %d 轮后仍有 %d 只失败（已记入 data_fetch_failures）",
                       label, rounds, len(remaining))
    return remaining

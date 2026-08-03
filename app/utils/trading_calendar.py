"""A 股交易日历：基于 akshare 全年交易日，缓存于 meta 表，一年更新一次。

akshare 的 tool_trade_date_hist_sina 返回自 1990 年至当年年底的完整交易日
（含当年全年节假日/调休安排），本模块裁剪为最近两年后缓存；缓存覆盖当天时
直接查，跨年/无缓存时自动刷新一次（一年一次）。拉取失败视为非交易日——
宁可当天不启动，也不基于不完整日历误判（避免把节假日当交易日启动）。
"""

import json
import time
from datetime import date

from app.database import db_conn, meta_get, meta_set
from app.utils.log import get_logger

logger = get_logger("trading_calendar")

_META_KEY = "trade_dates_cache"
_REFRESH_COOLDOWN_SECONDS = 1800  # 刷新失败后 30 分钟内不重复重试

_cache: set[str] | None = None
_last_refresh_at = 0.0


def _fetch_trade_dates() -> list[str]:
    """从 akshare 拉取交易日并裁剪为最近两年（含未来全年安排），返回升序列表。"""
    import akshare as ak  # 延迟导入：仅刷新时加载，避免拖慢启动

    df = ak.tool_trade_date_hist_sina()
    days = [str(d) for d in df["trade_date"].tolist()]
    if not days:
        return []
    max_year = int(max(days)[:4])
    start = f"{max_year - 1}-01-01"
    return [d for d in days if d >= start]


def _load_from_meta() -> set[str] | None:
    with db_conn() as conn:
        raw = meta_get(conn, _META_KEY)
    if not raw:
        return None
    try:
        days = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return set(days) if isinstance(days, list) else None


def _save_to_meta(days: list[str]) -> None:
    with db_conn() as conn:
        meta_set(conn, _META_KEY, json.dumps(days))


def _refresh_cache(day: date) -> bool:
    """刷新交易日缓存（拉取 akshare 日历并落库），返回 day 是否为交易日。

    拉取失败返回 False（视为非交易日），30 分钟内不重复重试。
    """
    global _cache, _last_refresh_at
    now = time.monotonic()
    if now - _last_refresh_at < _REFRESH_COOLDOWN_SECONDS:
        return False
    _last_refresh_at = now
    try:
        days = _fetch_trade_dates()
    except Exception as e:
        logger.error("交易日历拉取失败，本次视为非交易日: %s", str(e)[:120])
        return False
    if not days:
        logger.error("交易日历返回为空，本次视为非交易日")
        return False
    _cache = set(days)
    _save_to_meta(days)
    logger.info("交易日历已刷新: %d 个交易日（%s ~ %s）",
                len(days), min(days), max(days))
    return day.isoformat() in _cache


def is_trading_day(day: date | None = None) -> bool:
    """判断 day（默认今天）是否为 A 股交易日。

    以 akshare 新浪全年日历为准（自动涵盖节假日与调休）；缓存覆盖当天时直接查，
    跨年/无缓存时自动刷新一次（一年一次）；拉取失败视为非交易日（不启动）。
    """
    global _cache
    day = day or date.today()

    if _cache is None:
        _cache = _load_from_meta()
    if _cache and max(_cache) >= day.isoformat():
        return day.isoformat() in _cache

    return _refresh_cache(day)

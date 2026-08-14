"""A 股交易日历：基于新浪全年交易日（内置 akshare 解码逻辑），缓存于 meta 表，一年更新一次。

新浪 klc_td_sh.txt 返回自 1990 年至当年年底的完整交易日
（含当年全年节假日/调休安排），本模块裁剪为最近两年后缓存；缓存覆盖当天时
直接查，跨年/无缓存时自动刷新一次（一年一次）。拉取失败视为非交易日——
宁可当天不启动，也不基于不完整日历误判（避免把节假日当交易日启动）。

解码：新浪接口返回混淆压缩串，复用 akshare 的 hk_js_decode（内置在
sina_calendar_decode.py），用嵌入式 JS 引擎 py_mini_racer 执行，避免引入
整个 akshare 依赖链（scipy/py_mini_racer/lxml 等）。
"""

from app.repo import meta_keys as META
import json
import time
from datetime import date

from app.database import db_conn, meta_get, meta_set
from app.utils.log import get_logger

logger = get_logger("trading_calendar")

_SINA_CALENDAR_URL = "https://finance.sina.com.cn/realstock/company/klc_td_sh.txt"
_META_KEY = META.TRADE_DATES_CACHE
_REFRESH_COOLDOWN_SECONDS = 1800  # 刷新失败后 30 分钟内不重复重试

_cache: set[str] | None = None
_last_refresh_at = 0.0


def _fetch_sina_calendar_text() -> str:
    """请求新浪交易日历原始文本（var datelist="..." 混淆压缩串）。

    走项目统一的 fetch 封装（自动重试 + 限流退避），与数据基座其它拉取一致。
    """
    from app.data.fetchers import fetch

    return fetch(_SINA_CALENDAR_URL, timeout=15).text


def _decode_sina_calendar(text: str) -> list[str]:
    """解码新浪混淆日历 → 升序交易日列表（YYYY-MM-DD）。"""
    import py_mini_racer
    from app.utils.sina_calendar_decode import DECODE_JS

    payload = text.split("=")[1].split(";")[0].replace('"', "")
    js_code = py_mini_racer.MiniRacer()
    js_code.eval(DECODE_JS)
    return sorted(str(d)[:10] for d in js_code.call("d", payload))


def _fetch_trade_dates() -> list[str]:
    """拉取交易日并裁剪为最近两年（含未来全年安排），返回升序列表。"""
    days = _decode_sina_calendar(_fetch_sina_calendar_text())
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


def trading_day_lag(earlier: str, later: str, days: set[str] | None = None) -> int:
    """计算 earlier 到 later 之间隔的交易日数（不含 earlier、含 later）。

    单一来源：净值停更打标（mark_stale_funds）与特征新鲜度（_feature_freshness）
    共用此计数，消除各自手写滞后判定导致的漂移。
    days 缺省用交易日缓存（与 is_trading_day 同源）；调用方也可传入自己的日期集合
    （如净值实际日期集），保持各自口径不受影响。无缓存/异常/earlier >= later 返回 0。
    """
    if days is None:
        days = _load_from_meta()
    if not days or earlier >= later:
        return 0
    return sum(1 for d in days if earlier < d <= later)

"""A 股交易日历：基于新浪全年交易日（内置 akshare 解码逻辑），缓存于 meta 表，一年更新一次。

新浪 klc_td_sh.txt 返回自 1990 年至当年年底的完整交易日
（含当年全年节假日/调休安排），本模块裁剪为最近两年后缓存；缓存覆盖当天时
直接查，跨年/无缓存时自动刷新一次（一年一次）。拉取失败视为非交易日——
宁可当天不启动，也不基于不完整日历误判（避免把节假日当交易日启动）。

解码：新浪接口返回混淆压缩串，复用 akshare 的 hk_js_decode（内置在
sina_calendar_decode.py），用嵌入式 JS 引擎 py_mini_racer 执行，避免引入
整个 akshare 依赖链（scipy/py_mini_racer/lxml 等）。
"""

import json
import time
from datetime import date, timedelta

from app.repo import meta_keys as META
from app.repo.base import get_meta, save_meta
from app.utils.log import get_logger

logger = get_logger("trading_calendar")

_SINA_CALENDAR_URL = "https://finance.sina.com.cn/realstock/company/klc_td_sh.txt"
_META_KEY = META.TRADE_DATES_CACHE
_META_KEY_HISTORY = META.TRADE_DATES_HISTORY
_REFRESH_COOLDOWN_SECONDS = 1800  # 刷新失败后 30 分钟内不重复重试

_DISCLOSURE_WORKDAYS = 15
"""季报法定披露期限（工作日）：拿不到公告日时的保守滞后基数（共识 Q15）。"""
_DISCLOSURE_FALLBACK_DAYS = 31
"""离线退化上界（自然日）：15 个工作日即使跨春节/国庆长假也不超过 31 自然日。"""

_cache: set[str] | None = None
_history: set[str] | None = None
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
    raw = get_meta(_META_KEY)
    if not raw:
        return None
    try:
        days = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return set(days) if isinstance(days, list) else None


def _save_to_meta(days: list[str]) -> None:
    save_meta(_META_KEY, json.dumps(days))


def trade_dates() -> set[str] | None:
    """交易日集合（进程内缓存 + meta 单次解析；无缓存/解析失败 → None）。

    架构审查候选 5 的单一来源入口：foundation 指数新鲜度 / recommend 特征
    新鲜度 / 单基金闸门共用，不再各自 get_meta + json.loads。与 is_trading_day
    共享模块级 _cache。
    """
    global _cache
    if _cache is None:
        _cache = _load_from_meta()
    return _cache


def expected_trade_date(today: str | None = None) -> str | None:
    """期望交易日：今天在日历内 → 昨交易日（盘前任务拉 T-1 净值），否则最近交易日。

    单一来源（架构审查候选 5）：replace foundation._check_index_freshness 与
    recommend._expected_feature_date 的两份同构分支；无日历缓存 → None。
    """
    days = trade_dates()
    if not days:
        return None
    today = today or date.today().isoformat()
    if today in days:
        return max((d for d in days if d < today), default=None)
    return max((d for d in days if d <= today), default=None)


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


def _history_days() -> set[str]:
    """全历史交易日（1990 起）。缓存优先，缺失时联网一次并落库。

    与 trade_dates() 的近两年窗口刻意分开：公告日推算要覆盖历史报告期，
    而近两年窗口的消费者（新鲜度/停机判定）不应因历史区间变大而变慢。
    """
    global _history
    if _history is not None:
        return _history
    raw = get_meta(_META_KEY_HISTORY)
    if raw:
        try:
            _history = set(json.loads(raw))
            return _history
        except (json.JSONDecodeError, TypeError):
            pass
    try:
        days = _decode_sina_calendar(_fetch_sina_calendar_text())
    except Exception as e:
        logger.error("全历史交易日历拉取失败，公告日退化为自然日上界: %s", str(e)[:120])
        return set()
    if days:
        _history = set(days)
        save_meta(_META_KEY_HISTORY, json.dumps(days))
    return _history or set()


def add_trading_days(start: str, n: int, days: set[str] | None = None) -> str | None:
    """start 之后第 n 个交易日（严格晚于 start）。

    days 显式传入时不读缓存——供迁移等**不能开新数据库连接**的路径使用
    （迁移在 DB 初始化内部，任何 db_conn 都会递归）。
    日历未覆盖（区间不足 n 天）→ None。
    """
    if n < 1:
        return None
    if days is None:
        days = _history_days()
    later = sorted(d for d in days if d > start)
    if len(later) < n:
        return None
    return later[n - 1]


def disclosure_date(report_date: str, days: set[str] | None = None) -> str:
    """公告日的保守估计：报告期 + 15 个工作日（共识 Q15）。

    东财 jjcc 页面实测不含公告日期（只有报告期标签），故一律走保守滞后。
    口径是「宁晚不早」：日历拿不到时退化为 +31 自然日上界——该上界只会
    把可见时间往后推，不会把尚未公告的持仓算成可见（即不会泄漏）。
    """
    exact = add_trading_days(report_date, _DISCLOSURE_WORKDAYS, days=days)
    if exact:
        return exact
    return (date.fromisoformat(report_date)
            + timedelta(days=_DISCLOSURE_FALLBACK_DAYS)).isoformat()


def cached_history_days() -> set[str]:
    """已落库的全历史交易日（不联网、只读 meta）。无缓存/异常 → 空集。"""
    from app.repo.base import get_meta

    raw = get_meta(_META_KEY_HISTORY)
    if not raw:
        return set()
    try:
        return set(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        return set()

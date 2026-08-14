"""统一 HTTP 请求层：push2 域名自动 TLS 指纹伪装，三级降级。

同步/异步统一使用 httpx（openai 既有传递依赖，替换 requests + aiohttp）；
push2 接口因反爬需要浏览器 TLS 指纹，仍走 curl_cffi → tls_client → curl.exe 链。
"""

import asyncio
import logging
import re
import threading
import time
from urllib.parse import urlencode, urlparse

import httpx

logger = logging.getLogger(__name__)

_PUSH2_RE = re.compile(r"(?:^|\.)push2(?:his)?\.eastmoney\.com$", re.IGNORECASE)


def _is_push2(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return bool(_PUSH2_RE.search(host))


_RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504, 514}
"""可重试 HTTP 状态码。514 是东财限流（Frequency Capped），首次下载高频请求时常见。"""
_MAX_RETRIES = 2
_BASE_DELAY = 1.0


def _retry_delay(attempt: int, rate_limited: bool = False) -> float:
    # 限流（429/514）未触发全局暂停时用更长指数退避（5s/10s），普通网络错误用 1s/2s；
    # 已触发全局暂停时由 fetch/fetch_async 统一跟随暂停节奏，不再叠加个人退避
    if rate_limited:
        return _BASE_DELAY * 5 * (2 ** attempt)
    return _BASE_DELAY * (2 ** attempt)


def _retry_decision(e: Exception, status: int | None = None) -> tuple[bool, bool]:
    """重试判定单一来源（同步 fetch / 异步 fetch_async 共用）。

    返回 (should_retry, rate_limited)：rate_limited 为 True 时用更长退避（429/514）。
    未知异常不重试（同步路径的原行为；统一后异步路径同样遵守，避免掩盖程序性错误）。
    """
    if status is not None:  # HTTP 状态码路径：按状态码判定
        return status in _RETRYABLE_HTTP_CODES, status in (429, 514)
    if isinstance(e, (httpx.TransportError, asyncio.TimeoutError, ConnectionError)):
        return True, False
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        return code in _RETRYABLE_HTTP_CODES, code in (429, 514)
    return False, False


def _short_url(url: str, limit: int = 90) -> str:
    """压缩 URL 便于日志定位：取 host + 路径末尾（如 pingzhongdata/{code}.js），超长截断。"""
    u = urlparse(url)
    path = u.path or ""
    key = path.rsplit("/", 1)[-1]
    out = u.hostname or ""
    if key:
        out = f"{out}/{key}"
    return out if len(out) <= limit else out[: limit - 1] + "…"


def _host_of(url: str) -> str:
    """提取请求 host（限流计数/退避按 host 维度记忆，跨域名互不牵连）。"""
    return urlparse(url).netloc


class _QPSGate:
    """全局 QPS 令牌桶（东财 514 源头限速，架构深化）。

    东财对单 IP 的实测限速阈值约 5 请求/秒（社区多来源交叉验证）；本项目把全
    东财域名的总请求速率压到 _QPS_RATE（3/秒，留一倍余量）——从源头不触发限流，
    而不是触发后才熔断退避；同时天然削平"全局暂停结束瞬间所有请求同时醒来"的
    恢复洪峰（令牌逐秒发放，醒来者排队）。
    """

    def __init__(self, rate: float):
        self._rate = rate
        self._tokens = rate  # 满桶起步，允许启动时少量突发
        self._last = 0.0
        self._lock = threading.Lock()

    def _refill(self, now: float) -> None:
        if self._last:
            self._tokens = min(self._rate, self._tokens + (now - self._last) * self._rate)
        self._last = now

    def _try_acquire(self, now: float) -> float | None:
        """尝试取令牌；取不到返回需等待秒数，取到返回 None。"""
        with self._lock:
            self._refill(now)
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return None
            return (1.0 - self._tokens) / self._rate

    def acquire_sync(self) -> None:
        """同步路径：阻塞至拿到令牌。"""
        while True:
            wait = self._try_acquire(time.monotonic())
            if wait is None:
                return
            time.sleep(wait)

    async def acquire_async(self) -> None:
        """异步路径：阻塞至拿到令牌（不让出事件循环，等待者并发休眠）。"""
        while True:
            wait = self._try_acquire(time.monotonic())
            if wait is None:
                return
            await asyncio.sleep(wait)


# 全局每秒请求上限：东财单 IP 实测阈值约 5/s（社区多来源），留一倍余量取 3/s。
# 全东财域名共用同一闸门（限流按 IP 维度，暂停也按全局节奏）。
_QPS_RATE = 3.0
_qps_gate = _QPSGate(_QPS_RATE)


class _TransportState:
    """传输层运行状态封装（替代模块级可变全局，消解全局可变状态）。

    含三类状态：TLS 库惰性加载缓存、push2 请求限流计数、全局限流熔断（429/514）。
    fetch / fetch_async 共用同一实例，保证同步/异步路径限流口径一致。
    """

    def __init__(self):
        self._tls_libs_loaded = False
        self._tls_available = False
        self._push2_limit_start = 0.0
        self._push2_limit_count = 0
        # 限流计数与退避按 host 维度记忆（架构深化：东财 514 期间其他域名成功
        # 不再清零退避——全局单值 cooldown 被任意成功复位是退避永不升级的根因）
        self._host_rate: dict[str, tuple[float, int]] = {}  # host → (窗口起点, 窗口内限流次数)
        self._host_cooldown: dict[str, float] = {}          # host → 当前退避时长（秒）
        self._pause_until = 0.0
        self._pause_announced = False
        # 熔断状态跨线程安全：sync（行业映射等）与 async（持仓/净值）请求路径
        # 并发调用计数/复位，无锁时同秒叠加虚增退避（日志实测 30→60→120→1800 跳变）
        self._lock = threading.Lock()  # 暂停提示只打印一次，避免并发等待时刷屏

    # ── TLS 库惰性加载 ──

    def load_tls_libs(self) -> None:
        if self._tls_libs_loaded:
            return
        self._tls_libs_loaded = True
        try:
            import curl_cffi.requests as _cc  # noqa: F401
            self._tls_available = True
            return
        except ImportError:
            pass
        try:
            import tls_client as _tc  # noqa: F401
            self._tls_available = True
            return
        except ImportError:
            pass
        self._tls_available = False

    @property
    def tls_available(self) -> bool:
        return self._tls_available

    # ── push2 请求限流（10 次/分钟）──

    def push2_rate_limited(self) -> bool:
        with self._lock:
            now = time.monotonic()
            if now - self._push2_limit_start > 60:
                self._push2_limit_start = now
                self._push2_limit_count = 0
            self._push2_limit_count += 1
            return self._push2_limit_count > 10

    # ── 全局限流熔断（HTTP 429/514）──

    def _pause_remaining(self) -> float:
        """剩余全局暂停时长（秒），单一判定来源（架构深化 F）。

        等待前不清零 _pause_until——并发下只有首个调用者会真正等待，
        其余直接放行继续发请求，暂停形同虚设（东财持仓 514 持续限流的根因）；
        提示标志只打一次，避免并发等待时刷屏。
        """
        with self._lock:
            remaining = self._pause_until - time.monotonic()
            if remaining <= 0:
                self._pause_announced = False
                return 0.0
            if not self._pause_announced:
                self._pause_announced = True
                logger.warning("东财限流熔断：全局暂停 %.1f 秒后自动继续", remaining)
            return remaining

    async def global_pause_wait(self) -> None:
        """异步路径：位于全局暂停窗口时休眠至结束，结束后自动恢复（无需人工干预）。"""
        remaining = self._pause_remaining()
        if remaining > 0:
            try:
                await asyncio.sleep(remaining)
            finally:
                self._pause_announced = False

    def sync_pause_wait(self) -> None:
        """同步路径等待全局暂停窗口结束（回填/单只拉取共用，与异步口径一致）。"""
        remaining = self._pause_remaining()
        if remaining > 0:
            try:
                time.sleep(remaining)
            finally:
                self._pause_announced = False

    def reset(self) -> None:
        """复位全部运行状态（测试隔离 interface，架构深化 F）。

        修复：原测试直戳私有字段复位且漏掉 TLS/限流计数 4 字段（测试间可能串扰）；
        现收敛为公共方法，一次复位全部字段（含 host 维度退避表）。
        """
        with self._lock:
            self._tls_libs_loaded = False
            self._tls_available = False
            self._push2_limit_start = 0.0
            self._push2_limit_count = 0
            self._host_rate.clear()
            self._host_cooldown.clear()
            self._pause_until = 0.0
            self._pause_announced = False

    def is_paused(self) -> bool:
        """当前是否处于全局暂停窗口内。"""
        with self._lock:
            return self._pause_until > time.monotonic()

    def note_rate_limited(self, host: str) -> None:
        """记录一次限流（按 host 独立计数）；窗口内达到阈值则触发全局暂停（指数退避）。

        host 维度：每个域名的限流窗口/退避互不牵连——东财 514 期间其他域名
        正常请求不会稀释东财的退避级数；暂停仍是全局的（全请求跟随统一节奏）。
        整体持锁：sync/async 多执行流并发调用时计数与翻倍原子，
        避免同秒叠加虚增暂停时长（日志实测 30→60→120→1800 跳变）。
        """
        with self._lock:
            now = time.monotonic()
            if self._pause_until > now:
                # 暂停窗口内：多为暂停前已在途请求的失败，不重复计数，
                # 避免无人请求时暂停时长也被在途失败虚增翻倍
                return
            start, hits = self._host_rate.get(host, (0.0, 0))
            if now - start > _RATE_WIN_SEC:
                start, hits = now, 0
            hits += 1
            if hits >= _RATE_TRIGGER:
                # 指数退避：该 host 的暂停时长逐次翻倍，封顶 _RATE_BACKOFF_MAX
                cooldown = self._host_cooldown.get(host, 0.0)
                cooldown = _RATE_PAUSE_SEC if cooldown <= 0 else min(cooldown * 2, _RATE_BACKOFF_MAX)
                self._host_cooldown[host] = cooldown
                self._pause_until = now + cooldown
                self._host_rate[host] = (now, 0)
                logger.warning("检测到限流(429/514)已触发 %d 次，全局暂停 %.0f 秒（指数退避 30s→60s→120s…，%s 成功后才复位）",
                               _RATE_TRIGGER, cooldown, host)
            else:
                self._host_rate[host] = (start, hits)

    def reset_rate_limit(self, host: str) -> None:
        """该 host 一次请求成功：复位其退避基准（其他 host 不受牵连）。

        修复：原全局单值 _cooldown 被任意请求成功复位——东财 514 持续期间
        其他域名成功即清零退避，暂停时长永远停在 30s 基准（限流永不解除的根因）。
        """
        with self._lock:
            self._host_cooldown.pop(host, None)
            self._host_rate.pop(host, None)


_transport = _TransportState()


# 全局限流熔断（HTTP 429/514）：短窗口内多次触发 → 整个下载协程池全局暂停，
# 暂停时长按 host 指数退避（30s→60s→120s…封顶 30 分钟），每次暂停结束自动试探续跑，
# 直到该 host 请求成功才复位；其他域名成功不清零（原全局单值被任意成功复位，
# 东财 514 期间退避永远停在 30s 基准——限流永不解除的根因）。
# 暂停为全局：暂停窗口内所有域名跟随统一节奏，避免各自退避互相踩雷。
_RATE_WIN_SEC = 20.0      # 限流计数窗口（秒，按 host 独立）
_RATE_TRIGGER = 6         # 窗口内触发多少次限流则进入全局暂停
_RATE_PAUSE_SEC = 30.0    # 全局暂停基准时长（秒），每次触发翻倍
_RATE_BACKOFF_MAX = 1800.0 # 单次暂停时长上限（秒），封顶 30 分钟（东财 514 常持续 10-30 分钟）


def fetch(
    url: str,
    params: dict | None = None,
    timeout: float = 15,
    proxies: dict | None = None,
    headers: dict | None = None,
) -> httpx.Response:
    if _is_push2(url):
        _transport.load_tls_libs()
        if not _transport.tls_available:
            logger.warning("push2 域名需要 tls-client 或 curl_cffi，两者均未安装")
        return _push2_fetch(url, params=params, timeout=timeout, proxies=proxies, headers=headers)

    hdrs = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    }
    if headers:
        hdrs.update(headers)

    # requests 的 proxies 形如 {"http": ..., "https": ...}；httpx 单个 proxy 取 https 优先
    proxy = None
    if proxies:
        proxy = proxies.get("https") or proxies.get("http")

    last_error: Exception | None = None
    # trust_env=False：仅使用显式传入的 proxy，忽略 Windows 系统代理（避免代理软件未启动时全部请求失败）
    with httpx.Client(timeout=timeout, follow_redirects=True, proxy=proxy, trust_env=False) as client:
        for attempt in range(_MAX_RETRIES + 1):
            _transport.sync_pause_wait()
            _qps_gate.acquire_sync()  # 全局 QPS 闸门：源头限速，避免触发东财限流
            try:
                resp = client.get(url, params=params, headers=hdrs)
                resp.raise_for_status()
                if not _transport.is_paused():
                    # 暂停窗口内的在途成功不复位（限流可能未解除），暂停结束后的成功才复位
                    _transport.reset_rate_limit(_host_of(url))
                return resp
            except Exception as e:
                last_error = e
                should_retry, rate_limited = _retry_decision(e)
                if rate_limited:
                    _transport.note_rate_limited(_host_of(url))
                if not should_retry or attempt == _MAX_RETRIES:
                    raise
                if _transport.is_paused():
                    # 已触发全局暂停：不再叠加个人退避，跟随全局节奏（下次循环顶部等待）
                    logger.warning("请求失败(%s, 第%d次重试): %s，已触发全局暂停，跟随统一节奏",
                                   _short_url(url), attempt + 1, str(e)[:120])
                    continue
                delay = _retry_delay(attempt, rate_limited)
                logger.warning("请求失败(%s, 第%d次重试), %.1f秒后重试: %s",
                               _short_url(url), attempt + 1, delay, str(e)[:120])
                time.sleep(delay)

    raise last_error


def _fetch_push2_curl_cffi(url: str, hdrs: dict, timeout: float) -> tuple:
    """push2 降级策略 1：curl_cffi 模拟 chrome120 TLS 指纹（架构深化 F 策略序列）。

    返回 (Response | None, http_status | None)：None 响应表示降级到下一策略，
    status 供调用方在三级全失败时判断 429/514 进全局熔断。
    """
    try:
        import curl_cffi.requests as cc
    except ImportError:
        logger.info("curl_cffi 未安装, 降级到 tls-client")
        return None, None
    try:
        resp = cc.get(url, headers=hdrs, timeout=timeout, impersonate="chrome120")
        if resp.status_code < 500:
            resp.raise_for_status()
            return resp, None
        logger.warning("curl_cffi push2 返回 %d, 降级到 tls-client", resp.status_code)
        return None, resp.status_code
    except Exception as e:
        if isinstance(e, httpx.HTTPStatusError):
            logger.warning("curl_cffi push2 失败: %s, 降级到 tls-client", str(e)[:80])
            return None, e.response.status_code
        logger.warning("curl_cffi push2 失败: %s, 降级到 tls-client", str(e)[:80])
        return None, None


def _fetch_push2_tls_client(url: str, hdrs: dict, timeout: float) -> tuple:
    """push2 降级策略 2：tls_client 模拟 chrome_120（TLS 指纹随机化）。"""
    try:
        import tls_client
    except ImportError:
        logger.info("tls-client 未安装, 降级到 curl.exe -4")
        return None, None
    try:
        sess = tls_client.Session(
            client_identifier="chrome_120",
            random_tls_extension_order=True,
        )
        resp = sess.get(url, headers=hdrs, timeout_seconds=timeout)
        if resp.status_code < 500:
            resp.raise_for_status()
            return resp, None
        logger.warning("tls-client push2 返回 %d, 降级到 curl.exe -4", resp.status_code)
        return None, resp.status_code
    except Exception as e:
        if isinstance(e, httpx.HTTPStatusError):
            logger.warning("tls-client push2 失败: %s, 使用 curl.exe -4", str(e)[:80])
            return None, e.response.status_code
        logger.warning("tls-client push2 失败: %s, 使用 curl.exe -4", str(e)[:80])
        return None, None


def _fetch_push2_curl_exe(url: str, hdrs: dict, timeout: float) -> tuple:
    """push2 降级策略 3：系统 curl.exe -4 子进程（TLS 指纹最弱，最后兜底）。"""
    import subprocess
    try:
        quoted = url.replace('"', '\\"')
        cmd = f'curl.exe -4 -s -m {int(timeout)} -H "User-Agent: {hdrs["User-Agent"]}" "{quoted}"'
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 2)
        if result.returncode == 0:
            return httpx.Response(200, content=result.stdout.encode("utf-8")), None
        logger.warning("curl.exe -4 push2 返回码 %d, 重试", result.returncode)
    except Exception as e:
        logger.warning("curl.exe -4 push2 也失败: %s", str(e)[:80])
    return None, None


# push2 降级链（架构深化 F）：策略序列，首个可用者生效——
# 替代嵌套 try/except 阶梯，顺序可注入测试（新增策略只需追加元组元素）
_PUSH2_STRATEGIES = (_fetch_push2_curl_cffi, _fetch_push2_tls_client, _fetch_push2_curl_exe)


def _push2_fetch(url: str, params: dict | None = None, timeout: float = 15,
                 proxies: dict | None = None, headers: dict | None = None) -> httpx.Response:
    hdrs = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://quote.eastmoney.com/",
    }
    if headers:
        hdrs.update(headers)
    if params:
        url = url + "?" + urlencode(params)

    rate_limited_seen = False
    for attempt in range(_MAX_RETRIES + 1):
        # 统一限流协调（候选 6）：push2 与普通域名共用全局暂停/熔断节奏，
        # 不再游离于全局熔断之外（此前全局暂停期间 push2 照发、限流不计数）
        _transport.sync_pause_wait()
        _qps_gate.acquire_sync()  # 全局 QPS 闸门：push2 同样受单 IP 速率约束
        if _transport.push2_rate_limited():
            logger.warning("push2 请求已达速率上限(10次/分钟)，等待60秒...")
            time.sleep(60)

        status = None
        for strategy in _PUSH2_STRATEGIES:
            resp, st = strategy(url, hdrs, timeout)
            if resp is not None:
                if not _transport.is_paused():
                    _transport.reset_rate_limit(_host_of(url))
                return resp
            if st is not None:
                status = st

        # 三级降级全部失败：若检测到限流（429/514）进全局熔断计数
        if status in (429, 514):
            _transport.note_rate_limited(_host_of(url))
            rate_limited_seen = True
        if attempt < _MAX_RETRIES:
            if _transport.is_paused():
                # 已触发全局暂停：不再叠加个人退避，跟随全局节奏
                logger.warning("push2 三级降级全部失败(%s)，已触发全局暂停，跟随统一节奏",
                               _short_url(url))
                continue
            delay = _retry_delay(attempt, rate_limited_seen)
            logger.warning("push2 三级降级全部失败, 第%d次重试, %.1f秒后重试", attempt + 1, delay)
            time.sleep(delay)

    raise ConnectionError(f"push2 三级降级全部失败: {url[:80]}")


async def fetch_async(
    session: "httpx.AsyncClient",
    url: str,
    params: dict | None = None,
    timeout: float = 15,
    headers: dict | None = None,
) -> "httpx.Response":
    last_error: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        await _transport.global_pause_wait()
        await _qps_gate.acquire_async()  # 全局 QPS 闸门：源头限速，避免触发东财限流
        try:
            resp = await session.get(
                url, params=params, headers=headers, timeout=timeout,
            )
            resp.raise_for_status()
            if not _transport.is_paused():
                # 暂停窗口内的在途成功不复位（限流可能未解除），暂停结束后的成功才复位
                _transport.reset_rate_limit(_host_of(url))
            return resp
        except Exception as e:
            last_error = e
            status = e.response.status_code if isinstance(e, httpx.HTTPStatusError) else None
            should_retry, rate_limited = _retry_decision(e, status=status)
            if rate_limited:
                _transport.note_rate_limited(_host_of(url))
            if not should_retry or attempt == _MAX_RETRIES:
                raise
            if _transport.is_paused():
                # 已触发全局暂停：不再叠加个人退避，跟随全局节奏（下次循环顶部等待）
                logger.warning("异步请求失败(%s, 第%d次重试): %s，已触发全局暂停，跟随统一节奏",
                               _short_url(url), attempt + 1, str(e)[:120])
                continue
            delay = _retry_delay(attempt, rate_limited)
            logger.warning("异步请求失败(%s, 第%d次重试), %.1f秒后重试: %s",
                           _short_url(url), attempt + 1, delay, str(e)[:120], exc_info=True)
            await asyncio.sleep(delay)
    raise last_error or RuntimeError("unreachable")

"""统一 HTTP 请求层：push2 域名自动 TLS 指纹伪装，三级降级。"""

import asyncio
import logging
import re
import subprocess
import time
from urllib.parse import urlencode, urlparse

import requests

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
    # 限流（429/514）用更长的指数退避（5s/10s/20s），普通网络错误用 1s/2s
    if rate_limited:
        return _BASE_DELAY * 5 * (2 ** attempt)
    return _BASE_DELAY * (2 ** attempt)


def _is_retryable(e: Exception) -> bool:
    if isinstance(e, (requests.ConnectionError, requests.Timeout)):
        return True
    if isinstance(e, requests.HTTPError):
        return e.response.status_code in _RETRYABLE_HTTP_CODES
    if isinstance(e, ConnectionError):
        return True
    return False


def _short_url(url: str, limit: int = 90) -> str:
    """压缩 URL 便于日志定位：取 host + 路径末尾（如 pingzhongdata/{code}.js），超长截断。"""
    u = urlparse(url)
    path = u.path or ""
    key = path.rsplit("/", 1)[-1]
    out = u.hostname or ""
    if key:
        out = f"{out}/{key}"
    return out if len(out) <= limit else out[: limit - 1] + "…"


_TLS_LIBS_LOADED = False
_TLS_AVAILABLE = False


def _load_tls_libs():
    global _TLS_LIBS_LOADED, _TLS_AVAILABLE
    if _TLS_LIBS_LOADED:
        return
    _TLS_LIBS_LOADED = True
    try:
        import curl_cffi.requests as _cc  # noqa: F401
        _TLS_AVAILABLE = True
        return
    except ImportError:
        pass
    try:
        import tls_client as _tc  # noqa: F401
        _TLS_AVAILABLE = True
        return
    except ImportError:
        pass
    _TLS_AVAILABLE = False


_push2_limit_start = 0.0
_push2_limit_count = 0


def _push2_rate_limited():
    global _push2_limit_start, _push2_limit_count
    now = time.monotonic()
    if now - _push2_limit_start > 60:
        _push2_limit_start = now
        _push2_limit_count = 0
    _push2_limit_count += 1
    return _push2_limit_count > 10


def fetch(
    url: str,
    params: dict | None = None,
    timeout: float = 15,
    proxies: dict | None = None,
    headers: dict | None = None,
) -> requests.Response:
    import requests as sync_requests

    if _is_push2(url):
        _load_tls_libs()
        if not _TLS_AVAILABLE:
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

    last_error = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = sync_requests.get(
                url, params=params, timeout=timeout, proxies=proxies, headers=hdrs,
            )
            resp.raise_for_status()
            return resp
        except Exception as e:
            last_error = e
            if not _is_retryable(e):
                raise
            if attempt == _MAX_RETRIES:
                raise
            rate_limited = isinstance(e, requests.HTTPError) and e.response.status_code in (429, 514)
            delay = _retry_delay(attempt, rate_limited)
            logger.warning("请求失败(%s, 第%d次重试), %.1f秒后重试: %s",
                           _short_url(url), attempt + 1, delay, str(e)[:120])
            time.sleep(delay)

    raise last_error


def _push2_fetch(url, params=None, timeout=15, proxies=None, headers=None):
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

    if _push2_rate_limited():
        logger.warning("push2 请求已达速率上限(10次/分钟)，等待60秒...")
        time.sleep(60)

    for attempt in range(_MAX_RETRIES + 1):
        try:
            import curl_cffi.requests as cc
            resp = cc.get(url, headers=hdrs, timeout=timeout, impersonate="chrome120")
            if resp.status_code < 500:
                resp.raise_for_status()
                return resp
            logger.warning("curl_cffi push2 返回 %d, 降级到 tls-client", resp.status_code)
        except ImportError:
            logger.info("curl_cffi 未安装, 降级到 tls-client")
        except Exception as e:
            logger.warning("curl_cffi push2 失败: %s, 降级到 tls-client", str(e)[:80])

        try:
            import tls_client
            sess = tls_client.Session(
                client_identifier="chrome_120",
                random_tls_extension_order=True,
            )
            resp = sess.get(url, headers=hdrs, timeout_seconds=timeout)
            if resp.status_code < 500:
                return resp
            logger.warning("tls-client push2 返回 %d, 降级到 curl.exe -4", resp.status_code)
        except ImportError:
            logger.info("tls-client 未安装, 降级到 curl.exe -4")
        except Exception as e:
            logger.warning("tls-client push2 失败: %s, 使用 curl.exe -4", str(e)[:80])

        try:
            import subprocess
            quoted = url.replace('"', '\\"')
            cmd = f'curl.exe -4 -s -m {int(timeout)} -H "User-Agent: {hdrs["User-Agent"]}" "{quoted}"'
            if params:
                cmd += f" -d '{urlencode(params)}'"
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 2)
            if result.returncode == 0:
                resp = requests.Response()
                resp.status_code = 200
                resp._content = result.stdout.encode("utf-8")
                return resp
            logger.warning("curl.exe -4 push2 返回码 %d, 重试", result.returncode)
        except Exception as e:
            logger.warning("curl.exe -4 push2 也失败: %s", str(e)[:80])

        if attempt < _MAX_RETRIES:
            delay = _retry_delay(attempt)
            logger.warning("push2 三级降级全部失败, 第%d次重试, %.1f秒后重试", attempt + 1, delay)
            time.sleep(delay)

    raise ConnectionError(f"push2 三级降级全部失败: {url[:80]}")


async def fetch_async(
    session: "aiohttp.ClientSession",
    url: str,
    params: dict | None = None,
    timeout: float = 15,
    headers: dict | None = None,
) -> "aiohttp.ClientResponse":
    import aiohttp

    last_error: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = await session.get(
                url, params=params, headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            )
            resp.raise_for_status()
            return resp
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as e:
            last_error = e
            if attempt == _MAX_RETRIES:
                raise
            delay = _retry_delay(attempt)
            logger.warning("异步请求失败(%s, 第%d次重试), %.1f秒后重试: %s",
                           _short_url(url), attempt + 1, delay, str(e)[:120], exc_info=True)
            await asyncio.sleep(delay)
        except aiohttp.ClientResponseError as e:
            if e.status in _RETRYABLE_HTTP_CODES:
                last_error = e
                if attempt == _MAX_RETRIES:
                    raise
                rate_limited = e.status in (429, 514)
                delay = _retry_delay(attempt, rate_limited)
                logger.warning("异步请求 %d(%s, 第%d次重试), %.1f秒后重试",
                               e.status, _short_url(url), attempt + 1, delay)
                await asyncio.sleep(delay)
            else:
                raise
        except Exception as e:
            last_error = e
            if attempt == _MAX_RETRIES:
                raise
            delay = _retry_delay(attempt)
            logger.warning("异步请求异常(%s, 第%d次重试): %s",
                           _short_url(url), attempt + 1, str(e)[:120], exc_info=True)
            await asyncio.sleep(delay)
    raise last_error or RuntimeError("unreachable")

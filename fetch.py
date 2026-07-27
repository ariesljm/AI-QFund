"""统一 HTTP 请求层：push2 域名自动 TLS 指纹伪装，三级降级。

设计原则：
    - push2*.eastmoney.com → TLS 指纹伪装（JA3/JA4 绕过）
    - 其他域名 → 普通 requests（已验证可用）
    - 三级降级：tls-client → curl_cffi → subprocess curl.exe -4
    - 内置速率限制：push2 域名最多 10 次/分钟
"""

import asyncio
import logging
import random
import re
import subprocess
import time
from urllib.parse import urlencode
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

# ========== push2 域名判定 ==========

_PUSH2_RE = re.compile(r"(?:^|\.)push2(?:his)?\.eastmoney\.com$", re.IGNORECASE)


def _is_push2(url: str) -> bool:
    """判断 URL 是否属于 push2 域名族。"""
    host = urlparse(url).hostname or ""
    return bool(_PUSH2_RE.search(host))


# ========== push2 多 host 池（请求分布 + 故障切换）==========

# 东方财富存在多个 push2/push2his 解析 IP，单 host 被频控时可切备用
_PUSH2_HOSTS = [
    "push2.eastmoney.com",
    "17.push2.eastmoney.com",
    "29.push2.eastmoney.com",
    "79.push2.eastmoney.com",
    "91.push2.eastmoney.com",
]

_PUSH2HIS_HOSTS = [
    "push2his.eastmoney.com",
    "7.push2his.eastmoney.com",
    "17.push2his.eastmoney.com",
    "33.push2his.eastmoney.com",
    "63.push2his.eastmoney.com",
    "91.push2his.eastmoney.com",
]


def _resolve_push2_urls(url: str) -> list[str]:
    """返回待尝试的 host URL 列表（原始 host 优先，其余打乱顺序）。"""
    parsed = urlparse(url)
    original_host = parsed.hostname or ""

    pool = (
        _PUSH2HIS_HOSTS if "push2his" in original_host
        else _PUSH2_HOSTS
    )
    # 原始 host 优先，其余打乱做 fallback
    others = [h for h in pool if h != original_host]
    random.shuffle(others)
    ordered = [original_host] + others if original_host in pool else [original_host] + pool

    return [
        url.replace(original_host, host, 1)
        for host in ordered
    ]


# ========== 错误分类与重试 ==========

_RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 2
_BASE_DELAY = 1.0


def _is_retryable(e: Exception) -> bool:
    """判断错误是否值得重试（网络/超时/服务端错误可重试，4xx 除 429 外不可）。"""
    if isinstance(e, (requests.ConnectionError, requests.Timeout)):
        return True
    if isinstance(e, requests.HTTPError):
        return e.response.status_code in _RETRYABLE_HTTP_CODES
    if isinstance(e, ConnectionError):
        return True  # push2 三级降级全部失败
    return False


# ========== 速率限制 ==========

class _RateLimiter:
    """滑动窗口速率限制器（push2 专用）。"""

    def __init__(self, max_calls: int = 10, window: float = 60.0):
        self.max_calls = max_calls
        self.window = window
        self._timestamps: list[float] = []

    def wait_if_needed(self) -> None:
        now = time.monotonic()
        self._timestamps = [t for t in self._timestamps if now - t < self.window]
        if len(self._timestamps) >= self.max_calls:
            sleep_until = self._timestamps[0] + self.window
            sleep_time = sleep_until - now
            if sleep_time > 0:
                logger.info("push2 限速：等待 %.1f 秒", sleep_time)
                time.sleep(sleep_time)
        self._timestamps.append(time.monotonic())


_push2_limiter = _RateLimiter(max_calls=10, window=60.0)


# ========== UA 伪装 ==========

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
}


# ========== 降级路径 1: tls-client ==========

def _fetch_tls_client(url: str, params: dict | None = None, timeout: float = 15) -> requests.Response:
    """使用 tls-client（Chrome TLS 指纹）发起请求。"""
    import tls_client

    session = tls_client.Session(
        client_identifier="chrome120",
        random_tls_extension_order=True,
    )
    resp = session.get(url, params=params, headers=_HEADERS, timeout_seconds=int(timeout))
    # 封装为 requests.Response 以保持接口一致
    r = requests.Response()
    r.status_code = resp.status_code
    r._content = resp.content
    r.encoding = resp.encoding or "utf-8"
    r.headers = dict(resp.headers)
    r.url = resp.url or url
    r.raise_for_status()
    return r


# ========== 降级路径 2: curl_cffi ==========

def _fetch_curl_cffi(url: str, params: dict | None = None, timeout: float = 15) -> requests.Response:
    """使用 curl_cffi（Chrome TLS 指纹）发起请求。"""
    from curl_cffi import requests as cffi_requests

    s = cffi_requests.Session(impersonate="chrome120")
    resp = s.get(url, params=params, headers=_HEADERS, timeout=timeout)
    # 封装为 requests.Response
    r = requests.Response()
    r.status_code = resp.status_code
    r._content = resp.content
    r.encoding = resp.encoding or "utf-8"
    r.headers = dict(resp.headers)
    r.url = resp.url or url
    r.raise_for_status()
    return r


# ========== 降级路径 3: subprocess curl.exe -4 ==========

def _fetch_curl_subprocess(url: str, params: dict | None = None, timeout: float = 15) -> requests.Response:
    """使用系统 curl.exe -4（schannel TLS）发起请求，绕过 Python HTTP 栈。"""
    if params:
        separator = "&" if "?" in url else "?"
        full_url = url + separator + urlencode(params)
    else:
        full_url = url

    import sys as _sys
    curl_cmd = "curl.exe" if _sys.platform == "win32" else "curl"
    result = subprocess.run(
        [curl_cmd, "-4", "-s", "-m", str(int(timeout)), "--compressed", full_url],
        capture_output=True,
        text=True,
        timeout=timeout + 5,
    )
    if result.returncode != 0:
        raise RuntimeError(f"{curl_cmd} 失败 (rc={result.returncode}): {result.stderr[:200]}")

    r = requests.Response()
    r.status_code = 200
    r._content = result.stdout.encode("utf-8")
    r.encoding = "utf-8"
    r.url = url
    # curl 返回的是原始 body，需手动 raise_for_status
    return r


# ========== 统一入口 ==========

def _try_tls_paths(url: str, params: dict | None, timeout: float) -> requests.Response:
    """对单 host 依次尝试三级降级（tls-client → curl_cffi → curl.exe）。"""
    errors: list[str] = []

    for name, fn in [
        ("tls-client", _fetch_tls_client),
        ("curl_cffi", _fetch_curl_cffi),
        ("curl.exe", _fetch_curl_subprocess),
    ]:
        try:
            resp = fn(url, params, timeout)
            logger.debug("push2 %s 成功: %s", name, url)
            return resp
        except Exception as e:
            msg = f"{name}: {type(e).__name__}: {str(e)[:80]}"
            errors.append(msg)
            logger.debug("push2 %s 失败: %s", name, msg)

    raise ConnectionError(f"三级降级均失败: {'; '.join(errors)}")


def _fetch_push2(url: str, params: dict | None = None, timeout: float = 15) -> requests.Response:
    """push2 域名专用：速率限制 + 多 host 容错 + TLS 三级降级。

    先试原始 host（三级降级），全失败则依次尝试其他 push2 host（仅 tls-client），
    应对单 host 被频控或临时不可用场景。
    """
    _push2_limiter.wait_if_needed()
    candidates = _resolve_push2_urls(url)

    host_errors: list[str] = []
    for i, host_url in enumerate(candidates):
        try:
            if i == 0:
                # 原始 host：完整三级降级
                return _try_tls_paths(host_url, params, timeout)
            # 备用 host：仅 tls-client（降级路径大概率同因失败，不浪费时间）
            return _fetch_tls_client(host_url, params, timeout)
        except Exception as e:
            host = urlparse(host_url).hostname
            host_errors.append(f"{host}: {e}")
            logger.debug("push2 host %s 失败: %s", host, e)

    raise ConnectionError(
        f"push2 所有 host 均失败: {'; '.join(host_errors)}"
    )


def fetch(url: str, params: dict | None = None, timeout: float = 15) -> requests.Response:
    """发起 GET 请求（带重试）。

    - push2 域名：速率限制 + TLS 指纹伪装三级降级
    - 其他域名：普通 requests（trust_env=False 绕过系统代理）
    - 网络/超时/服务端错误自动指数退避重试
    """
    last_error: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            if _is_push2(url):
                return _fetch_push2(url, params, timeout)
            return _fetch_regular(url, params, timeout)
        except Exception as e:
            last_error = e
            if not _is_retryable(e) or attempt == _MAX_RETRIES:
                raise
            delay = _BASE_DELAY * (2 ** attempt)
            logger.warning("请求失败(第%d次重试), %.1f秒后重试: %s", attempt + 1, delay, str(e)[:120], exc_info=True)
            time.sleep(delay)

    # 不应到达此处，但保持类型安全
    raise RuntimeError("unreachable") from last_error


def _fetch_regular(url: str, params: dict | None = None, timeout: float = 15) -> requests.Response:
    """普通 requests 请求（绕过系统代理）。"""
    s = requests.Session()
    s.trust_env = False
    resp = s.get(url, params=params, headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp


async def fetch_async(
    session: "aiohttp.ClientSession",
    url: str,
    params: dict | None = None,
    timeout: float = 15,
    headers: dict | None = None,
) -> "aiohttp.ClientResponse":
    """异步 GET 请求（带重试），供 data_foundation.py 的异步批量下载使用。

    与 sync fetch() 共享重试策略：网络/超时/5xx 自动指数退避重试。
    """
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
            delay = _BASE_DELAY * (2 ** attempt)
            logger.warning("异步请求失败(第%d次重试), %.1f秒后重试: %s", attempt + 1, delay, str(e)[:120], exc_info=True)
            await asyncio.sleep(delay)
        except aiohttp.ClientResponseError as e:
            if e.status in _RETRYABLE_HTTP_CODES and attempt < _MAX_RETRIES:
                delay = _BASE_DELAY * (2 ** attempt)
                logger.warning("异步请求 HTTP %d(第%d次重试), %.1f秒后重试", e.status, attempt + 1, delay)
                await asyncio.sleep(delay)
                continue
            raise

    raise RuntimeError("unreachable") from last_error

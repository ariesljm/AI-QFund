"""统一 HTTP 请求层：push2 域名自动 TLS 指纹伪装，三级降级。

设计原则：
    - push2*.eastmoney.com → TLS 指纹伪装（JA3/JA4 绕过）
    - 其他域名 → 普通 requests（已验证可用）
    - 三级降级：tls-client → curl_cffi → subprocess curl.exe -4
    - 内置速率限制：push2 域名最多 10 次/分钟
"""

import json
import logging
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

    result = subprocess.run(
        ["curl.exe", "-4", "-s", "-m", str(int(timeout)), "--compressed", full_url],
        capture_output=True,
        text=True,
        timeout=timeout + 5,
    )
    if result.returncode != 0:
        raise RuntimeError(f"curl.exe 失败 (rc={result.returncode}): {result.stderr[:200]}")

    r = requests.Response()
    r.status_code = 200
    r._content = result.stdout.encode("utf-8")
    r.encoding = "utf-8"
    r.url = url
    # curl 返回的是原始 body，需手动 raise_for_status
    return r


# ========== 统一入口 ==========

def fetch(url: str, params: dict | None = None, timeout: float = 15) -> requests.Response:
    """发起 GET 请求。

    - push2 域名：速率限制 + TLS 指纹伪装三级降级
    - 其他域名：普通 requests（trust_env=False 绕过系统代理）
    """
    if not _is_push2(url):
        return _fetch_regular(url, params, timeout)

    # push2 域名：速率限制 + 三级降级
    _push2_limiter.wait_if_needed()

    errors: list[str] = []

    # 路径 1: tls-client
    try:
        resp = _fetch_tls_client(url, params, timeout)
        logger.debug("push2 tls-client 成功: %s", url)
        return resp
    except Exception as e:
        errors.append(f"tls-client: {type(e).__name__}: {str(e)[:80]}")
        logger.debug("tls-client 失败: %s", errors[-1])

    # 路径 2: curl_cffi
    try:
        resp = _fetch_curl_cffi(url, params, timeout)
        logger.debug("push2 curl_cffi 成功: %s", url)
        return resp
    except Exception as e:
        errors.append(f"curl_cffi: {type(e).__name__}: {str(e)[:80]}")
        logger.debug("curl_cffi 失败: %s", errors[-1])

    # 路径 3: subprocess curl.exe -4
    try:
        resp = _fetch_curl_subprocess(url, params, timeout)
        logger.debug("push2 curl.exe 成功: %s", url)
        return resp
    except Exception as e:
        errors.append(f"curl.exe: {type(e).__name__}: {str(e)[:80]}")
        logger.debug("curl.exe 失败: %s", errors[-1])

    # 全部失败
    raise ConnectionError(
        f"push2 所有降级路径均失败: {'; '.join(errors)}"
    )


def _fetch_regular(url: str, params: dict | None = None, timeout: float = 15) -> requests.Response:
    """普通 requests 请求（绕过系统代理）。"""
    s = requests.Session()
    s.trust_env = False
    resp = s.get(url, params=params, headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp

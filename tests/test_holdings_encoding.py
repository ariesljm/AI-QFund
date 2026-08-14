"""持仓抓取编码解码测试：东财接口 charset 由 gbk 改为 utf-8 后的解码正确性。

回归锁：旧代码 getattr(resp, "charset") 恒为 None → 永远 gbk 解码 → utf-8 响应
产生乱码股票名（U+FFFD）。修复后以响应声明的 encoding 解码，gbk 兜底。
"""

import asyncio
import pytest

import app.data.foundation as foundation
from app.data.foundation import _async_fetch_holdings_one

# 东财持仓页 HTML 片段（真实结构：报告期 + 表格行）
_HTML_UTF8 = (
    "<h4>持仓截止至：<font class='px12'>2026-06-30</font></label></h4>"
    "<table><tbody>"
    "<tr><td>1</td><td><a href='/x'>605117</a></td><td class='tol'><a href='/x'>德业股份</a></td>"
    "<td class='tor'>9.91%</td></tr>"
    "<tr><td>2</td><td><a href='/x'>301327</a></td><td class='tol'><a href='/x'>华宝新能</a></td>"
    "<td class='tor'>8.52%</td></tr>"
    "</tbody></table>"
)


class _FakeResponse:
    """模拟 fetch_async 返回值：content 字节 + encoding 声明。"""

    def __init__(self, content: bytes, encoding: str | None):
        self.content = content
        self.encoding = encoding


def _run_fetch(fake_resp):
    """驱动 _async_fetch_holdings_one，mock fetch_async 返回 fake_resp。"""
    async def _inner():
        orig = foundation.fetch_async
        foundation.fetch_async = _async_side_effect(fake_resp)
        try:
            return await _async_fetch_holdings_one(
                None, "007590", "https://x", asyncio.Semaphore(1))
        finally:
            foundation.fetch_async = orig

    def _async_side_effect(resp):
        async def _fake(session, url, params=None, timeout=15, headers=None):
            return resp
        return _fake

    return asyncio.run(_inner())


def test_utf8_response_decoded_correctly():
    """东财新接口（charset=utf-8）：股票名正常，无乱码。"""
    code, report_date, holdings, failed = _run_fetch(
        _FakeResponse(_HTML_UTF8.encode("utf-8"), "utf-8"))
    assert failed is False
    assert report_date == "2026-06-30"
    names = [h["stock_name"] for h in holdings]
    assert names == ["德业股份", "华宝新能"]
    assert all("\ufffd" not in n for n in names)


def test_gbk_response_decoded_correctly():
    """东财旧接口（charset=gbk）：gbk 内容仍正常解析。"""
    code, report_date, holdings, failed = _run_fetch(
        _FakeResponse(_HTML_UTF8.encode("gbk"), "gbk"))
    assert failed is False
    names = [h["stock_name"] for h in holdings]
    assert names == ["德业股份", "华宝新能"]


def test_no_declared_charset_falls_back_to_gbk():
    """响应无 charset 声明：回退 gbk 兜底（历史接口行为）。"""
    code, report_date, holdings, failed = _run_fetch(
        _FakeResponse(_HTML_UTF8.encode("gbk"), None))
    assert failed is False
    names = [h["stock_name"] for h in holdings]
    assert names == ["德业股份", "华宝新能"]

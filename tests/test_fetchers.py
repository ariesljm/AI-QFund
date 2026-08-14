"""fetchers 限流/重试逻辑测试。

回归根因：东财持仓下载（fundf10 FundArchivesDatas.aspx）持续 514 限流，
而净值下载重试后可恢复。根因是全局暂停存在竞态——并发下只有首个调用者
真正等待，其余直接放行继续请求，暂停形同虚设；且触发暂停后仍在途的失败
会重复计数，把暂停时长虚增翻倍。
"""

import asyncio
import time
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import pytest

from app.data import fetchers


def _reset_transport():
    """复位全局传输状态（经公共 interface，架构深化 F）——
    一次复位全部字段（含 TLS 加载/限流计数，原直戳私有字段漏 4 个）。"""
    fetchers._transport.reset()


@pytest.fixture(autouse=True)
def _clean_transport(monkeypatch):
    _reset_transport()
    # 测试替身闸门：高 rate 不阻塞——QPS 行为由 TestQPSGate 单独验证，
    # 避免旧测试的 fake sleep（不推进时钟）在真实令牌桶里死循环
    monkeypatch.setattr(fetchers, "_qps_gate", fetchers._QPSGate(rate=1e9))
    yield
    _reset_transport()


def test_reset_clears_all_state(monkeypatch):
    """公共 reset() 一次复位全部字段（含 TLS/限流计数，原直戳私有字段漏 4 个）。"""
    t = fetchers._transport
    t._tls_libs_loaded = True
    t._tls_available = True
    t._push2_limit_start = time.monotonic()
    t._push2_limit_count = 5
    t._host_rate["a.eastmoney.com"] = (time.monotonic(), 3)
    t._host_cooldown["a.eastmoney.com"] = 60.0
    t._pause_until = time.monotonic() + 5.0
    t._pause_announced = True

    t.reset()

    assert t._tls_libs_loaded is False
    assert t._tls_available is False
    assert t._push2_limit_start == 0.0
    assert t._push2_limit_count == 0
    assert t._host_rate == {}
    assert t._host_cooldown == {}
    assert t._pause_until == 0.0
    assert t._pause_announced is False


def test_global_pause_wait_all_concurrent_callers_wait(monkeypatch):
    """全局暂停必须让所有并发调用者都等待，而非只有首个（竞态：暂停形同虚设）。"""
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay, result=None):
        sleeps.append(delay)
        await real_sleep(0)  # 让出事件循环，模拟并发时序

    monkeypatch.setattr(fetchers.asyncio, "sleep", fake_sleep)

    t = fetchers._transport
    t._pause_until = time.monotonic() + 5.0

    async def main():
        await asyncio.gather(*[t.global_pause_wait() for _ in range(10)])

    asyncio.run(main())

    # 修复前：首个清零 _pause_until，其余 9 个直接放行 → 仅 1 次 sleep
    assert len(sleeps) == 10
    assert all(abs(s - 5.0) < 0.5 for s in sleeps)


def test_note_rate_limited_ignored_during_pause():
    """暂停窗口内的失败（多为暂停前在途请求）不应重复计数、不应虚增暂停时长。"""
    t = fetchers._transport
    host = "fundf10.eastmoney.com"
    t._host_rate[host] = (time.monotonic(), 0)
    for _ in range(fetchers._RATE_TRIGGER):
        t.note_rate_limited(host)
    assert t.is_paused()
    cooldown_before = t._host_cooldown[host]
    assert cooldown_before == fetchers._RATE_PAUSE_SEC

    # 暂停期内再失败：计数不增长、暂停时长不翻倍
    for _ in range(fetchers._RATE_TRIGGER * 2):
        t.note_rate_limited(host)
    assert t._host_cooldown[host] == cooldown_before
    assert t._host_rate[host] == (t._host_rate[host][0], 0)


def test_fetch_async_follows_global_pause_on_ratelimit(monkeypatch):
    """限流触发全局暂停后，重试不再叠加个人退避（5s/10s），统一跟随全局节奏。"""
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay, result=None):
        sleeps.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(fetchers.asyncio, "sleep", fake_sleep)

    t = fetchers._transport
    host = "fundf10.eastmoney.com"
    t._host_rate[host] = (time.monotonic(), fetchers._RATE_TRIGGER - 1)  # 预热：再失败 1 次即触发全局暂停

    url = "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
    req = httpx.Request("GET", url)

    class _FakeSession:
        async def get(self, *a, **kw):
            return httpx.Response(514, request=req)

    async def main():
        with pytest.raises(httpx.HTTPStatusError):
            await fetchers.fetch_async(_FakeSession(), url)

    asyncio.run(main())

    assert t._host_cooldown.get(host, 0) >= fetchers._RATE_PAUSE_SEC  # 限流已计数并触发暂停
    assert sleeps, "触发暂停后应等待全局节奏"
    # 修复前会出现 5s/10s 个人退避 + 仅一次暂停等待；修复后全部为暂停等待
    assert all(s >= fetchers._RATE_PAUSE_SEC - 0.5 for s in sleeps)


def test_fetch_sync_counts_ratelimit_and_follows_pause(monkeypatch):
    """同步路径（回填/单只拉取）也应计数限流并跟随全局暂停。"""
    t = fetchers._transport
    host = "fundf10.eastmoney.com"
    t._host_rate[host] = (time.monotonic(), fetchers._RATE_TRIGGER - 1)

    url = "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
    req = httpx.Request("GET", url)

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **kw):
            resp = httpx.Response(514, request=req)
            resp.raise_for_status()  # 模拟 fetch 内 raise_for_status 抛错路径
            return resp

    sleeps: list[float] = []
    monkeypatch.setattr(fetchers.time, "sleep", lambda d: sleeps.append(d))
    monkeypatch.setattr(fetchers.httpx, "Client", _FakeClient)

    with pytest.raises(httpx.HTTPStatusError):
        fetchers.fetch(url)

    assert t._host_cooldown.get(host, 0) >= fetchers._RATE_PAUSE_SEC  # 同步路径也触发了限流熔断
    assert sleeps, "触发暂停后应等待全局节奏"
    assert all(s >= fetchers._RATE_PAUSE_SEC - 0.5 for s in sleeps)


def test_push2_ratelimit_counts_into_global_fuse(monkeypatch):
    """候选 6：push2 路径 514 也进全局熔断计数（此前游离于熔断之外）。"""
    import sys as _sys
    import types as _types
    t = fetchers._transport
    t._host_rate["push2.eastmoney.com"] = (time.monotonic(), fetchers._RATE_TRIGGER - 1)  # 预热：再 1 次 514 即触发全局暂停

    monkeypatch.setattr(t, "push2_rate_limited", lambda: False)
    monkeypatch.setattr(fetchers.time, "sleep", lambda d: None)  # 不真实等待暂停/退避
    url = "https://push2.eastmoney.com/api/qt/ulist.np/get"
    req = httpx.Request("GET", url)

    # 三级降级：curl_cffi/tls-client 均返回 514，curl.exe 抛异常 → 全失败
    cc_pkg = _types.ModuleType("curl_cffi")
    cc_mod = _types.ModuleType("curl_cffi.requests")
    cc_mod.get = lambda *a, **k: httpx.Response(514, request=req)
    monkeypatch.setitem(_sys.modules, "curl_cffi", cc_pkg)
    monkeypatch.setitem(_sys.modules, "curl_cffi.requests", cc_mod)
    import subprocess as _subprocess
    monkeypatch.setattr(_subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("curl失败")))

    with pytest.raises(ConnectionError):
        fetchers._push2_fetch(url)

    assert t.is_paused()  # 514 已计数并触发全局暂停


class TestHostBackoff:
    """架构深化：指数退避按 host 维度记忆。

    回归根因：原全局单值 cooldown 被任意请求成功复位——东财 514 持续期间
    push2 等其他域名成功即清零退避，暂停时长永远停在 30s 基准，限流永不解除。
    """

    def test_doubles_per_host_and_other_host_immune(self):
        """A 连续限流 30→60→120；期间 B 成功不复位 A 的退避。"""
        t = fetchers._transport
        a = "fundf10.eastmoney.com"
        b = "push2.eastmoney.com"

        # 第一轮：A 攒满 6 次触发 30s 暂停
        for _ in range(fetchers._RATE_TRIGGER):
            t.note_rate_limited(a)
        assert t._host_cooldown[a] == fetchers._RATE_PAUSE_SEC
        assert t.is_paused()

        # 暂停窗口内 B 复位：A 的退避不受牵连
        t.reset_rate_limit(b)
        assert t._host_cooldown.get(a) == fetchers._RATE_PAUSE_SEC

        # 第二轮（暂停结束、窗口重开）：A 再攒满 → 60s
        t._pause_until = 0.0
        t._host_rate[a] = (time.monotonic(), 0)
        for _ in range(fetchers._RATE_TRIGGER):
            t.note_rate_limited(a)
        assert t._host_cooldown[a] == fetchers._RATE_PAUSE_SEC * 2

        # 第三轮 → 120s
        t._pause_until = 0.0
        t._host_rate[a] = (time.monotonic(), 0)
        for _ in range(fetchers._RATE_TRIGGER):
            t.note_rate_limited(a)
        assert t._host_cooldown[a] == fetchers._RATE_PAUSE_SEC * 4

    def test_capped_at_max(self):
        """退避封顶 _RATE_BACKOFF_MAX（30 分钟），到顶后保持不再翻倍。"""
        t = fetchers._transport
        host = "fundf10.eastmoney.com"
        t._host_cooldown[host] = fetchers._RATE_BACKOFF_MAX
        t._pause_until = 0.0
        t._host_rate[host] = (time.monotonic(), 0)
        for _ in range(fetchers._RATE_TRIGGER):
            t.note_rate_limited(host)
        assert t._host_cooldown[host] == fetchers._RATE_BACKOFF_MAX

    def test_reset_rate_limit_only_clears_own_host(self):
        """复位只清触发 host 自己的退避，其他 host 保留。"""
        t = fetchers._transport
        a = "fundf10.eastmoney.com"
        b = "push2.eastmoney.com"
        t._host_cooldown[a] = 120.0
        t._host_cooldown[b] = 60.0
        t.reset_rate_limit(a)
        assert a not in t._host_cooldown
        assert t._host_cooldown[b] == 60.0

    def test_inflight_success_during_pause_does_not_reset(self, monkeypatch):
        """暂停窗口内的在途成功不复位退避；暂停结束后的成功才复位。"""
        t = fetchers._transport
        host = "fundf10.eastmoney.com"
        url = f"https://{host}/FundArchivesDatas.aspx"
        req = httpx.Request("GET", url)

        class _FakeSession:
            async def get(self, *a, **kw):
                return httpx.Response(200, request=req)

        # mock 掉暂停等待：模拟请求在暂停前已发出、此刻才返回在途成功
        async def no_wait():
            pass
        monkeypatch.setattr(t, "global_pause_wait", no_wait)

        t._host_cooldown[host] = 120.0
        t._pause_until = time.monotonic() + 5.0  # 暂停中

        async def run():
            await fetchers.fetch_async(_FakeSession(), url)
        asyncio.run(run())

        # 在途成功（暂停前发出）不代表限流解除：退避保留
        assert t._host_cooldown.get(host) == 120.0

        # 暂停结束后成功 → 复位该 host
        t._pause_until = 0.0
        asyncio.run(run())
        assert host not in t._host_cooldown


class TestQPSGate:
    """架构深化：全局 QPS 令牌桶（东财 514 源头限速）。

    东财单 IP 实测阈值约 5/s（社区多来源），闸门压到 3/s 从源头不触发；
    同时削平暂停结束瞬间的恢复洪峰（令牌逐秒发放，醒来者排队）。
    测试用推进式假时钟：fake sleep 同时推进 monotonic，避免死循环。
    """

    def _clock(self, monkeypatch):
        now = [1000.0]
        sleeps: list[float] = []
        monkeypatch.setattr(fetchers.time, "monotonic", lambda: now[0])

        def fake_sleep(d):
            sleeps.append(d)
            now[0] += d  # 推进时钟
        monkeypatch.setattr(fetchers.time, "sleep", fake_sleep)
        return now, sleeps

    def test_burst_then_throttled(self, monkeypatch):
        """满桶允许少量突发（rate 个立即通过），随后按 1/rate 节奏发放。"""
        now, sleeps = self._clock(monkeypatch)
        gate = fetchers._QPSGate(rate=3.0)
        for _ in range(3):
            gate.acquire_sync()
        assert sleeps == []
        gate.acquire_sync()  # 第 4 个：等待令牌补充（约 1/3 秒）
        assert len(sleeps) == 1
        assert abs(sleeps[0] - 1.0 / 3.0) < 0.05

    def test_refills_over_time(self, monkeypatch):
        """时间推移后令牌恢复：等待后下一个请求立即通过。"""
        now, sleeps = self._clock(monkeypatch)
        gate = fetchers._QPSGate(rate=2.0)
        for _ in range(2):
            gate.acquire_sync()  # 满桶 2 个
        assert sleeps == []
        gate.acquire_sync()  # 第 3 个：需等 0.5s
        assert len(sleeps) == 1 and abs(sleeps[0] - 0.5) < 0.05
        now[0] += 1.0  # 模拟空闲 1 秒（令牌补满）
        gate.acquire_sync()  # 空闲后立即拿到
        assert len(sleeps) == 1

    def test_async_acquire_respects_rate(self, monkeypatch):
        """异步路径同样限速（await asyncio.sleep 等待）。"""
        now = [1000.0]
        sleeps: list[float] = []
        monkeypatch.setattr(fetchers.time, "monotonic", lambda: now[0])
        real_sleep = asyncio.sleep

        async def fake_sleep(delay, result=None):
            sleeps.append(delay)
            now[0] += delay
            await real_sleep(0)

        monkeypatch.setattr(fetchers.asyncio, "sleep", fake_sleep)
        gate = fetchers._QPSGate(rate=1.0)

        async def run():
            for _ in range(3):
                await gate.acquire_async()

        asyncio.run(run())
        assert len(sleeps) == 2  # 满桶 1 个，后两个各等 ~1s
        assert all(abs(s - 1.0) < 0.05 for s in sleeps)

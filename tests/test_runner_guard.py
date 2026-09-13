"""B1 调度并发守卫与交易时段判定测试（app/web/runner.py）。

覆盖：
- run_pipeline_wrapper 并发守卫：双线程同时触发只执行一次（可注入执行计数器）；
- running 状态重复触发被忽略（手工触发与定时触发同时到达的双跑竞态回归）；
- is_trading_time 四边界（09:30/11:30/13:00/15:00 均含）与区间外、非交易日；
- 管线内部异常不挂死守卫，状态回落 error 后再次触发能正常执行到 done。

隔离方式：monkeypatch 管线入口（run_full_pipeline 等）与 mark_slot_run 写库；
交易日历走 trading_calendar 模块级 _cache stub（真实 is_trading_day 判定逻辑，
不触网拉取）。
"""

import sys
import threading
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import app.utils.trading_calendar as trading_calendar
import app.web.runner as runner


@pytest.fixture(autouse=True)
def _reset_pipeline_state():
    """每个测试前重置 runner 模块级管线状态，避免跨测试污染。"""
    runner.pipeline.status = {"state": "idle", "message": ""}
    runner.pipeline.logs.clear()
    runner.pipeline.last_run_date = None
    yield
    runner.pipeline.status = {"state": "idle", "message": ""}
    runner.pipeline.logs.clear()
    runner.pipeline.last_run_date = None


class TestPipelineConcurrencyGuard:
    """run_pipeline_wrapper 并发守卫：运行中忽略重复触发，管线至多执行一次。"""

    def test_running_state_ignores_duplicate_trigger(self, monkeypatch):
        """running 状态下重复触发被忽略：管线不执行、状态保持 running、留下跳过日志。"""
        executed = []
        monkeypatch.setattr(runner, "run_full_pipeline", lambda: executed.append(1))
        monkeypatch.setattr(runner, "mark_slot_run", lambda *a, **k: None)

        runner.pipeline.status = {"state": "running", "message": "管线启动..."}
        runner.run_pipeline_wrapper()

        assert executed == []
        assert runner.pipeline.status["state"] == "running"
        assert any("[跳过]" in line for line in runner.pipeline.logs)

    def test_concurrent_dual_trigger_runs_once(self, monkeypatch):
        """双线程同时触发：首个线程持有 running 状态期间，第二个触发被守卫忽略，管线只执行一次。

        用 entered/release 事件保证线程1确实已越过锁进入执行段后，再启动线程2，
        避免竞态误判；可注入计数器断言管线执行次数。
        """
        entered = threading.Event()
        release = threading.Event()
        executed = []

        def fake_full_pipeline():
            executed.append(1)
            entered.set()
            release.wait(10)

        monkeypatch.setattr(runner, "run_full_pipeline", fake_full_pipeline)
        monkeypatch.setattr(runner, "mark_slot_run", lambda *a, **k: None)

        t1 = threading.Thread(target=runner.run_pipeline_wrapper)
        t1.start()
        assert entered.wait(10), "首个触发未进入管线执行段"
        t2 = threading.Thread(target=runner.run_pipeline_wrapper)
        t2.start()
        t2.join(10)
        assert not t2.is_alive(), "运行中重复触发未被忽略（第二个线程未退出）"
        release.set()
        t1.join(10)
        assert not t1.is_alive(), "首个触发未正常结束"

        assert executed == [1]
        assert runner.pipeline.status["state"] == "done"

    def test_pipeline_failure_does_not_stick_guard(self, monkeypatch):
        """管线内部异常：不外抛、状态回落 error；再次触发（状态非 running）守卫不拦截，
        能正常执行到 done——失败不挂死守卫。"""
        executed = []

        def boom():
            raise RuntimeError("数据基座拉取失败（模拟）")

        monkeypatch.setattr(runner, "run_full_pipeline", boom)
        monkeypatch.setattr(runner, "mark_slot_run", lambda *a, **k: None)

        runner.run_pipeline_wrapper()
        assert runner.pipeline.status["state"] == "error"
        assert "管线执行失败" in runner.pipeline.status["message"]

        monkeypatch.setattr(runner, "run_full_pipeline", lambda: executed.append(1))
        runner.run_pipeline_wrapper()
        assert executed == [1]
        assert runner.pipeline.status["state"] == "done"

    def test_slot_dispatch_routes_to_matching_pipeline(self, monkeypatch):
        """slot 路由：data/recommend/None 分别走 run_data_pipeline / run_recommend_pipeline / run_full_pipeline。"""
        calls = []
        monkeypatch.setattr(runner, "run_data_pipeline", lambda: calls.append("data"))
        monkeypatch.setattr(runner, "run_recommend_pipeline", lambda: calls.append("recommend"))
        monkeypatch.setattr(runner, "run_full_pipeline", lambda: calls.append("full"))
        monkeypatch.setattr(runner, "mark_slot_run", lambda *a, **k: None)

        runner.run_pipeline_wrapper(slot="data")
        assert calls == ["data"]
        runner.run_pipeline_wrapper(slot="recommend")
        assert calls == ["data", "recommend"]
        runner.run_pipeline_wrapper()
        assert calls == ["data", "recommend", "full"]
        assert runner.pipeline.status["state"] == "done"


# 交易日历缓存 stub（真实 is_trading_day 逻辑 + 不触网）：08-21 与 08-24 为交易日，
# 08-22/08-23（周六日）不在缓存内。
_TRADE_CACHE = {"2026-08-21", "2026-08-24"}


@pytest.fixture
def trade_calendar_stub(monkeypatch):
    """把 trading_calendar 模块级 _cache 替换为固定交易日集合（monkeypatch 自动还原）。"""
    monkeypatch.setattr(trading_calendar, "_cache", set(_TRADE_CACHE))


class TestIsTradingTime:
    """A股交易时段判定：交易日内的 09:30-11:30、13:00-15:00 闭区间（含边界）。"""

    @pytest.mark.parametrize("hm", [(9, 30), (11, 30), (13, 0), (15, 0)])
    def test_boundary_inclusive(self, hm, trade_calendar_stub):
        """四边界 09:30/11:30/13:00/15:00 均为交易时段（闭区间含边界）。"""
        now = datetime(2026, 8, 24, hm[0], hm[1])
        assert runner.is_trading_time(now) is True

    @pytest.mark.parametrize("hm", [(9, 29), (11, 31), (12, 59), (15, 1)])
    def test_outside_session_false(self, hm, trade_calendar_stub):
        """边界外（09:29/午休 11:31-12:59/15:01）均为非交易时段。"""
        now = datetime(2026, 8, 24, hm[0], hm[1])
        assert runner.is_trading_time(now) is False

    def test_non_trading_day_false(self, trade_calendar_stub):
        """非交易日（缓存中无该日，如周末）→ 盘中时刻也判为非交易时段。"""
        now = datetime(2026, 8, 22, 10, 0)
        assert runner.is_trading_time(now) is False

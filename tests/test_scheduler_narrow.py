"""架构深化 K：调度窄读（runner.next_run_for / has_run_today）单元测试。

回归根因：scheduler_loop 触发判定与 /api/pipeline-schedule 展示推算各自读
settings 重算一遍，口径不一致——只填 hour 不填 minute 时 API 显示启用但
调度永不触发；现收敛为 runner 单一窄 interface，触发侧与展示侧同一口径。
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime, timedelta

import app.web.runner as runner


class TestNextRunFor:
    """下次执行推算（hour+minute 都非空才启用）。"""

    def test_disabled_when_minute_missing(self, monkeypatch):
        """只填 hour 不填 minute → 未启用（修复：API 曾显示启用但永不触发）。"""
        monkeypatch.setattr(runner, "load_settings",
                            lambda: {"scheduler": {"hour": "14"}})
        assert runner.next_run_for() is None

    def test_disabled_when_hour_missing(self, monkeypatch):
        monkeypatch.setattr(runner, "load_settings",
                            lambda: {"scheduler": {"minute": "30"}})
        assert runner.next_run_for() is None

    def test_disabled_when_empty_sched(self, monkeypatch):
        monkeypatch.setattr(runner, "load_settings", lambda: {})
        assert runner.next_run_for() is None

    def test_future_run_today(self, monkeypatch):
        """未到触发时刻 → 该时刻所在日（晚 22 点后 +2h 跨天则次日，与实现同一推算规则自洽）。"""
        now = datetime.now()
        future = (now + timedelta(hours=2)).strftime("%H:%M")
        h, m = future.split(":")
        monkeypatch.setattr(runner, "load_settings",
                            lambda: {"scheduler": {"hour": h, "minute": m}})
        nxt = runner.next_run_for("全流程")
        assert nxt is not None
        expect = now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
        expected_day = ((expect + timedelta(days=1)).strftime("%Y-%m-%d")
                        if expect <= now else expect.strftime("%Y-%m-%d"))
        assert nxt.startswith(expected_day)

    def test_past_run_tomorrow(self, monkeypatch):
        """已过触发时刻且当日已跑 → 该时刻顺延一天（与实现同一推算规则自洽验证）。"""
        now = datetime.now()
        past = now - timedelta(minutes=30)
        h, m = past.strftime("%H"), past.strftime("%M")
        monkeypatch.setattr(runner, "load_settings",
                            lambda: {"scheduler": {"hour": h, "minute": m}})
        monkeypatch.setattr(runner, "has_run_today", lambda slot: True)
        nxt = runner.next_run_for("全流程")
        assert nxt is not None
        expect = now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
        expected_day = ((expect + timedelta(days=1)).strftime("%Y-%m-%d")
                        if expect <= now else expect.strftime("%Y-%m-%d"))
        assert nxt.startswith(expected_day)

    def test_past_not_run_shows_today_catchup(self, monkeypatch):
        """已过触发时刻但当日未跑（如服务重启） → 显示今天（下一 tick 即补跑），
        与窗口触发的实际行为一致，不再误报为明天。"""
        now = datetime.now()
        past = now - timedelta(minutes=30)
        h, m = past.strftime("%H"), past.strftime("%M")
        monkeypatch.setattr(runner, "load_settings",
                            lambda: {"scheduler": {"hour": h, "minute": m}})
        monkeypatch.setattr(runner, "has_run_today", lambda slot: False)
        nxt = runner.next_run_for("全流程")
        assert nxt is not None
        assert nxt.startswith(now.strftime("%Y-%m-%d"))

    def test_sched_run_time_single_source(self, monkeypatch):
        """_sched_run_time 单一口径：调度与展示共用的启用判定/触发点解析。

        修复：展示侧曾只判 hour（仅填小时时显示启用但调度永不触发）——
        启用语义现收敛为 time 解析器统一输出。
        """
        now = datetime(2026, 8, 24, 12, 0)
        # 字符串与整数 hour 都可用
        assert runner._sched_run_time(now, {"hour": "11", "minute": "30"}) == now.replace(
            hour=11, minute=30)
        assert runner._sched_run_time(now, {"hour": 11, "minute": 30}) == now.replace(
            hour=11, minute=30)
        # 缺 minute / 空值 → 未启用
        assert runner._sched_run_time(now, {"hour": "11"}) is None
        assert runner._sched_run_time(now, {"hour": "", "minute": "30"}) is None
        assert runner._sched_run_time(now, {}) is None


class TestHasRunToday:
    """今日去重（调度触发与展示共用）。"""

    def test_true_when_marked_today(self, monkeypatch):
        today = datetime.now().strftime("%Y-%m-%d")
        monkeypatch.setattr(runner, "slot_last_run", lambda slot: today)
        assert runner.has_run_today("full") is True

    def test_false_when_not_marked(self, monkeypatch):
        monkeypatch.setattr(runner, "slot_last_run", lambda slot: "2026-01-01")
        assert runner.has_run_today("full") is False

    def test_false_when_none(self, monkeypatch):
        monkeypatch.setattr(runner, "slot_last_run", lambda slot: None)
        assert runner.has_run_today("full") is False

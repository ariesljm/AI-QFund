"""净值增量对齐规划测试——回归根因：增量跳过判断误用本地全局最新日期。

修复前 bug：跳过判断以本地 MAX(date) 为基准，当全库基金停在旧日期 D 而接口
已有 D+1 数据时，所有基金被跳过，新数据永远拉不到。
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio

import pytest

from app.data import nav


# ============================================================
# _plan_nav_tasks — 增量任务规划（对齐到接口最新日期）
# ============================================================

class TestPlanNavTasks:
    def test_api_newer_than_local_plans_full_sync(self):
        """接口最新日期 > 本地全局最新 → 所有基金都应规划增量（全库对齐）。"""
        all_codes = ["000001", "000002", "000003"]
        local_max = {"000001": "2026-07-31", "000002": "2026-07-31", "000003": "2026-07-31"}
        tasks, incr, full = nav._plan_nav_tasks(
            all_codes, local_max, global_latest="2026-07-31", api_latest="2026-08-03"
        )
        assert len(tasks) == 3
        assert incr == 3
        assert full == 0
        assert ("000001", "2026-07-31") in tasks
        assert ("000002", "2026-07-31") in tasks
        assert ("000003", "2026-07-31") in tasks

    def test_no_new_data_only_lagging_funds(self):
        """接口最新 == 本地全局最新 → 已对齐基金跳过，仅滞后基金增量。"""
        all_codes = ["000001", "000002"]
        local_max = {"000001": "2026-07-31", "000002": "2026-07-28"}
        tasks, incr, full = nav._plan_nav_tasks(
            all_codes, local_max, global_latest="2026-07-31", api_latest="2026-07-31"
        )
        assert tasks == [("000002", "2026-07-28")]
        assert incr == 1
        assert full == 0

    def test_probe_failed_falls_back_to_local(self):
        """探测失败（api_latest=None）→ 降级为原逻辑：跳过本地==global_latest 的基金。"""
        all_codes = ["000001", "000002", "000003"]
        local_max = {"000001": "2026-07-31", "000002": "2026-07-31", "000003": "2026-07-30"}
        tasks, incr, full = nav._plan_nav_tasks(
            all_codes, local_max, global_latest="2026-07-31", api_latest=None
        )
        assert tasks == [("000003", "2026-07-30")]
        assert incr == 1
        assert full == 0

    def test_fund_without_local_data_plans_full(self):
        """无本地数据的基金（新基金/丢失）→ 规划全量兜底。"""
        all_codes = ["000001", "000004"]
        local_max = {"000001": "2026-07-31"}
        tasks, incr, full = nav._plan_nav_tasks(
            all_codes, local_max, global_latest="2026-07-31", api_latest="2026-08-03"
        )
        assert tasks == [("000001", "2026-07-31"), ("000004", "")]
        assert incr == 1
        assert full == 1

    def test_fund_already_at_api_latest_skipped(self):
        """接口有新数据但某基金已对齐到接口最新 → 该基金跳过。"""
        all_codes = ["000001", "000002"]
        local_max = {"000001": "2026-08-03", "000002": "2026-07-31"}
        tasks, incr, full = nav._plan_nav_tasks(
            all_codes, local_max, global_latest="2026-08-03", api_latest="2026-08-03"
        )
        assert tasks == [("000002", "2026-07-31")]
        assert incr == 1
        assert full == 0


# ============================================================
# _probe_lsjz_latest — 多基金探测取最大日期
# ============================================================

def _lsjz_text(day: str) -> str:
    """构造单条 lsjz 响应文本（jQuery 包裹）。"""
    return (
        'jQuery({"Data":{"LSJZList":[{"FSRQ":"%s","LJJZ":"1.5"}],'
        '"TotalCount":1},"ErrCode":0,"Success":true,"Message":""});'
    ) % day


class _FakeResp:
    def __init__(self, text: str):
        self._text = text

    async def text(self) -> str:
        return self._text


class TestProbeLsjzLatest:
    def test_takes_max_across_funds(self, monkeypatch):
        """不同基金返回不同日期 → 取最大日期。"""

        async def fake_fetch(session, url, timeout=15, headers=None):
            code = url.split("fundCode=")[1].split("&")[0]
            days = {"000001": "2026-08-03", "110011": "2026-07-31", "161725": "2026-08-02"}
            return _FakeResp(_lsjz_text(days.get(code, "2026-07-30")))

        monkeypatch.setattr(nav, "fetch_async", fake_fetch)
        latest = asyncio.run(nav._probe_lsjz_latest(session=None, headers={}))
        assert latest == "2026-08-03"

    def test_ignores_failed_fund(self, monkeypatch):
        """某只基金请求失败 → 跳过，继续用其他基金结果。"""

        async def fake_fetch(session, url, timeout=15, headers=None):
            code = url.split("fundCode=")[1].split("&")[0]
            if code == "000001":
                raise asyncio.TimeoutError("boom")
            return _FakeResp(_lsjz_text("2026-07-31"))

        monkeypatch.setattr(nav, "fetch_async", fake_fetch)
        latest = asyncio.run(nav._probe_lsjz_latest(session=None, headers={}))
        assert latest == "2026-07-31"

    def test_all_failed_returns_none(self, monkeypatch):
        """全部基金探测失败 → 返回 None（上层降级）。"""

        async def fake_fetch(session, url, timeout=15, headers=None):
            raise asyncio.TimeoutError("boom")

        monkeypatch.setattr(nav, "fetch_async", fake_fetch)
        latest = asyncio.run(nav._probe_lsjz_latest(session=None, headers={}))
        assert latest is None

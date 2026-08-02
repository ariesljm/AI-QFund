"""净值增量对齐规划测试——回归根因：增量跳过判断误用本地全局最新日期。

修复前 bug：跳过判断以本地 MAX(date) 为基准，当全库基金停在旧日期 D 而接口
已有 D+1 数据时，所有基金被跳过，新数据永远拉不到。
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio

import pytest

import app.database as db_mod
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


@pytest.fixture
def iso_db(monkeypatch, tmp_path):
    """隔离 DB：指向临时数据库，并强制 _migrate 重新执行（覆盖 schema）。"""
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(db_mod._migrate, "_done", False, raising=False)
    yield


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


# ============================================================
# async_update_nav_incremental — 确认无新数据/空结果也应计入失败与冷却
# ============================================================

class TestIncrementalNoUpdateCooldown:
    """回归：接口确认无新数据（停更/滞后/无净值页）的基金需累计失败进入冷却，
    否则每次运行都会反复重试，日志中增量/全量兜底数量恒定不变。"""

    @staticmethod
    def _lsjz_resp(days: list[str]) -> str:
        rows = ",".join(f'{{"FSRQ":"{d}","LJJZ":"1.5"}}' for d in days)
        return f'jQuery({{"Data":{{"LSJZList":[{rows}]}},"TotalCount":{len(days)}}})'

    @staticmethod
    def _seed_buyable(code: str) -> None:
        with db_mod.db_conn() as conn:
            conn.execute(
                "INSERT INTO fund_basic (code, name, type, is_buyable) VALUES (?, ?, ?, ?)",
                (code, "测试基金", "混合型", 1),
            )

    @staticmethod
    def _seed_aligned(code: str, date: str) -> None:
        """种入一只已对齐到接口最新日期的基金，保证全局最新日期的探测基准存在。"""
        with db_mod.db_conn() as conn:
            conn.execute("INSERT INTO fund_nav (code, date, cum_nav) VALUES (?, ?, ?)",
                         (code, date, 1.5))

    def _stub_empty(self, monkeypatch):
        """lsjz 与 pingzhongdata 均返回空（接口确认无数据）。"""
        async def fake_probe(session, headers):
            return "2026-07-31"

        async def fake_fetch(session, url, timeout=15, headers=None):
            if "lsjz" in url:
                return _FakeResp(self._lsjz_resp([]))
            return _FakeResp("var ACWorthTrend = [];")

        monkeypatch.setattr(nav, "_probe_lsjz_latest", fake_probe)
        monkeypatch.setattr(nav, "fetch_async", fake_fetch)

    def test_no_data_full_fund_enters_cooldown(self, iso_db, monkeypatch):
        """无本地数据的基金每次全量兜底拉空 → 记失败，连续 3 个周期后进入冷却被跳过。"""
        self._seed_buyable("000099")
        self._seed_aligned("000001", "2026-07-31")
        self._stub_empty(monkeypatch)

        for _ in range(3):
            asyncio.run(nav.async_update_nav_incremental(concurrency=1))

        rows = {r["target"]: r for r in nav.list_failures("nav_incr")}
        assert rows["000099"]["attempts"] == 3
        assert rows["000099"]["status"] == "failed"

        # 第 4 次运行：000099 进入冷却不再拉取；000001 已对齐也跳过 → 无任何网络请求
        async def fake_assert(session, url, timeout=15, headers=None):
            raise AssertionError("冷却/对齐基金不应再发起网络请求")

        monkeypatch.setattr(nav, "fetch_async", fake_assert)
        asyncio.run(nav.async_update_nav_incremental(concurrency=1))

    def test_lagging_fund_no_update_enters_cooldown(self, iso_db, monkeypatch):
        """本地滞后的基金拉取空 → 无新数据，连续 3 个周期后进入冷却。"""
        self._seed_buyable("000002")
        with db_mod.db_conn() as conn:
            conn.execute("INSERT INTO fund_nav (code, date, cum_nav) VALUES ('000002', '2026-07-30', 1.2)")
        self._seed_aligned("000001", "2026-07-31")
        self._stub_empty(monkeypatch)

        for _ in range(3):
            asyncio.run(nav.async_update_nav_incremental(concurrency=1))

        rows = {r["target"]: r for r in nav.list_failures("nav_incr")}
        assert rows["000002"]["attempts"] == 3
        assert "000001" not in rows  # 已对齐到接口最新，从未被规划

    def test_no_update_fund_recovers_when_data_arrives(self, iso_db, monkeypatch):
        """无新数据基金恢复更新后：写入成功清除失败记录，不再进入冷却。"""
        self._seed_buyable("000002")
        with db_mod.db_conn() as conn:
            conn.execute("INSERT INTO fund_nav (code, date, cum_nav) VALUES ('000002', '2026-07-30', 1.2)")
        self._seed_aligned("000001", "2026-07-31")

        async def fake_probe(session, headers):
            return "2026-07-31"

        async def fake_empty(session, url, timeout=15, headers=None):
            if "lsjz" in url:
                return _FakeResp(self._lsjz_resp([]))
            return _FakeResp("var ACWorthTrend = [];")

        monkeypatch.setattr(nav, "_probe_lsjz_latest", fake_probe)
        monkeypatch.setattr(nav, "fetch_async", fake_empty)
        asyncio.run(nav.async_update_nav_incremental(concurrency=1))
        rows = {r["target"]: r for r in nav.list_failures("nav_incr")}
        assert rows["000002"]["attempts"] == 1

        # 次轮接口恢复更新 → 写入成功 → 失败记录清除，不再进入冷却
        async def fake_data(session, url, timeout=15, headers=None):
            if "lsjz" in url:
                return _FakeResp(self._lsjz_resp(["2026-07-31"]))
            return _FakeResp("var ACWorthTrend = [];")

        monkeypatch.setattr(nav, "fetch_async", fake_data)
        asyncio.run(nav.async_update_nav_incremental(concurrency=1))
        rows = {r["target"]: r for r in nav.list_failures("nav_incr")}
        assert rows["000002"]["status"] == "recovered"
        assert "000002" not in nav.cooldown_targets("nav_incr")

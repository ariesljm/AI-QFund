"""净值增量对齐规划测试——回归根因：增量跳过判断误用本地全局最新日期。

修复前 bug：跳过判断以本地 MAX(date) 为基准，当全库基金停在旧日期 D 而接口
已有 D+1 数据时，所有基金被跳过，新数据永远拉不到。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

import app.database as db_mod
from app.data import nav
from app.data.ingest import run_batched_fetch
from app.data.store import cooldown_targets, list_failures

# ============================================================
# _split_tasks — 增量任务三路拆分（差1天批量/差多天lsjz/无本地全量）
# ============================================================

class TestSplitTasks:
    def test_one_day_lag_goes_batch(self):
        """本地最新 == 全局最新（只差最新 1 天）→ 批量路径。"""
        tasks = [("000001", "2026-09-07"), ("110011", "2026-09-07")]
        batch, lag, full = nav._split_tasks(tasks, "2026-09-07")
        assert batch == ["000001", "110011"]
        assert lag == [] and full == []

    def test_multi_day_lag_goes_lsjz(self):
        """本地最新 < 全局最新（差 2+ 天，QDII/停更）→ lsjz 逐只补全。"""
        tasks = [("000001", "2026-09-05"), ("110011", "2026-09-06")]
        batch, lag, full = nav._split_tasks(tasks, "2026-09-07")
        assert batch == []
        assert lag == tasks
        assert full == []

    def test_no_local_goes_full(self):
        """本地无数据（lm 空串）→ pingzhongdata 全量。"""
        tasks = [("028944", "")]
        batch, lag, full = nav._split_tasks(tasks, "2026-09-07")
        assert batch == [] and lag == []
        assert full == tasks

    def test_mixed_split(self):
        tasks = [("a", "2026-09-07"), ("b", "2026-09-05"), ("c", "")]
        batch, lag, full = nav._split_tasks(tasks, "2026-09-07")
        assert batch == ["a"]
        assert lag == [("b", "2026-09-05")]
        assert full == [("c", "")]

    def test_empty_global_latest_no_batch(self):
        """global_latest 为 None（库空）时不误判 full 基金进批量。"""
        tasks = [("a", "")]
        batch, lag, full = nav._split_tasks(tasks, None)
        assert batch == [] and lag == []
        assert full == tasks


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
        'jQuery({"Data":{"LSJZList":[{"FSRQ":"' + day + '","LJJZ":"1.5"}],'
        '"TotalCount":1},"ErrCode":0,"Success":true,"Message":""});'
    )


class _FakeResp:
    def __init__(self, text: str):
        self._text = text

    @property
    def text(self) -> str:
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
                raise TimeoutError("boom")
            return _FakeResp(_lsjz_text("2026-07-31"))

        monkeypatch.setattr(nav, "fetch_async", fake_fetch)
        latest = asyncio.run(nav._probe_lsjz_latest(session=None, headers={}))
        assert latest == "2026-07-31"

    def test_all_failed_returns_none(self, monkeypatch):
        """全部基金探测失败 → 返回 None（上层降级）。"""

        async def fake_fetch(session, url, timeout=15, headers=None):
            raise TimeoutError("boom")

        monkeypatch.setattr(nav, "fetch_async", fake_fetch)
        latest = asyncio.run(nav._probe_lsjz_latest(session=None, headers={}))
        assert latest is None


# ============================================================
# cooldown_targets — 按失败类型分级冷却
# ============================================================

class TestCooldownByStage:
    """分级冷却：确认无新数据（no_update）用长冷却（7 天），
    临时拉取失败（primary）保持短冷却（1 天）。"""

    @staticmethod
    def _seed_failure(fetch_type, target, stage, attempts, last_failed_at):
        with db_mod.db_conn() as conn:
            conn.execute(
                "INSERT INTO data_fetch_failures "
                "(fetch_type, target, stage, error, attempts, status, last_failed_at) "
                "VALUES (?, ?, ?, '', ?, 'failed', ?)",
                (fetch_type, target, stage, attempts, last_failed_at),
            )

    @staticmethod
    def _days_ago(days: int) -> str:
        return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    def test_no_update_uses_longer_cooldown(self, iso_db):
        """2 天前失败：no_update 目标（7 天冷却）仍在冷却，primary 目标（1 天）已过期。"""
        self._seed_failure("nav_incr", "000001", "no_update", 3, self._days_ago(2))
        self._seed_failure("nav_incr", "000002", "primary", 3, self._days_ago(2))
        cooled = cooldown_targets("nav_incr", stage_cooldown_days={"no_update": 7})
        assert "000001" in cooled
        assert "000002" not in cooled

    def test_no_update_cooldown_expires(self, iso_db):
        """10 天前失败：超过 7 天冷却期 → 不再冷却，允许重试。"""
        self._seed_failure("nav_incr", "000001", "no_update", 3, self._days_ago(10))
        assert "000001" not in cooldown_targets("nav_incr", stage_cooldown_days={"no_update": 7})

    def test_without_stage_map_keeps_default(self, iso_db):
        """不传 stage_cooldown_days → 所有失败类型都用默认 1 天冷却。"""
        self._seed_failure("nav_incr", "000001", "no_update", 3, self._days_ago(2))
        assert "000001" not in cooldown_targets("nav_incr")


# ============================================================
# _count_stale_lagging — 日志统计长期停更基金数
# ============================================================

class TestCountStaleLagging:
    def test_counts_only_lag_ge_2_days(self):
        """滞后 1 天（QDII 正常晚发布）不算停更；滞后 >= 2 天算停更；空 start_date 跳过。"""
        meta = [
            ("a", "2026-08-11"),  # 滞后 1 天 → 不算
            ("b", "2026-08-10"),  # 滞后 2 天 → 算
            ("c", "2026-08-07"),  # 滞后 5 天 → 算
            ("d", ""),            # 全量兜底 → 跳过
        ]
        assert nav._count_stale_lagging(meta, "2026-08-12") == 2

    def test_no_target_returns_zero(self):
        assert nav._count_stale_lagging([("a", "2026-08-01")], None) == 0


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

        rows = {r["target"]: r for r in list_failures("nav_incr")}
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

        rows = {r["target"]: r for r in list_failures("nav_incr")}
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
        rows = {r["target"]: r for r in list_failures("nav_incr")}
        assert rows["000002"]["attempts"] == 1

        # 次轮接口恢复更新 → 写入成功 → 失败记录清除，不再进入冷却
        async def fake_data(session, url, timeout=15, headers=None):
            if "lsjz" in url:
                return _FakeResp(self._lsjz_resp(["2026-07-31"]))
            return _FakeResp("var ACWorthTrend = [];")

        monkeypatch.setattr(nav, "fetch_async", fake_data)
        asyncio.run(nav.async_update_nav_incremental(concurrency=1))
        rows = {r["target"]: r for r in list_failures("nav_incr")}
        assert rows["000002"]["status"] == "recovered"
        assert "000002" not in cooldown_targets("nav_incr")


# ============================================================
# 增量新增条数 — 只统计真正缺失的交易日（边界日不重复计数）
# ============================================================

class TestIncrementalNewCount:
    """回归：lsjz 的 startDate 是闭区间（返回含本地最新日），
    写入时按 date > 本地最新过滤，新增条数必须等于真正缺失的交易日数。
    """

    @staticmethod
    def _lsjz_resp(days: list[str]) -> str:
        rows = ",".join(f'{{"FSRQ":"{d}","LJJZ":"1.5"}}' for d in days)
        return f'jQuery({{"Data":{{"LSJZList":[{rows}]}},"TotalCount":{len(days)}}})'

    def _seed(self, code: str, latest: str) -> None:
        with db_mod.db_conn() as conn:
            conn.execute(
                "INSERT INTO fund_basic (code, name, type, is_buyable) VALUES (?, ?, ?, ?)",
                (code, "测试基金", "混合型", 1),
            )
            conn.execute("INSERT INTO fund_nav (code, date, cum_nav) VALUES (?, ?, ?)",
                         (code, latest, 1.2))

    def _run(self, monkeypatch, api_days: list[str]) -> int:
        async def fake_probe(session, headers):
            return api_days[0]  # 接口最新日期

        async def fake_fetch(session, url, timeout=15, headers=None):
            if "lsjz" in url:
                return _FakeResp(self._lsjz_resp(api_days))
            return _FakeResp("var ACWorthTrend = [];")

        monkeypatch.setattr(nav, "_probe_lsjz_latest", fake_probe)
        monkeypatch.setattr(nav, "fetch_async", fake_fetch)
        return asyncio.run(nav.async_update_nav_incremental(concurrency=1))

    def test_one_new_day_counts_one(self, iso_db, monkeypatch):
        """本地最新 07-31，接口新增 08-03（响应含边界日 07-31）→ 只写 1 条。"""
        self._seed("000002", "2026-07-31")
        n = self._run(monkeypatch, ["2026-08-03", "2026-07-31"])
        assert n == 1
        with db_mod.db_conn() as conn:
            days = [r[0] for r in conn.execute(
                "SELECT date FROM fund_nav WHERE code='000002' ORDER BY date")]
        assert days == ["2026-07-31", "2026-08-03"]  # 边界日不重复入库

    def test_two_missing_days_count_two(self, iso_db, monkeypatch):
        """本地最新 07-30，缺失 07-31 与 08-03 两个交易日 → 写 2 条。"""
        self._seed("000002", "2026-07-30")
        n = self._run(monkeypatch, ["2026-08-03", "2026-07-31", "2026-07-30"])
        assert n == 2
        with db_mod.db_conn() as conn:
            days = [r[0] for r in conn.execute(
                "SELECT date FROM fund_nav WHERE code='000002' ORDER BY date")]
        assert days == ["2026-07-30", "2026-07-31", "2026-08-03"]

    def test_no_new_day_counts_zero(self, iso_db, monkeypatch):
        """接口无新数据（本地已对齐）→ 基金跳过，新增 0 条。"""
        self._seed("000002", "2026-07-31")
        n = self._run(monkeypatch, ["2026-07-31"])
        assert n == 0


# ============================================================
# _parse_lsjz_page / 增量 — 假空（反爬垃圾响应）必须按拉取失败处理
# ============================================================

class TestLsjzGarbageResponse:
    """回归：反爬/限流返回的空 body、拦截页与\"接口确认无数据\"不可混为一谈。

    修复前：_parse_lsjz_page 解析失败返回 [],0 → 被记为 no_update，
    健康基金连续 3 个周期后误入 7 天长冷却，造成全库静默断档。
    """

    def test_parse_garbage_raises(self):
        """空 body/HTML 拦截页/非 JSON 文本应抛 ValueError，而非静默返回空。"""
        for garbage in ["", "  ", "<html>请开启 JavaScript</html>", "jQuery(", "null"]:
            with pytest.raises(ValueError):
                nav._parse_lsjz_page(garbage)

    def test_parse_valid_empty_is_not_error(self):
        """合法 JSON 且 TotalCount=0 才是真·无数据，返回空列表不抛错。"""
        text = 'jQuery({"Data":{"LSJZList":[]},"TotalCount":0})'
        assert nav._parse_lsjz_page(text) == ([], 0)

    def test_garbage_response_recorded_as_primary_failure(self, iso_db, monkeypatch):
        """lsjz 返回垃圾响应 → 记为 primary 拉取失败（短冷却+补查），不是 no_update。"""
        with db_mod.db_conn() as conn:
            conn.execute(
                "INSERT INTO fund_basic (code, name, type, is_buyable) VALUES (?, ?, ?, ?)",
                ("000099", "测试基金", "混合型", 1),
            )
            conn.execute(
                "INSERT INTO fund_nav (code, date, cum_nav) VALUES ('000099', '2026-07-30', 1.2)")

        async def fake_probe(session, headers):
            return "2026-07-31"

        async def fake_fetch(session, url, timeout=15, headers=None):
            if "lsjz" in url:
                return _FakeResp("")  # 反爬空 body
            return _FakeResp("var ACWorthTrend = [];")

        monkeypatch.setattr(nav, "_probe_lsjz_latest", fake_probe)
        monkeypatch.setattr(nav, "fetch_async", fake_fetch)
        asyncio.run(nav.async_update_nav_incremental(concurrency=1))

        rows = {r["target"]: r for r in list_failures("nav_incr")}
        assert rows["000099"]["stage"] == "primary"
        assert rows["000099"]["attempts"] == 1


# ============================================================
# run_batched_fetch — 大批量"无新数据"占比过高时按系统性故障处理
# ============================================================

class TestNoUpdateSystemicGuard:
    """回归：大批量下载中确认无新数据的占比超过熔断阈值 → 疑似接口批量异常
    （如反爬返回空响应被解析为无数据），改按拉取失败记录，避免健康基金
    误入长冷却；小批量维持原语义。"""

    @staticmethod
    def _run(targets: list[str], no_update_count: int) -> None:
        no_update = set(targets[:no_update_count])

        async def fetch_one(session_, item):
            return item, None, False

        def handle_batch(conn_, results):
            return {
                "new_count": 0,
                "success": {c for c, _, f in results if c not in no_update and not f},
                "no_update": [c for c, _, f in results if c in no_update],
                "failed": [c for c, _, f in results if f],
            }

        asyncio.run(run_batched_fetch(
            session=None, fetch_type="nav_incr", label="增量净值",
            targets=targets, batch_size=100,
            fetch_one=fetch_one, handle_batch=handle_batch,
            no_update_note="接口确认无新数据", primary_note="增量净值拉取失败",
        ))

    def test_large_batch_mostly_no_update_treated_as_systemic(self, iso_db):
        targets = [f"{i:06d}" for i in range(101)]
        self._run(targets, no_update_count=90)  # 89% 无新数据 > 熔断阈值 50%

        rows = {r["target"]: r["stage"] for r in list_failures("nav_incr")}
        assert len(rows) == 90  # 仅无新数据的 90 只记录失败，其余 11 只按成功清除
        assert all(stage == "primary" for stage in rows.values())  # 全部按拉取失败记录

    def test_small_batch_no_update_keeps_original_semantics(self, iso_db):
        targets = [f"{i:06d}" for i in range(10)]
        self._run(targets, no_update_count=9)  # 占比同样 90%，但小批量不触发护栏

        rows = {r["target"]: r["stage"] for r in list_failures("nav_incr")}
        assert len(rows) == 9
        assert all(stage == "no_update" for stage in rows.values())

    def test_guard_exempt_via_no_update_guard_false(self, iso_db):
        """高占比\"无数据\"属常态的下载（如持仓）可豁免护栏：照常记 no_update 长冷却。"""
        targets = [f"{i:06d}" for i in range(101)]
        no_update = set(targets[:90])

        async def fetch_one(session_, item):
            return item, None, False

        def handle_batch(conn_, results):
            return {
                "new_count": 0,
                "success": {c for c, _, f in results if c not in no_update},
                "no_update": [c for c, _, f in results if c in no_update],
                "failed": [],
            }

        asyncio.run(run_batched_fetch(
            session=None, fetch_type="holdings", label="持仓",
            targets=targets, batch_size=100,
            fetch_one=fetch_one, handle_batch=handle_batch,
            no_update_note="接口无持仓披露", no_update_guard=False,
        ))

        rows = {r["target"]: r["stage"] for r in list_failures("holdings")}
        assert len(rows) == 90
        assert all(stage == "no_update" for stage in rows.values())  # 未被误判为 primary


# ============================================================
# fundmobapi 批量路径记账契约（架构深化候选 2）
# ============================================================

class TestBatchNoUpdateAccounting:
    """批量分支记账与 lsjz 路径同语义：no_update 进冷却、系统性假空回退 lsjz。"""

    @staticmethod
    def _lsjz_resp(days: list[str]) -> str:
        rows = ",".join(f'{{"FSRQ":"{d}","LJJZ":"1.5"}}' for d in days)
        return f'jQuery({{"Data":{{"LSJZList":[{rows}]}},"TotalCount":{len(days)}}})'

    @staticmethod
    def _seed(code: str, aligned_to: str) -> None:
        with db_mod.db_conn() as conn:
            conn.execute(
                "INSERT INTO fund_basic (code, name, type, is_buyable) VALUES (?, ?, ?, ?)",
                (code, "测试基金", "混合型", 1))
            conn.execute("INSERT INTO fund_nav (code, date, cum_nav) VALUES (?, ?, ?)",
                         (code, aligned_to, 1.5))

    def _stub(self, monkeypatch, batch_pdate: str, lsjz_days: list[str]):
        """probe 定 target=07-31；批量接口返回 PDATE=batch_pdate；lsjz 返回 lsjz_days。"""
        async def fake_probe(session, headers):
            return "2026-07-31"

        async def fake_fetch(session, url, timeout=15, headers=None):
            if "FundMNFInfo" in url:
                import re as _re
                codes = _re.search(r"Fcodes=([\d,]+)", url).group(1).split(",")
                datas = ",".join(
                    f'{{"FCODE":"{c}","PDATE":"{batch_pdate}","ACCNAV":"1.5"}}'
                    for c in codes)
                return _FakeResp(f'{{"Datas":[{datas}],"ErrCode":0}}')
            if "lsjz" in url:
                return _FakeResp(self._lsjz_resp(lsjz_days))
            return _FakeResp("var ACWorthTrend = [];")

        monkeypatch.setattr(nav, "_probe_lsjz_latest", fake_probe)
        monkeypatch.setattr(nav, "fetch_async", fake_fetch)

    def test_batch_no_update_enters_cooldown(self, iso_db, monkeypatch):
        """批量接口返回 PDATE <= 本地最新（确认无新数据）→ 累计失败进冷却。

        回归：批量分支曾绕过失败/冷却状态机（no_update 从不 record_failure），
        系统性重复请求无冷却痕迹。
        """
        self._seed("000099", "2026-07-30")
        self._seed("000001", "2026-07-31")  # 探测基准（已对齐，跳过）
        self._stub(monkeypatch, batch_pdate="2026-07-30", lsjz_days=[])

        for _ in range(3):
            asyncio.run(nav.async_update_nav_incremental(concurrency=1))

        rows = {r["target"]: r for r in list_failures("nav_incr")}
        assert rows["000099"]["attempts"] == 3
        assert rows["000099"]["status"] == "failed"

    def test_batch_systematic_no_update_falls_back_to_lsjz(self, iso_db, monkeypatch):
        """批量 no_update 占比超护栏阈值（系统性假空）→ 并入 lsjz 回退确认，不记账冷却。

        场景：探测定 target=07-31，批量接口 PDATE 集体滞后（返回 07-30）→
        全部 save 得 0。lsjz 逐只确认拉到 07-31 净值并写入——批量接口异常
        不应导致健康基金被误判停更。
        """
        codes = [f"{i:06d}" for i in range(1, 102)]  # 101 只 >= 护栏样本阈值
        for c in codes:
            self._seed(c, "2026-07-30")
        self._stub(monkeypatch, batch_pdate="2026-07-30", lsjz_days=["2026-07-31"])

        total = asyncio.run(nav.async_update_nav_incremental(concurrency=1))

        assert total >= 101  # lsjz 回退每只写入 07-31 一条
        # 无 no_update 冷却记账（系统性假空不误伤）
        no_update_rows = [r for r in list_failures("nav_incr")
                          if r.get("stage") == "no_update"]
        assert no_update_rows == []
        # 数据真实写入
        with db_mod.db_conn() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM fund_nav WHERE date = '2026-07-31'").fetchone()[0]
        assert n == len(codes)

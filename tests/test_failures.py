"""数据拉取失败记录与多轮补查重试测试。

覆盖：record_failure / mark_recovered / list_failures 的幂等与状态流转，
run_backfill_rounds 的多轮重试、失败率保护（backfill_guard）与持久化。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import app.database as db_mod
from app.data import store


@pytest.fixture
def iso_db(monkeypatch, tmp_path):
    """隔离 DB：指向临时数据库，并强制 _migrate 重新执行（覆盖 schema）。"""
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(db_mod._migrate, "_done", False, raising=False)
    yield


class TestFailureRecords:
    def test_record_and_recover(self, iso_db):
        store.record_failure("nav_full", "000001", "超时", stage="primary")
        rows = store.list_failures("nav_full")
        assert len(rows) == 1
        assert rows[0]["target"] == "000001"
        assert rows[0]["status"] == "failed"
        assert rows[0]["attempts"] == 1

        # 再次失败（主循环级，count_attempt 默认 True）：attempts 累积，不重复插入
        store.record_failure("nav_full", "000001", "514限流")
        rows = store.list_failures("nav_full")
        assert len(rows) == 1
        assert rows[0]["attempts"] == 2
        assert rows[0]["error"] == "514限流"

        # 补查阶段失败：count_attempt=False，不增加运行周期计数
        store.record_failure("nav_full", "000001", "补查失败", stage="backfill1",
                             count_attempt=False)
        rows = store.list_failures("nav_full")
        assert rows[0]["attempts"] == 2

        # 恢复
        store.mark_recovered("nav_full", "000001")
        rows = store.list_failures("nav_full")
        assert rows[0]["status"] == "recovered"
        assert rows[0]["recovered_at"]

        # 恢复后再失败：重置为 failed、attempts 从 1 重新计数，清空 recovered_at
        store.record_failure("nav_full", "000001", "再次失败")
        rows = store.list_failures("nav_full")
        assert rows[0]["status"] == "failed"
        assert rows[0]["attempts"] == 1
        assert rows[0]["recovered_at"] is None

    def test_distinct_targets(self, iso_db):
        store.record_failure("nav_full", "000001")
        store.record_failure("holdings", "000002")
        assert len(store.list_failures()) == 2
        assert len(store.list_failures(fetch_type="nav_full")) == 1
        assert len(store.list_failures(status="recovered")) == 0


class TestBackfillRounds:
    def test_all_recovered(self, iso_db):
        failed = ["000001", "000002"]
        calls = []

        def backfill_one(code):
            calls.append(code)

        remaining = store.run_backfill_rounds(
            "nav_full", failed, backfill_one, total=1000, label="测试", rounds=2, delay=0.0)
        assert remaining == []
        assert sorted(calls) == sorted(failed)
        assert all(r["status"] == "recovered" for r in store.list_failures("nav_full"))

    def test_retry_until_success(self, iso_db):
        """前两轮抛异常，第二轮后成功 → 全部恢复，重试轮数符合预期。"""
        failed = ["000001", "000002"]
        attempts = {"000001": 0, "000002": 0}

        def backfill_one(code):
            attempts[code] += 1
            if attempts[code] < 2:
                raise RuntimeError("临时故障")

        remaining = store.run_backfill_rounds(
            "nav_full", failed, backfill_one, total=1000, label="测试", rounds=3, delay=0.0)
        assert remaining == []
        assert all(a == 2 for a in attempts.values())
        assert all(r["status"] == "recovered" for r in store.list_failures("nav_full"))

    def test_persistent_failure(self, iso_db):
        failed = ["000001"]

        def backfill_one(code):
            raise RuntimeError("一直失败")

        remaining = store.run_backfill_rounds(
            "nav_full", failed, backfill_one, total=1000, label="测试", rounds=2, delay=0.0)
        assert remaining == ["000001"]
        rows = store.list_failures("nav_full")
        assert rows[0]["status"] == "failed"
        # 补查阶段失败 count_attempt=False，不累积运行周期计数（主循环的 primary 记录才 +1）
        assert rows[0]["attempts"] == 1

    def test_fail_rate_guard_aborts(self, iso_db):
        """total == len(failed) → 失败率 100% > 50%，backfill_guard 拦截，不做补查。"""
        failed = ["000001", "000002"]
        called = []

        def backfill_one(code):
            called.append(code)

        remaining = store.run_backfill_rounds(
            "nav_full", failed, backfill_one, total=2, label="测试", rounds=2, delay=0.0)
        assert remaining == failed
        assert called == []


class TestCooldown:
    def test_cooldown_requires_attempt_threshold(self, iso_db):
        """attempts 未达阈值（默认 3）时不进入冷却。"""
        store.record_failure("nav_full", "000001")  # attempts=1
        store.record_failure("nav_full", "000002")  # attempts=1
        store.record_failure("nav_full", "000003")  # attempts=1
        store.record_failure("nav_full", "000003")  # attempts=2
        store.record_failure("nav_full", "000003")  # attempts=3
        cooldown = store.cooldown_targets("nav_full")
        assert "000001" not in cooldown
        assert "000002" not in cooldown
        assert "000003" in cooldown

    def test_recovered_not_in_cooldown(self, iso_db):
        """恢复后不再是 failed，不进入冷却。"""
        store.record_failure("nav_full", "000001")
        store.record_failure("nav_full", "000001")
        store.record_failure("nav_full", "000001")  # attempts=3
        store.mark_recovered("nav_full", "000001")
        assert store.cooldown_targets("nav_full") == set()

    def test_stale_failure_not_in_cooldown(self, iso_db):
        """最近失败时间超出冷却期（1 天）的记录不冷却。"""
        store.record_failure("nav_full", "000001")
        store.record_failure("nav_full", "000001")
        store.record_failure("nav_full", "000001")  # attempts=3
        # 把 last_failed_at 改到 2 天前（UTC），模拟冷却期已过
        with db_mod.db_conn() as conn:
            conn.execute(
                "UPDATE data_fetch_failures SET last_failed_at = "
                "datetime('now', '-2 days') WHERE fetch_type='nav_full' AND target='000001'"
            )
        assert store.cooldown_targets("nav_full") == set()


class TestRecoverBatch:
    def test_mark_recovered_batch(self, iso_db):
        """主循环成功拉取后批量清除 failed 记录，仅影响确实 failed 的目标。"""
        store.record_failure("nav_full", "000001")
        store.record_failure("nav_full", "000002")
        store.record_failure("nav_full", "000003")
        # 000002 先手动恢复
        store.mark_recovered("nav_full", "000002")

        store.mark_recovered_batch("nav_full", ["000001", "000002", "000004"])
        by_target = {r["target"]: r["status"] for r in store.list_failures("nav_full")}
        assert by_target["000001"] == "recovered"
        assert by_target["000002"] == "recovered"
        # 000003 未在本批成功列表中，保持 failed；000004 无记录不受影响
        assert by_target["000003"] == "failed"
        assert "000004" not in by_target

    def test_cooldown_cleared_after_recover_batch(self, iso_db):
        """冷却中的基金批量恢复后，不再被冷却逻辑跳过（用户提醒的核心场景）。"""
        store.record_failure("nav_full", "000001")
        store.record_failure("nav_full", "000001")
        store.record_failure("nav_full", "000001")  # attempts=3 → 进入冷却
        assert "000001" in store.cooldown_targets("nav_full")

        store.mark_recovered_batch("nav_full", ["000001"])
        assert "000001" not in store.cooldown_targets("nav_full")

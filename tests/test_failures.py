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
        store.record_failure("nav_full", "000001", "超时", stage="primary", attempts=1)
        rows = store.list_failures("nav_full")
        assert len(rows) == 1
        assert rows[0]["target"] == "000001"
        assert rows[0]["status"] == "failed"

        # 再次失败：attempts 累积更新，不重复插入
        store.record_failure("nav_full", "000001", "514限流", attempts=2)
        rows = store.list_failures("nav_full")
        assert len(rows) == 1
        assert rows[0]["attempts"] == 2
        assert rows[0]["error"] == "514限流"

        # 恢复
        store.mark_recovered("nav_full", "000001")
        rows = store.list_failures("nav_full")
        assert rows[0]["status"] == "recovered"
        assert rows[0]["recovered_at"]

        # 恢复后再失败：重置为 failed，清空 recovered_at
        store.record_failure("nav_full", "000001", "再次失败", attempts=1)
        rows = store.list_failures("nav_full")
        assert rows[0]["status"] == "failed"
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
        assert rows[0]["attempts"] >= 2

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

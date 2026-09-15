"""票 18 决策周期入口：校准层信号记账（触发 → 40 日结算 → assess 消费）。

record_signal_trigger（outcome NULL 待结算）/ settle_signal（0/1 回填）/
get_signal_history（已结算历史）→ calibration.assess 消费（降权/停用判定）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.database as db_mod
from app.engine.calibration import assess
from app.repo.decision import get_signal_history, record_signal_trigger, settle_signal


class TestSignalLedger:
    def test_trigger_settle_assess(self, monkeypatch, tmp_path):
        """全链：触发 → 结算 → assess 用真实历史判定降权。"""
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "sig.db")
        # 7 次命中 + 3 次连续失灵（共 10 样本 ≥ MIN_SAMPLES）
        for i in range(7):
            record_signal_trigger("drift_corr", f"2026-08-{i+1:02d}")
        for i in range(3):
            record_signal_trigger("drift_corr", f"2026-09-{i+1:02d}")
        from app.repo.decision import db_conn
        with db_conn() as conn:
            rows = conn.execute("SELECT ts FROM signal_outcomes "
                                "WHERE signal_id='drift_corr' AND outcome IS NULL "
                                "ORDER BY ts").fetchall()
        # 前 7 条命中，后 3 条未命中（连续失灵）
        for i, (ts,) in enumerate(rows):
            settle_signal("drift_corr", ts, hit=(i < 7))
        hist = get_signal_history("drift_corr")
        assert len(hist) == 10 and sum(hist) == 7
        got = assess(hist)
        assert got["action"] == "downgrade"          # 连续 3 失灵 → 降权
        assert got["hit_rate"] == 0.7 and got["samples"] == 10

    def test_insufficient_samples_no_action(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "sig2.db")
        for i in range(3):
            record_signal_trigger("s1", f"2026-09-{i+1:02d}")
        with db_mod.db_conn() as conn:
            rows = conn.execute("SELECT ts FROM signal_outcomes WHERE signal_id='s1'").fetchall()
        for (ts,) in rows:
            settle_signal("s1", ts, hit=False)
        assert assess(get_signal_history("s1"))["action"] == "hold"   # 样本 < MIN_SAMPLES

    def test_pending_not_counted(self, monkeypatch, tmp_path):
        """未结算（outcome NULL）不进入历史（不污染命中率）。"""
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "sig3.db")
        record_signal_trigger("s2", "2026-09-10")
        record_signal_trigger("s2", "2026-09-11")
        with db_mod.db_conn() as conn:
            rows = conn.execute("SELECT ts FROM signal_outcomes WHERE signal_id='s2'").fetchall()
        settle_signal("s2", rows[0][0], hit=True)   # 只结算第一条
        assert get_signal_history("s2") == [True]   # 第二条待结算不入历史


class TestCalibrationBlock:
    """票 23：校准曲线数据契约。"""

    def test_block_aggregates(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "cal.db")
        from app.repo.decision import db_conn
        from app.web.dashboard import calibration_block
        record_signal_trigger("s1", "2026-09-01")
        record_signal_trigger("s1", "2026-09-02")
        record_signal_trigger("s2", "2026-09-01")
        with db_conn() as conn:
            rows = conn.execute("SELECT signal_id, ts FROM signal_outcomes ORDER BY ts").fetchall()
        settle_signal("s1", rows[0][1], True)
        settle_signal("s1", rows[1][1], False)
        settle_signal("s2", rows[2][1], True)
        block = calibration_block()
        by_id = {b["signal_id"]: b for b in block}
        assert by_id["s1"]["samples"] == 2 and by_id["s1"]["hits"] == 1
        assert by_id["s1"]["hit_rate"] == 0.5
        assert by_id["s2"]["hit_rate"] == 1.0

    def test_empty(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "cal2.db")
        from app.web.dashboard import calibration_block
        assert calibration_block() == []

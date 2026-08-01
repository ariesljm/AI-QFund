"""T2 模型每周自动重训测试。

核心：距上次训练 ≥7 天自动触发重训；meta 记录训练时间。
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import date

from app.engine import recommend
import app.database as db_mod
from app import repo


class TestRetrainDue:
    """_retrain_due：距上次训练 ≥7 天返回 True。"""

    def test_no_record_triggers(self):
        assert recommend._retrain_due(None, today=date(2026, 8, 1)) is True

    def test_same_day_no(self):
        assert recommend._retrain_due("2026-08-01", today=date(2026, 8, 1)) is False

    def test_six_days_no(self):
        assert recommend._retrain_due("2026-07-26", today=date(2026, 8, 1)) is False

    def test_seven_days_triggers(self):
        assert recommend._retrain_due("2026-07-25", today=date(2026, 8, 1)) is True

    def test_old_record_triggers(self):
        assert recommend._retrain_due("2026-01-01", today=date(2026, 8, 1)) is True

    def test_invalid_date_treated_as_due(self):
        assert recommend._retrain_due("garbage", today=date(2026, 8, 1)) is True


class TestModelLastTrainedMeta:
    """meta 记录最近训练时间。"""

    def test_round_trip(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "test.db")
        assert repo.get_model_last_trained() is None
        repo.set_model_last_trained("2026-08-01")
        assert repo.get_model_last_trained() == "2026-08-01"

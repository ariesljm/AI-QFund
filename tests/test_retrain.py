"""T2 模型每周自动重训测试。

核心：距上次训练 ≥7 天自动触发重训；meta 记录训练时间。
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import date

import pandas as pd

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


class _FakeBooster:
    pass


class TestGetOrTrainModel:
    """_get_or_train_model：模型准备 phase 决策可独立测试。"""

    def _empty_data(self):
        return (pd.DataFrame(), pd.Series(dtype=float),
                pd.DataFrame(), pd.Series(dtype=float))

    def test_returns_none_without_model_and_data(self, monkeypatch, tmp_path):
        """无模型且训练样本为空 → 返回 None（跳过本次推荐）。"""
        monkeypatch.setattr(recommend, "MODEL_PATH", tmp_path / "nope.txt")
        monkeypatch.setattr(recommend, "_retrain_due", lambda x: True)
        monkeypatch.setattr(recommend, "prepare_lgb_training_data", self._empty_data)
        assert recommend._get_or_train_model(False) is None

    def test_trains_model_when_due(self, monkeypatch, tmp_path):
        """到期重训成功 → 返回模型并记录训练时间。"""
        model_path = tmp_path / "lgb.txt"
        model_path.write_text("x")
        monkeypatch.setattr(recommend, "MODEL_PATH", model_path)
        monkeypatch.setattr(recommend, "_retrain_due", lambda x: True)
        df = pd.DataFrame({"a": [1.0]})
        monkeypatch.setattr(recommend, "prepare_lgb_training_data",
                            lambda: (df, pd.Series([1.0]), df, pd.Series([1.0])))
        monkeypatch.setattr(recommend, "train_lgb_model", lambda *a, **k: _FakeBooster())
        recorded = {}
        monkeypatch.setattr(recommend.repo, "set_model_last_trained",
                            lambda d: recorded.__setitem__("d", d))
        assert isinstance(recommend._get_or_train_model(False), _FakeBooster)
        assert "d" in recorded

    def test_loads_existing_when_not_due(self, monkeypatch, tmp_path):
        """未到期且模型存在 → 直接加载现有模型。"""
        model_path = tmp_path / "lgb.txt"
        model_path.write_text("x")
        monkeypatch.setattr(recommend, "MODEL_PATH", model_path)
        monkeypatch.setattr(recommend, "_retrain_due", lambda x: False)
        monkeypatch.setattr(recommend.lgb, "Booster", lambda **k: _FakeBooster())
        assert isinstance(recommend._get_or_train_model(False), _FakeBooster)

    def test_retrain_failure_falls_back_to_existing(self, monkeypatch, tmp_path):
        """重训异常但旧模型存在 → 回退加载。"""
        model_path = tmp_path / "lgb.txt"
        model_path.write_text("x")
        monkeypatch.setattr(recommend, "MODEL_PATH", model_path)
        monkeypatch.setattr(recommend, "_retrain_due", lambda x: True)
        monkeypatch.setattr(recommend, "prepare_lgb_training_data",
                            lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        monkeypatch.setattr(recommend.lgb, "Booster", lambda **k: _FakeBooster())
        assert isinstance(recommend._get_or_train_model(False), _FakeBooster)

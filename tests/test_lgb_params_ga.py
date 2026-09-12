"""LightGBM 树结构超参 GA 寻优测试（ticket 10）。

锁定：超参从硬编码剥离（meta 快照优先）、GA 在边界内寻优且不劣于当前、
月度寻优在显著改善时才写 meta 快照。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import app.repo as repo
from app import model as model_mod
from app.database import db_conn
from app.engine import evolve, ga
from app.repo import meta_keys as META


@pytest.fixture(autouse=True)
def _clean_snapshot():
    """每个用例前清空超参快照（conftest 业务库为 session 级共享）。"""
    with db_conn() as conn:
        conn.execute("DELETE FROM meta WHERE key = ?", (META.LGB_PARAMS_SNAPSHOT,))
    yield


class TestGetLgbParams:
    def test_defaults_without_snapshot(self):
        p = model_mod.get_lgb_params()
        assert p["num_leaves"] == 16
        assert p["learning_rate"] == 0.03
        assert p["max_depth"] == 8
        assert p["objective"] == "regression_l1"

    def test_reads_meta_snapshot(self):
        repo.save_meta(META.LGB_PARAMS_SNAPSHOT,
                       json.dumps({"num_leaves": 40, "learning_rate": 0.05, "max_depth": 6}))
        p = model_mod.get_lgb_params()
        assert p["num_leaves"] == 40
        assert p["learning_rate"] == 0.05
        assert p["max_depth"] == 6

    def test_malformed_snapshot_falls_back(self):
        repo.save_meta(META.LGB_PARAMS_SNAPSHOT, "not-json")
        p = model_mod.get_lgb_params()
        assert p["num_leaves"] == 16  # 回退默认


class TestTrainUsesConfiguredParams:
    def test_train_reads_meta_snapshot(self, monkeypatch):
        """训练实际使用 meta 快照超参（硬编码已剥离）。"""
        repo.save_meta(META.LGB_PARAMS_SNAPSHOT,
                       json.dumps({"num_leaves": 40, "learning_rate": 0.05, "max_depth": 6}))
        seen: dict = {}
        monkeypatch.setattr(model_mod.lgb, "Dataset", lambda *a, **k: object())

        def fake_train(params, data, num_boost_round=50):
            seen["params"] = params
            return object()

        monkeypatch.setattr(model_mod.lgb, "train", fake_train)
        model_mod.train(object(), object(), None, save_path=None)

        assert seen["params"]["num_leaves"] == 40
        assert seen["params"]["learning_rate"] == 0.05
        assert seen["params"]["max_depth"] == 6


class TestGaOptimizeLgbParams:
    def test_within_bounds_and_not_worse_than_current(self, monkeypatch):
        def fake_eval(params, data=None):
            return -(abs(params["num_leaves"] - 32) / 100.0
                     + abs(params["learning_rate"] - 0.05)
                     + abs(params["max_depth"] - 8) / 100.0)

        monkeypatch.setattr(ga, "evaluate_lgb_params", fake_eval)
        monkeypatch.setattr(ga, "prepare_training_data",
                            lambda *a, **k: ("X", [0, 1], None, "Xv", [0, 1], None))
        monkeypatch.setattr(ga, "get_lgb_params",
                            lambda: {"learning_rate": 0.03, "num_leaves": 16, "max_depth": 8})

        best, f = ga.ga_optimize_lgb_params(population=6, generations=3, seed=0)

        assert 8 <= best["num_leaves"] <= 64
        assert 0.01 <= best["learning_rate"] <= 0.1
        assert 3 <= best["max_depth"] <= 12
        # 精英保留：结果不劣于当前参数
        cur_f = fake_eval({"learning_rate": 0.03, "num_leaves": 16, "max_depth": 8})
        assert f >= cur_f


class TestLgbParamsTune:
    def test_writes_snapshot_when_significantly_better(self, monkeypatch):
        monkeypatch.setattr(model_mod, "prepare_training_data",
                            lambda *a, **k: ("X", [0, 1], None, "Xv", [0, 1], None))
        monkeypatch.setattr(model_mod, "get_lgb_params",
                            lambda: {"learning_rate": 0.03, "num_leaves": 16, "max_depth": 8})
        monkeypatch.setattr(model_mod, "evaluate_lgb_params", lambda p, d=None: -0.10)
        monkeypatch.setattr(ga, "ga_optimize_lgb_params",
                            lambda **k: ({"learning_rate": 0.05, "num_leaves": 32, "max_depth": 6}, -0.05))
        monkeypatch.setattr(evolve, "_save_self_fix", lambda msg: None)

        msg = evolve._lgb_params_tune()

        assert msg is not None
        saved = repo.get_meta(META.LGB_PARAMS_SNAPSHOT)
        assert saved is not None
        assert json.loads(saved)["num_leaves"] == 32

    def test_no_snapshot_when_no_significant_gain(self, monkeypatch):
        monkeypatch.setattr(model_mod, "prepare_training_data",
                            lambda *a, **k: ("X", [0, 1], None, "Xv", [0, 1], None))
        monkeypatch.setattr(model_mod, "get_lgb_params",
                            lambda: {"learning_rate": 0.03, "num_leaves": 16, "max_depth": 8})
        monkeypatch.setattr(model_mod, "evaluate_lgb_params", lambda p, d=None: -0.10)
        # 改善仅 0.0001 < 阈值 0.0005
        monkeypatch.setattr(ga, "ga_optimize_lgb_params",
                            lambda **k: ({"learning_rate": 0.03, "num_leaves": 17, "max_depth": 8}, -0.0999))
        monkeypatch.setattr(evolve, "_save_self_fix", lambda msg: None)

        assert evolve._lgb_params_tune() is None
        assert repo.get_meta(META.LGB_PARAMS_SNAPSHOT) is None

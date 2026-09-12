"""T01（reco-hardening）：模型标签版本防御验收测试。

背景：重构把训练标签改为 abs_ret_40d，但生产模型是旧标签训练的——若加载路径不校验，
新逻辑（入场门槛/排序/监控退出）会跑在旧模型输出上（最长一周错配窗口）。
本测试固化：标签版本写入/校验/强制重训三件事。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import model as model_mod
from app.repo import meta_keys as META


class TestLabelVersion:
    def test_current_version_is_risk_adjusted(self):
        """当前代码标签版本 = risk_adj_40d_v2（λ 标定 0.5→1.0 后递增，与训练标签单一来源对齐）。"""
        assert model_mod.LABEL_VERSION == "risk_adj_40d_v2"

    def test_mismatch_detected(self, monkeypatch):
        """meta 记录旧标签 → 判定需重训。"""
        monkeypatch.setattr(model_mod.repo, "get_model_label_version", lambda: "abs_ret_20d")
        assert model_mod.label_version_mismatch() is True

    def test_match_ok(self, monkeypatch):
        """meta 记录一致 → 无需重训。"""
        monkeypatch.setattr(model_mod.repo, "get_model_label_version",
                            lambda: model_mod.LABEL_VERSION)
        assert model_mod.label_version_mismatch() is False

    def test_missing_meta_triggers_retrain(self, monkeypatch):
        """老模型无元信息（None）→ 视为不一致（宁可多训一次，不可静默用错标签）。"""
        monkeypatch.setattr(model_mod.repo, "get_model_label_version", lambda: None)
        assert model_mod.label_version_mismatch() is True


class TestGetOrTrain:
    def _fake_train_env(self, monkeypatch):
        """构造"模型文件存在、未到期"的环境；返回训练调用记录。"""
        monkeypatch.setattr(model_mod, "MODEL_PATH", Path("models/lgb_model.txt"))
        monkeypatch.setattr(model_mod, "retrain_due", lambda *a, **k: False)
        # 特征维度校验本组恒定为"匹配"：本组聚焦标签版本逻辑，且真实
        # models/lgb_model.txt 可能是旧维度，会让用例意外走重训分支
        monkeypatch.setattr(model_mod, "feature_dim_mismatch", lambda: False)
        monkeypatch.setattr(model_mod.repo, "set_model_last_trained", lambda d: None)
        monkeypatch.setattr(model_mod.repo, "set_model_label_version", lambda v: None)
        called = {}

        def fake_prep(*a, **k):
            called["prep"] = True
            # X_train/y_train 用非空 list：get_or_train 检查 len(X_train)==0 判定空样本
            return ([1], [1], None, [1], [1], None)

        def fake_train(*a, **k):
            called["train"] = True
            return object()  # 假装 Booster

        monkeypatch.setattr(model_mod, "prepare_training_data", fake_prep)
        monkeypatch.setattr(model_mod, "train", fake_train)
        return called

    def test_retrains_on_label_mismatch(self, monkeypatch):
        """模型文件在、未到期、但标签不匹配 → 仍走重训分支。"""
        monkeypatch.setattr(model_mod.repo, "get_model_label_version", lambda: "old_label")
        called = self._fake_train_env(monkeypatch)
        model_mod.get_or_train()
        assert called.get("train") is True, "标签错配必须触发重训"

    def test_loads_on_label_match(self, monkeypatch):
        """模型文件在、未到期、标签一致 → 走加载分支（不重训）。"""
        monkeypatch.setattr(model_mod.repo, "get_model_label_version",
                            lambda: model_mod.LABEL_VERSION)
        monkeypatch.setattr(model_mod, "load", lambda: "loaded")
        called = self._fake_train_env(monkeypatch)
        out = model_mod.get_or_train()
        assert out == "loaded"
        assert called.get("train") is None, "标签一致时不应重训"

    def test_train_records_label_version(self, monkeypatch):
        """train() 保存模型时写入标签版本 meta（防止下次加载误判）。"""
        seen = {}

        def fake_set(v):
            seen["version"] = v

        monkeypatch.setattr(model_mod.repo, "set_model_label_version", fake_set)
        # 用真实 train 但跳过 lightgbm：mock 到 save_model 层面
        class _FakeBooster:
            def save_model(self, path):
                seen["saved"] = path

        monkeypatch.setattr(model_mod.lgb, "Dataset", lambda *a, **k: object())
        monkeypatch.setattr(model_mod.lgb, "train", lambda *a, **k: _FakeBooster())
        model_mod.train(object(), object(), None)
        assert seen.get("version") == "risk_adj_40d_v2"
        assert seen.get("saved") is not None


_ = META  # noqa: F401  # 保留 meta_keys 引用（键定义单一来源，测试随 repo 演进）

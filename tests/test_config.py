"""config 缓存与持久化测试。

回归：旧实现里 save_settings 清的是 save_settings 函数对象属性，
load_settings 读的是 load_settings 函数对象属性，两槽永不连通；
且 load_settings 返回缓存 dict 本身，web 层 .pop() 会污染共享缓存。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.config as config_mod


@pytest.fixture(autouse=True)
def _isolated_cache():
    """每个测试前后清空 module 级缓存，避免测试间串扰。"""
    config_mod._settings_cache = None
    yield
    config_mod._settings_cache = None


def _isolate_env(monkeypatch, tmp_path):
    monkeypatch.setattr(config_mod, "_ENV_OVERRIDE_MAP", {})
    monkeypatch.setattr(config_mod, "DB_PATH", tmp_path / "nope.db")


class TestConfigCache:
    def test_returned_dict_is_a_copy(self, tmp_path, monkeypatch):
        """调用方 pop 返回值不应污染缓存（回归：GET /api/settings 清掉密码）。"""
        cfg = tmp_path / "settings.toml"
        cfg.write_text('web = { settings_password = "secret" }\n', encoding="utf-8")
        monkeypatch.setattr(config_mod, "SETTINGS_PATH", cfg)
        _isolate_env(monkeypatch, tmp_path)

        first = config_mod.load_settings()
        first.get("web", {}).pop("settings_password", None)

        second = config_mod.load_settings()
        assert second["web"]["settings_password"] == "secret"

    def test_save_settings_invalidates_cache(self, tmp_path, monkeypatch):
        """保存后 load 必须读到新值（回归：旧实现缓存永不失效）。"""
        cfg = tmp_path / "settings.toml"
        cfg.write_text('[llm]\nmodel = "old"\n', encoding="utf-8")
        monkeypatch.setattr(config_mod, "SETTINGS_PATH", cfg)
        _isolate_env(monkeypatch, tmp_path)

        assert config_mod.load_settings()["llm"]["model"] == "old"
        config_mod.save_settings({"llm": {"model": "new"}})
        assert config_mod.load_settings()["llm"]["model"] == "new"

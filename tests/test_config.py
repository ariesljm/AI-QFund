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

    def test_load_settings_picks_up_file_edits(self, tmp_path, monkeypatch):
        """运行期编辑 settings.toml（不重启、不调 save_settings）后 load 必须读到新值。

        回归：web 进程启动后直接改 settings.toml 设置密码不生效，
        check-password 仍用缓存旧值（空密码），导致任意密码都能进设置页。
        """
        cfg = tmp_path / "settings.toml"
        cfg.write_text('[web]\nsettings_password = ""\n', encoding="utf-8")
        monkeypatch.setattr(config_mod, "SETTINGS_PATH", cfg)
        _isolate_env(monkeypatch, tmp_path)

        # 首次加载：空密码
        assert config_mod.load_settings()["web"]["settings_password"] == ""
        # 模拟用户运行期编辑文件设置密码（进程不重启）
        cfg.write_text('[web]\nsettings_password = "secret"\n', encoding="utf-8")
        assert config_mod.load_settings()["web"]["settings_password"] == "secret"

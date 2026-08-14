"""配置管理：TOML 配置加载、环境变量覆盖、运行时持久化。"""

import copy
import hashlib
import json
import os as _os
import re
import tomllib as _tomllib
from pathlib import Path

from app.database import DB_PATH, db_conn
from app.utils.log import get_logger

logger = get_logger("config")

SETTINGS_PATH = Path("config/settings.toml")

# 配置缓存：单槽 module 级状态，由 save_settings 显式失效；
# 文件 mtime 变化（运行期直接编辑 settings.toml）时同样失效。
_settings_cache: dict | None = None
# 缓存失效信号：文件内容 sha256。用内容而非 mtime，因为 Windows 上
# 短时间连续写入同一文件时 mtime 可能不变，mtime 失效会漏掉运行期编辑。
_settings_digest: str | None = None

_ENV_OVERRIDE_MAP = {
    "LLM_BASE_URL": ("llm", "base_url"),
    "LLM_API_KEY": ("llm", "api_key"),
    "LLM_MODEL": ("llm", "model"),
    "SCHEDULER_HOUR": ("scheduler", "hour"),
    "SCHEDULER_MINUTE": ("scheduler", "minute"),
    "WEB_PORT": ("web", "port"),
}


def load_settings() -> dict:
    global _settings_cache, _settings_digest
    # 回归：运行期直接编辑 settings.toml（如设置 settings_password）后，
    # 进程不重启时旧缓存永不失效，web 密码校验会读到过期空值。
    # 以文件内容摘要变化作为缓存失效信号（mtime 在快速连续写入时可能不变）。
    try:
        digest = hashlib.sha256(SETTINGS_PATH.read_bytes()).hexdigest()
    except OSError:
        digest = None
    if _settings_cache is not None and digest == _settings_digest:
        return copy.deepcopy(_settings_cache)
    try:
        with open(SETTINGS_PATH, "rb") as f:
            settings = _tomllib.load(f)
    except FileNotFoundError:
        settings = {}
    for env_key, (section, key) in _ENV_OVERRIDE_MAP.items():
        val = _os.environ.get(env_key)
        if val:
            settings.setdefault(section, {})
            if key not in settings[section]:
                settings[section][key] = val
    try:
        with db_conn() as conn:
            rows = conn.execute(
                "SELECT key, value FROM meta WHERE key LIKE 'settings:%'"
            ).fetchall()
        for key, value in rows:
            parts = key.split(":", 2)
            if len(parts) == 3:
                _, section, name = parts
                settings.setdefault(section, {})[name] = json.loads(value)
    except Exception:
        pass
    _settings_cache = settings
    _settings_digest = digest
    # 返回副本，调用方变异（如 web 层剔除密码）不会污染缓存。
    return copy.deepcopy(settings)


def save_settings(settings: dict) -> bool:
    global _settings_cache
    toml_ok = False
    try:
        text = SETTINGS_PATH.read_text(encoding="utf-8")
        for section, values in settings.items():
            for key, value in values.items():
                if isinstance(value, bool):
                    line = f'{key} = {"true" if value else "false"}'
                elif isinstance(value, int):
                    line = f'{key} = {value}'
                elif isinstance(value, float):
                    line = f'{key} = {value}'
                else:
                    line = f'{key} = "{value}"'
                text = re.sub(rf'^{re.escape(key)}\s*=.*$', line, text, flags=re.MULTILINE)
        SETTINGS_PATH.write_text(text, encoding="utf-8")
        toml_ok = True
    except OSError:
        pass

    if toml_ok:
        if DB_PATH.exists():
            try:
                with db_conn() as conn:
                    conn.execute("DELETE FROM meta WHERE key LIKE 'settings:%'")
            except Exception:
                pass
    else:
        if not DB_PATH.exists():
            raise
        with db_conn() as conn:
            for section, values in settings.items():
                for key, value in values.items():
                    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                                 (f"settings:{section}:{key}", json.dumps(value, ensure_ascii=False)))
    _settings_cache = None
    logger.info("配置已保存: %s", {k: list(v.keys()) for k, v in settings.items()})
    return True

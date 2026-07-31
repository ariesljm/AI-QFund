"""配置管理：TOML 配置加载、环境变量覆盖、运行时持久化。"""

import json
import os as _os
import re
import sqlite3
import tomllib as _tomllib
from pathlib import Path

from app.utils.log import get_logger

logger = get_logger("config")

DB_PATH = Path("data/qfund.db")
SETTINGS_PATH = Path("config/settings.toml")

_ENV_OVERRIDE_MAP = {
    "LLM_BASE_URL": ("llm", "base_url"),
    "LLM_API_KEY": ("llm", "api_key"),
    "LLM_MODEL": ("llm", "model"),
    "SCHEDULER_HOUR": ("scheduler", "hour"),
    "SCHEDULER_MINUTE": ("scheduler", "minute"),
    "WEB_PORT": ("web", "port"),
}


def load_settings():
    cached = getattr(load_settings, '_cached', None)
    if cached is not None:
        return cached
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
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        rows = conn.execute("SELECT key, value FROM meta WHERE key LIKE 'settings:%'").fetchall()
        conn.close()
        for key, value in rows:
            parts = key.split(":", 2)
            if len(parts) == 3:
                _, section, name = parts
                settings.setdefault(section, {})[name] = json.loads(value)
    except Exception:
        pass
    load_settings._cached = settings
    return settings


def save_settings(settings: dict) -> bool:
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
                conn = sqlite3.connect(str(DB_PATH))
                conn.execute("DELETE FROM meta WHERE key LIKE 'settings:%'")
                conn.commit()
                conn.close()
            except Exception:
                pass
    else:
        if not DB_PATH.exists():
            raise
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        for section, values in settings.items():
            for key, value in values.items():
                conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                             (f"settings:{section}:{key}", json.dumps(value, ensure_ascii=False)))
        conn.commit()
        conn.close()
    save_settings._cached = None
    logger.info("配置已保存: %s", {k: list(v.keys()) for k, v in settings.items()})
    return True

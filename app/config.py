"""配置管理：TOML 配置加载、环境变量覆盖、运行时持久化。"""

import copy
import hashlib
import os as _os
import re
import tomllib as _tomllib
from pathlib import Path

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
    _settings_cache = settings
    _settings_digest = digest
    # 返回副本，调用方变异（如 web 层剔除密码）不会污染缓存。
    return copy.deepcopy(settings)


def save_settings(settings: dict) -> bool:
    """把 settings 写回 settings.toml（唯一事实来源，不再双写 DB meta）。

    按 section 定位键行替换；键不存在时在该 section 下追加（覆盖"仅注释无实际行"
    的首次配置场景）。写失败抛 OSError，由调用方决定兜底。
    """
    global _settings_cache
    text = SETTINGS_PATH.read_text(encoding="utf-8")
    lines = text.splitlines()
    for section, values in settings.items():
        header = f"[{section}]"
        start = next((i for i, ln in enumerate(lines) if ln.strip() == header), None)
        if start is None:
            # section 不存在：追加到文件末尾
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(header)
            start = len(lines) - 1
        end = next((i for i in range(start + 1, len(lines))
                    if re.match(r'^\s*\[[^\[\]]+\]\s*$', lines[i])), len(lines))
        for key, value in values.items():
            new_line = _format_value(key, value)
            idx = next((i for i in range(start + 1, end)
                        if re.match(rf'^\s*{re.escape(key)}\s*=', lines[i])), None)
            if idx is not None:
                lines[idx] = new_line
            else:
                lines.insert(end, new_line)
                end += 1
    SETTINGS_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _settings_cache = None
    logger.info("配置已保存: %s", {k: list(v.keys()) for k, v in settings.items()})
    return True


def _format_value(key: str, value) -> str:
    """键值格式化（bool/int/float/字符串）。"""
    if isinstance(value, bool):
        return f'{key} = {"true" if value else "false"}'
    if isinstance(value, (int, float)):
        return f'{key} = {value}'
    return f'{key} = "{value}"'


_LABEL_LAMBDA_DEFAULT = 1.0
"""λ 默认值 = 生产标定值（非文档的 1.5~2.0 无证据外推，见票 09 Comments）。"""


def get_label_lambda() -> float:
    """标签 λ（风险厌恶系数）：settings.toml [label].lambda，缺省 1.0。

    票 09：从 domain 常量迁移到配置（标定报告要求 λ 可调、初值有据）。
    """
    return float(load_settings().get("label", {}).get("lambda", _LABEL_LAMBDA_DEFAULT))

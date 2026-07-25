import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path("data/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "system.log"

_formatter = logging.Formatter(
    "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def _get_log_level(key: str, default: str) -> int:
    try:
        import tomllib
        with open("config/settings.toml", "rb") as f:
            cfg = tomllib.load(f)
        level = cfg.get("logging", {}).get(key, default)
        return getattr(logging, level.upper(), logging.INFO)
    except Exception:
        return getattr(logging, default.upper(), logging.INFO)


_file_level = _get_log_level("file_level", "INFO")
_console_level = _get_log_level("console_level", "INFO")

_handler = RotatingFileHandler(
    str(LOG_FILE), maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
)
_handler.setFormatter(_formatter)
_handler.setLevel(_file_level)

_console = logging.StreamHandler()
_console.setFormatter(_formatter)
_console.setLevel(_console_level)

root = logging.getLogger()
root.handlers.clear()
root.addHandler(_handler)
root.addHandler(_console)
root.setLevel(min(_file_level, _console_level))

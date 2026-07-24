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

_handler = RotatingFileHandler(
    str(LOG_FILE), maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
)
_handler.setFormatter(_formatter)
_handler.setLevel(logging.DEBUG)

_console = logging.StreamHandler()
_console.setFormatter(_formatter)
_console.setLevel(logging.INFO)

root = logging.getLogger()
root.handlers.clear()
root.addHandler(_handler)
root.addHandler(_console)
root.setLevel(logging.DEBUG)

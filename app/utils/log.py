import json
import logging
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime

LOG_DIR = Path("data/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "system.log"

_correlation_local = threading.local()


def set_correlation_id(cid: str) -> None:
    _correlation_local.cid = cid


def get_correlation_id() -> str:
    return getattr(_correlation_local, "cid", "")


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        obj = {
            "timestamp": datetime.fromtimestamp(record.created).astimezone().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", record.msg.split(":")[0] if ": " in record.msg else record.msg),
            "message": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", get_correlation_id()),
        }
        extra_keys = getattr(record, "extra", None)
        if extra_keys:
            obj.update(extra_keys)
        if record.exc_info and record.exc_info[0]:
            obj["exception"] = self.formatException(record.exc_info)
        return json.dumps(obj, ensure_ascii=False)


_text_fmt = logging.Formatter(
    "%(asctime)s [%(levelname)-7s] [%(correlation_id)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


class ContextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        cid = getattr(record, "correlation_id", get_correlation_id())
        record.correlation_id = cid or "-"
        return _text_fmt.format(record)


class StructLogger(logging.LoggerAdapter):
    def log(self, level, msg, *args, event="", extra=None, exc_info=None, **kwargs):
        log_extra = dict(self.extra)
        if extra:
            log_extra.update(extra)
        if event:
            log_extra["event"] = event
        cid = get_correlation_id()
        if cid:
            log_extra["correlation_id"] = cid
        kwargs["extra"] = log_extra
        if exc_info is not None:
            kwargs["exc_info"] = exc_info
        if self.isEnabledFor(level):
            self.logger._log(level, msg, args, **kwargs)

    def info_event(self, event: str, msg: str, extra: dict | None = None, exc_info=None):
        self.log(logging.INFO, msg, event=event, extra=extra, exc_info=exc_info)

    def warn_event(self, event: str, msg: str, extra: dict | None = None, exc_info=None):
        self.log(logging.WARNING, msg, event=event, extra=extra, exc_info=exc_info)

    def error_event(self, event: str, msg: str, extra: dict | None = None, exc_info=None):
        self.log(logging.ERROR, msg, event=event, extra=extra, exc_info=exc_info)


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

_json_handler = RotatingFileHandler(
    str(LOG_FILE), maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
)
_json_handler.setFormatter(JSONFormatter())
_json_handler.setLevel(_file_level)

_console = logging.StreamHandler()
_console.setFormatter(ContextFormatter())
_console.setLevel(_console_level)

logging.basicConfig(
    handlers=[_console],
    level=min(_file_level, _console_level),
    force=True,
)
root = logging.getLogger()
root.addHandler(_json_handler)


def get_logger(name: str) -> StructLogger:
    return StructLogger(logging.getLogger(name), {})

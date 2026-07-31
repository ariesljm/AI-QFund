import json
import logging
import queue
import sqlite3
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime

LOG_DIR = Path("data/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "system.log"

# SQLite 日志表：供 web 日志查看器查询（所有级别入库，不受 file_level 限制）。
_SYSTEM_LOG_DB_PATH = Path("data/qfund.db")
_SYSTEM_LOG_QUEUE: "queue.Queue" = queue.Queue(maxsize=5000)
_SYSTEM_LOG_MAX_ROWS = 100_000

SYSTEM_LOG_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS system_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    logger TEXT,
    event TEXT,
    message TEXT,
    correlation_id TEXT,
    created_at TEXT DEFAULT (datetime('now'))
)
"""

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


_file_level = _get_log_level("file_level", "DEBUG")
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
    level=logging.DEBUG,
    force=True,
)
root = logging.getLogger()
root.addHandler(_json_handler)


class SQLiteLogHandler(logging.Handler):
    """把日志异步批量写入 SQLite，供 web 日志查看器查询。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            _SYSTEM_LOG_QUEUE.put_nowait(record)
        except queue.Full:
            pass  # 队列满时丢弃，避免阻塞业务线程


def _record_to_log_row(record: logging.LogRecord) -> tuple:
    msg = record.getMessage()
    event = getattr(record, "event", msg.split(":")[0] if ": " in msg else msg)
    cid = getattr(record, "correlation_id", None) or get_correlation_id() or "-"
    return (
        datetime.fromtimestamp(record.created).astimezone().isoformat(),
        record.levelname,
        record.name,
        event,
        msg,
        cid,
    )


def _prune_system_logs(conn: sqlite3.Connection) -> None:
    """超出保留上限时删除最旧的日志行，只保留最新的 N 条。"""
    try:
        total = conn.execute("SELECT COUNT(*) FROM system_logs").fetchone()[0]
        if total <= _SYSTEM_LOG_MAX_ROWS:
            return
        cutoff = conn.execute(
            "SELECT id FROM system_logs ORDER BY id DESC LIMIT 1 OFFSET ?",
            (_SYSTEM_LOG_MAX_ROWS - 1,),
        ).fetchone()
        if cutoff:
            conn.execute("DELETE FROM system_logs WHERE id <= ?", (cutoff[0],))
            conn.commit()
    except Exception:
        pass


def _backfill_system_logs(conn: sqlite3.Connection) -> None:
    """一次性把既有 system.log 文件里的旧日志导入 system_logs（仅空表时执行）。"""
    try:
        if conn.execute("SELECT COUNT(*) FROM system_logs").fetchone()[0]:
            return
        if not LOG_FILE.exists():
            return
        rows = []
        with open(str(LOG_FILE), "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    j = json.loads(line)
                except Exception:
                    continue
                rows.append((
                    j.get("timestamp", ""),
                    j.get("level", "INFO"),
                    j.get("logger", ""),
                    j.get("event", ""),
                    j.get("message", ""),
                    j.get("correlation_id", "-"),
                ))
        if rows:
            conn.executemany(
                "INSERT INTO system_logs (ts, level, logger, event, message, correlation_id) "
                "VALUES (?,?,?,?,?,?)",
                rows,
            )
            conn.commit()
    except Exception:
        pass


def _init_system_log_table() -> None:
    """模块导入时建表并回填旧文件日志；失败不影响程序启动。"""
    try:
        conn = sqlite3.connect(str(_SYSTEM_LOG_DB_PATH))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(SYSTEM_LOG_TABLE_SQL)
        conn.commit()
        _backfill_system_logs(conn)
        conn.close()
    except Exception:
        pass


def _system_log_writer() -> None:
    """后台线程：批量把日志写入 SQLite，降低单条写入开销。"""
    conn = None
    counter = 0
    last_prune = 0
    while True:
        batch = []
        try:
            batch.append(_SYSTEM_LOG_QUEUE.get(timeout=0.5))
            while len(batch) < 200:
                try:
                    batch.append(_SYSTEM_LOG_QUEUE.get_nowait())
                except queue.Empty:
                    break
        except queue.Empty:
            continue
        try:
            if conn is None:
                conn = sqlite3.connect(str(_SYSTEM_LOG_DB_PATH))
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(SYSTEM_LOG_TABLE_SQL)
            conn.executemany(
                "INSERT INTO system_logs (ts, level, logger, event, message, correlation_id) "
                "VALUES (?,?,?,?,?,?)",
                [_record_to_log_row(r) for r in batch],
            )
            conn.commit()
            counter += len(batch)
            if counter - last_prune >= 500:
                _prune_system_logs(conn)
                last_prune = counter
        except Exception:
            # DB 写入失败时回退 stderr，避免日志静默丢失
            for r in batch:
                try:
                    print(r.getMessage(), file=sys.stderr)
                except Exception:
                    pass
            try:
                if conn is not None:
                    conn.rollback()
            except Exception:
                pass


_db_handler = SQLiteLogHandler()
_db_handler.setLevel(logging.DEBUG)
root.addHandler(_db_handler)
root.setLevel(logging.DEBUG)  # 根级别放行所有级别，由各 handler 自行过滤

threading.Thread(target=_system_log_writer, name="system-log-db", daemon=True).start()

_init_system_log_table()


def get_logger(name: str) -> StructLogger:
    return StructLogger(logging.getLogger(name), {})

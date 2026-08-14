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

# system_logs 滚动保留默认值（可被 config/settings.toml [logging] 段覆盖）
_DB_RETENTION_DAYS = 30
_DB_MAX_ROWS = 20_000

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

class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        obj = {
            "timestamp": datetime.fromtimestamp(record.created).astimezone().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", record.msg.split(":")[0] if ": " in record.msg else record.msg),
            "message": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", ""),
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
        cid = getattr(record, "correlation_id", "")
        record.correlation_id = cid or "-"
        return _text_fmt.format(record)


class StructLogger(logging.LoggerAdapter):
    def __init__(self, logger: logging.Logger, extra: dict | None = None, cid: str = ""):
        super().__init__(logger, extra or {})
        # 显式 correlation id：随 adapter 绑定传递，不依赖模块级全局状态
        self.cid = cid

    def with_cid(self, cid: str) -> "StructLogger":
        """返回绑定新 cid 的副本（不可变：原 adapter 不受影响）。"""
        return StructLogger(self.logger, dict(self.extra), cid=cid)

    def log(self, level: int, msg: str, *args: object, event: str = "",
            extra: dict | None = None, exc_info=None,
            **kwargs: object) -> None:
        log_extra = dict(self.extra)
        if extra:
            log_extra.update(extra)
        if event:
            log_extra["event"] = event
        cid = self.cid
        if cid:
            log_extra["correlation_id"] = cid
        kwargs["extra"] = log_extra
        if exc_info is not None:
            kwargs["exc_info"] = exc_info
        if self.isEnabledFor(level):
            self.logger._log(level, msg, args, **kwargs)

    def info_event(self, event: str, msg: str, extra: dict | None = None, exc_info=None) -> None:
        self.log(logging.INFO, msg, event=event, extra=extra, exc_info=exc_info)

    def warn_event(self, event: str, msg: str, extra: dict | None = None, exc_info=None) -> None:
        self.log(logging.WARNING, msg, event=event, extra=extra, exc_info=exc_info)

    def error_event(self, event: str, msg: str, extra: dict | None = None, exc_info=None) -> None:
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

# httpx/httpcore 内部事件日志（DEBUG 的响应头/体事件 + INFO 的每请求行）刷屏且无业务价值，
# 用户只需结果日志；屏蔽至 WARNING 以上（网络错误仍可见）。
# 第三方库统一降级：网络/HTTP/LLM 库的 DEBUG/INFO 过程日志不进业务日志流，
# 保留其 WARNING/ERROR（限流、超时、连接失败等真实问题仍可见）。
for _noisy_lib in ("httpx", "httpcore", "urllib3", "openai", "tls_client", "curl_cffi", "asyncio"):
    logging.getLogger(_noisy_lib).setLevel(logging.WARNING)


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
    cid = getattr(record, "correlation_id", "") or "-"
    return (
        datetime.fromtimestamp(record.created).astimezone().isoformat(),
        record.levelname,
        record.name,
        event,
        msg,
        cid,
    )


def _read_retention_config() -> tuple[int, int]:
    """读取 [logging] 段的日志保留配置（天数、最大行数），失败时用默认值。"""
    try:
        import tomllib
        with open("config/settings.toml", "rb") as f:
            cfg = tomllib.load(f)
        log_cfg = cfg.get("logging", {})
        days = int(log_cfg.get("db_retention_days", _DB_RETENTION_DAYS))
        rows = int(log_cfg.get("db_max_rows", _DB_MAX_ROWS))
        return max(days, 1), max(rows, 1)
    except Exception:
        return _DB_RETENTION_DAYS, _DB_MAX_ROWS


def _prune_system_logs(conn: sqlite3.Connection) -> None:
    """滚动保留：先按时间删除超期日志，再按行数上限兜底删最旧（双条件）。"""
    try:
        days, max_rows = _read_retention_config()
        # 时间维度：删除超过保留天数的日志（ts 为 ISO 8601，可被 SQLite datetime() 解析）
        conn.execute(
            "DELETE FROM system_logs WHERE datetime(ts) < datetime('now', ?)",
            (f"-{days} days",),
        )
        # 行数维度：仍超上限则删除最旧行（保留最新 max_rows 条）
        total = conn.execute("SELECT COUNT(*) FROM system_logs").fetchone()[0]
        if total > max_rows:
            cutoff = conn.execute(
                "SELECT id FROM system_logs ORDER BY id DESC LIMIT 1 OFFSET ?",
                (max_rows,),
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
        # 一次性清理历史垃圾：入库级别改为 INFO+ 前残留的 httpx/httpcore 网络事件与 DEBUG 过程日志，
        # 用户无需查看且无排查价值；后续入库只存 INFO+ 业务结果日志。
        try:
            conn.execute(
                "DELETE FROM system_logs WHERE level = 'DEBUG' "
                "OR logger = 'httpx' OR logger = 'httpcore' OR logger LIKE 'httpcore.%'"
            )
            conn.commit()
        except Exception:
            pass
        _backfill_system_logs(conn)
        # 启动时清理一次，处理停机期间攒下的超期日志
        _prune_system_logs(conn)
        conn.close()
    except Exception:
        pass


def _system_log_writer() -> None:
    """后台线程：批量把日志写入 SQLite，降低单条写入开销。"""
    conn = None
    counter = 0
    last_prune = 0
    connected_path: Path | None = None  # 当前连接对应的日志库路径
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
            # 路径变化时重连（测试隔离场景：pytest conftest 重定向日志库路径）
            if conn is None or connected_path != _SYSTEM_LOG_DB_PATH:
                if conn is not None:
                    conn.close()
                conn = sqlite3.connect(str(_SYSTEM_LOG_DB_PATH))
                connected_path = _SYSTEM_LOG_DB_PATH
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
# 入库只存 INFO+（业务结果/警告/错误）：DEBUG 属开发调试过程（网络事件、轮询、中间变量），
# 仅写入 system.log 文件供排查，不进入 web 日志查看器（用户只需结果日志）。
_db_handler.setLevel(logging.INFO)
root.addHandler(_db_handler)
root.setLevel(logging.DEBUG)  # 根级别放行所有级别，由各 handler 自行过滤

threading.Thread(target=_system_log_writer, name="system-log-db", daemon=True).start()

_init_system_log_table()


def get_logger(name: str, cid: str = "") -> StructLogger:
    return StructLogger(logging.getLogger(name), {}, cid=cid)

"""pytest 全局 fixture：隔离 SQLite 日志写入，防止测试日志污染生产库 system_logs。

背景：app/utils/log.py 的 system_logs 写入路径（_SYSTEM_LOG_DB_PATH）是独立于
app.database.DB_PATH 的相对路径，测试即使 monkeypatch 了业务库，日志仍会写进
生产库 data/qfund.db（历史 14 万行日志中绝大多数是测试输出）。

本 fixture 在 session 开始前把日志库重定向到临时目录；writer 后台线程每次批量
写入前比较连接路径，发现变化会自动重连（见 app/utils/log.py _system_log_writer）。
"""

import sqlite3
import sys
from pathlib import Path

import pytest

# 与 tests/ 各测试文件相同的路径样板：pytest 入口不把项目根加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.utils import log as log_mod
import app.database as db_mod


@pytest.fixture(scope="session", autouse=True)
def _isolate_business_db(tmp_path_factory):
    """隔离业务库：防止未 mock DB_PATH 的路径（如 run_evolve 写 meta/insights）
    污染生产 data/qfund.db。与 _isolate_system_logs 同级；各测试文件内
    monkeypatch DB_PATH 的仍局部覆盖本值（monkeypatch 测试内生效、结束还原）。
    """
    db_path = tmp_path_factory.mktemp("qfund") / "qfund.db"
    original = db_mod.DB_PATH
    db_mod.DB_PATH = db_path
    try:
        yield
    finally:
        db_mod.DB_PATH = original


@pytest.fixture(scope="session", autouse=True)
def _isolate_system_logs(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("syslogs") / "system_logs.db"
    original = log_mod._SYSTEM_LOG_DB_PATH
    log_mod._SYSTEM_LOG_DB_PATH = db_path
    try:
        conn = sqlite3.connect(str(db_path))
        conn.execute(log_mod.SYSTEM_LOG_TABLE_SQL)
        conn.commit()
        conn.close()
    except Exception:
        pass
    yield
    log_mod._SYSTEM_LOG_DB_PATH = original

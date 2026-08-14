"""复现 database is locked：长写事务（模拟管线批量写）+ 并发连接（web/调度器轮询）。

修复前预期：worker 线程出现 sqlite3.OperationalError: database is locked
修复后预期：0 错误（busy_timeout 提升 + 迁移不再无条件写）
"""
import sqlite3
import threading
import time
from pathlib import Path

from app.database import db_conn, DB_PATH
from app.repo.decision import get_latest_reco_id

ERRORS: list[str] = []
N_WORKERS = 6
HOLD_SECONDS = 8  # 长事务持有时长，超过 sqlite 默认 busy_timeout 5s
META_KEY = "__lock_repro__"


def holder() -> None:
    """模拟管线批量写：持写锁 HOLD_SECONDS 秒。"""
    conn = sqlite3.connect(str(DB_PATH), timeout=1)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (META_KEY, "1"))
        time.sleep(HOLD_SECONDS)
        conn.commit()
    finally:
        conn.close()


def worker(tid: int) -> None:
    try:
        for i in range(3):
            get_latest_reco_id()  # 触发 _migrate 的 UPDATE + commit（写）
            with db_conn() as conn:
                conn.execute("SELECT COUNT(*) FROM meta").fetchone()
    except sqlite3.OperationalError as e:
        ERRORS.append(f"thread{tid}: {e}")
    except Exception as e:  # noqa: BLE001
        ERRORS.append(f"thread{tid}: 其他异常 {type(e).__name__}: {e}")


def main() -> None:
    # 清理上次残留的测试键
    with db_conn() as conn:
        conn.execute(f"DELETE FROM meta WHERE key = '{META_KEY}'")
    h = threading.Thread(target=holder)
    h.start()
    time.sleep(0.3)  # 确保长事务已持有写锁
    threads = [threading.Thread(target=worker, args=(t,)) for t in range(N_WORKERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    h.join()
    with db_conn() as conn:
        conn.execute(f"DELETE FROM meta WHERE key = '{META_KEY}'")
    if ERRORS:
        print(f"复现成功：{len(ERRORS)} 个 database is locked 错误（首条：{ERRORS[0]}）")
    else:
        print(f"无错误：{N_WORKERS * 3} 次并发连接在长事务期间全部成功")


if __name__ == "__main__":
    main()

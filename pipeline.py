"""管线编排模块：数据基座 → 推荐 → 监控 → 进化(每月1号)"""

import logging
import time
from datetime import datetime

from data_foundation import run_pipeline as run_data_foundation
from data_store import _db_conn
from monitor import run_monitor
from recommend import run_recommendation

logger = logging.getLogger("pipeline")

_HOLDINGS_INTERVAL_DAYS = 7


def _daily_data_steps() -> list[int]:
    with _db_conn() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        row = conn.execute("SELECT value FROM meta WHERE key = 'holdings_last_run'").fetchone()
    if row:
        last = datetime.strptime(row[0], "%Y-%m-%d").date()
        elapsed = (datetime.now().date() - last).days
    else:
        elapsed = _HOLDINGS_INTERVAL_DAYS + 1
    if elapsed > _HOLDINGS_INTERVAL_DAYS:
        with _db_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('holdings_last_run', ?)",
                (datetime.now().strftime("%Y-%m-%d"),),
            )
        return [1, 2, 3, 4, 7]
    return [1, 2, 3, 7]


def run(force: bool = False) -> None:
    def _data_phase():
        run_data_foundation(steps=_daily_data_steps())

    phases = [
        ("数据基座", _data_phase),
        ("推荐引擎", lambda: run_recommendation(force=force)),
        ("监控引擎", lambda: run_monitor()),
    ]
    if datetime.now().day == 1:
        from evolve import run_evolve
        phases.append(("进化引擎", lambda: run_evolve()))

    pipeline_start = time.time()
    for name, fn in phases:
        phase_start = time.time()
        logger.info("[启动] %s开始执行", name)
        try:
            fn()
            phase_ms = (time.time() - phase_start) * 1000
            logger.info("[完成] %s执行完毕 (%.0fms)", name, phase_ms)
        except Exception as e:
            phase_ms = (time.time() - phase_start) * 1000
            logger.error("[错误] %s执行失败 (%.0fms): %s", name, phase_ms, e)
            raise
    total_ms = (time.time() - pipeline_start) * 1000
    logger.info("管线全流程完成 (%.0fms)", total_ms)

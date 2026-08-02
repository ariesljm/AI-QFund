"""管线编排模块：数据基座 → 推荐 → 监控 → 进化(每月1号)"""

import time
import uuid
from datetime import datetime

from app.data.foundation import run_pipeline as run_data_foundation
from app.database import db_conn
from app.utils.log import set_correlation_id, get_logger
from app.engine.monitor import run_monitor
from app.engine.recommend import run_recommendation

logger = get_logger("pipeline")

_HOLDINGS_INTERVAL_DAYS = 7


def _daily_data_steps() -> list[int]:
    with db_conn() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        row = conn.execute("SELECT value FROM meta WHERE key = 'holdings_last_run'").fetchone()
    if row:
        last = datetime.strptime(row[0], "%Y-%m-%d").date()
        elapsed = (datetime.now().date() - last).days
    else:
        elapsed = _HOLDINGS_INTERVAL_DAYS + 1
    if elapsed > _HOLDINGS_INTERVAL_DAYS:
        with db_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('holdings_last_run', ?)",
                (datetime.now().strftime("%Y-%m-%d"),),
            )
        return [1, 2, 3, 4, 7]
    return [1, 2, 3, 7]


def run(force: bool = False, today: datetime | None = None) -> None:
    """管线全流程编排；today 可注入（测试用），缺省取当前日期。"""
    today = today or datetime.now()
    cid = uuid.uuid4().hex[:12]
    set_correlation_id(cid)
    logger.info_event("pipeline_start", "管线启动", extra={"correlation_id": cid, "force": force})

    def _data_phase():
        run_data_foundation(steps=_daily_data_steps())

    phases = [
        ("数据基座", _data_phase),
        ("推荐引擎", lambda: run_recommendation(force=force)),
        ("监控引擎", lambda: run_monitor()),
    ]
    if today.day == 1:
        from app.engine.evolve import run_evolve
        phases.append(("进化引擎", lambda: run_evolve()))

    pipeline_start = time.time()
    for name, fn in phases:
        phase_start = time.time()
        logger.info_event("phase_start", f"{name}开始执行", extra={"phase": name})
        try:
            fn()
            phase_ms = (time.time() - phase_start) * 1000
            logger.info_event("phase_end", f"{name}执行完毕",
                              extra={"phase": name, "duration_ms": int(phase_ms)})
        except Exception as e:
            phase_ms = (time.time() - phase_start) * 1000
            logger.error_event("phase_failed", f"{name}执行失败",
                               extra={"phase": name, "duration_ms": int(phase_ms), "error": str(e)})
            raise
    total_ms = (time.time() - pipeline_start) * 1000
    logger.info_event("pipeline_end", "管线全流程完成",
                      extra={"duration_ms": int(total_ms)})

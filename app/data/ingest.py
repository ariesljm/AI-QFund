"""异步批量下载 harness：并发批次 → 熔断 → 失败/恢复记录 → 多轮补查。

三处异步下载（净值增量/净值全量/持仓）此前各自内联同一套 ~120 行骨架，
失败-冷却-补查状态机散落在调用处。此模块把骨架收敛为一个 deep module，
各下载入口只需提供 fetch 与存储回调。
"""

import asyncio
import time

from app.database import db_conn
from app.data.store import record_failure, mark_recovered_batch, run_backfill_rounds
from app.utils.log import get_logger

logger = get_logger("data_ingest")

# 熔断阈值：批次失败率超过该比例，疑似接口故障，提前中止避免白耗请求
CIRCUIT_BREAK_FAIL_RATE = 0.5


async def run_batched_fetch(
    session,
    *,
    fetch_type: str,
    label: str,
    targets: list,
    batch_size: int,
    fetch_one,
    handle_batch,
    backfill_one=None,
    conn=None,
    no_update_note: str = "接口确认无新数据",
    primary_note: str = "拉取失败",
) -> dict:
    """并发批次下载通用骨架：semaphore 并发 → 熔断 → 失败/恢复记录 → 多轮补查。

    - ``fetch_one(session, item)``：async 单目标拉取，返回 ``(item, payload, failed)``。
    - ``handle_batch(conn, results)``：把一批结果写入库，返回
      ``{"new_count": int, "success": set, "no_update": list, "failed": list}``。
    - ``backfill_one(item)``：同步补查单目标（可省略）；补查异常由 run_backfill_rounds 记录。
    - ``conn``：复用调用方的共享连接（持仓等需要连接内状态可见的路径）；
      缺省时每批次自开连接。
    - 返回汇总 {"new_count", "total", "success", "no_update", "failed"}。
    """
    all_failed: list = []
    no_update: list = []
    success: set = set()
    new_count = 0
    done = 0
    start_time = time.monotonic()

    for i in range(0, len(targets), batch_size):
        batch = targets[i: i + batch_size]
        results = await asyncio.gather(*(fetch_one(session, item) for item in batch))

        if conn is not None:
            outcome = handle_batch(conn, results)
        else:
            with db_conn() as conn_:
                outcome = handle_batch(conn_, results)
        batch_failed = len(outcome["failed"])
        new_count += outcome["new_count"]
        success |= outcome["success"]
        no_update.extend(outcome["no_update"])
        all_failed.extend(outcome["failed"])

        # 熔断：批次失败率异常高，疑似接口故障，提前中止避免白耗请求
        if batch_failed / len(batch) > CIRCUIT_BREAK_FAIL_RATE:
            logger.error(
                "%s批次失败率 %.0f%%（%d/%d）超过 50%%，疑似接口故障，提前中止",
                label, batch_failed / len(batch) * 100, batch_failed, len(batch),
            )
            break

        done += len(batch)
        elapsed = time.monotonic() - start_time
        speed = done / elapsed if elapsed > 0 else 0
        if outcome["new_count"]:
            logger.info("%s批次写入 %d 条 (进度 %d/%d, %.1f/s)",
                        label, outcome["new_count"], done, len(targets), speed)

    # 本次成功的目标：清除失败记录，避免冷却逻辑误判为仍在失败
    if success:
        mark_recovered_batch(fetch_type, sorted(success))

    # 确认无新数据的目标：记录失败（累计冷却次数），不计入熔断失败率、不触发补查
    if no_update:
        for item in no_update:
            record_failure(fetch_type, item, no_update_note, stage="no_update")
        logger.info("%s：%d 个目标确认无新数据，已累计失败次数（满 3 次进入冷却）",
                    label, len(no_update))

    # 失败目标：记录并多轮补查
    if all_failed:
        for item in all_failed:
            record_failure(fetch_type, item, primary_note, stage="primary")
        logger.info("%s失败 %d 个目标，开始补查", label, len(all_failed))
        if backfill_one is not None:
            run_backfill_rounds(fetch_type, all_failed, backfill_one,
                                len(targets), label=label, rounds=2, delay=30)

    return {"new_count": new_count, "total": len(targets),
            "success": success, "no_update": no_update, "failed": all_failed}

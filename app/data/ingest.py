"""异步批量下载 harness：并发批次 → 熔断 → 失败/恢复记录 → 多轮补查。

三处异步下载（净值增量/净值全量/持仓）此前各自内联同一套 ~120 行骨架，
失败-冷却-补查状态机散落在调用处。此模块把骨架收敛为一个 deep module，
各下载入口只需提供 fetch 与存储回调。
"""

import asyncio
import time
from typing import TypedDict

from app.data.store import (
    STAGE_NO_UPDATE,
    STAGE_PRIMARY,
    cooldown_targets,
    list_failures,
    mark_recovered_batch,
    record_failure,
    run_backfill_rounds,
)
from app.database import db_conn
from app.utils.log import get_logger

logger = get_logger("data_ingest")

# 熔断阈值：批次失败率超过该比例，疑似接口故障，提前中止避免白耗请求
CIRCUIT_BREAK_FAIL_RATE = 0.5

# 系统性"无新数据"护栏最小目标数：大批量下载中确认无新数据的占比超过熔断阈值时，
# 疑似接口批量异常（如反爬返回空响应被解析为无数据），改按拉取失败处理，
# 避免健康基金被误判停更、误入长冷却造成静默断档。小批量（补查、测试）不适用。
NO_UPDATE_GUARD_MIN_TARGETS = 100


def is_systematic_no_update(no_update_count: int, targets_count: int,
                            no_update_guard: bool = True) -> bool:
    """大批量下"确认无新数据"占比超熔断阈值 → 疑似接口批量异常（系统性假空）。

    run_batched_fetch 与 nav 批量分支（fundmobapi）共用同一语义；样本不足
    NO_UPDATE_GUARD_MIN_TARGETS 或 no_update_guard=False（高占比"无数据"属
    常态的下载，如持仓：ETF联接/商品基金本就无重仓披露）时不判系统性。
    """
    return (no_update_guard and targets_count >= NO_UPDATE_GUARD_MIN_TARGETS
            and no_update_count / targets_count > CIRCUIT_BREAK_FAIL_RATE)


class FetchOutcome(TypedDict):
    """handle_batch 结果契约（净值增量/全量/持仓共用）：

    - new_count: 新增行数
    - success: 本次有新数据写入的目标集合（主循环据此 mark_recovered）
    - no_update: 接口确认无新数据的目标（累计进冷却）
    - failed: 拉取失败的目标（记录 + 补查）
    """

    new_count: int
    success: set[str]
    no_update: list[str]
    failed: list[str]


def filter_cooldown_targets(fetch_type: str, targets: list, label: str,
                            stage_cooldown_days: dict[str, int] | None = None) -> list:
    """下载入口 preflight：记录待重试失败、过滤冷却目标（nav 增量/全量/持仓/行业映射共用样板）。

    - 日志打印待重试失败记录数（观察用）
    - 过滤掉连续失败进入冷却期的目标，避免对注定失败的基金/股票反复请求
    - stage_cooldown_days：按失败类型（stage）覆盖冷却期，透传给 cooldown_targets
    - 返回过滤后的目标列表（不修改入参）
    """
    pending = list_failures(fetch_type, status="failed")
    if pending:
        logger.info("%s：存在 %d 条待重试失败记录", label, len(pending))
    cooldown = cooldown_targets(fetch_type, stage_cooldown_days=stage_cooldown_days)
    if cooldown:
        logger.info("%s：%d 个目标连续失败进入冷却期，本次跳过", label, len(cooldown))
        return [t for t in targets if t not in cooldown]
    return targets


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
    no_update_guard: bool = True,
    primary_note: str = "拉取失败",
) -> FetchOutcome:
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

    # 确认无新数据的目标：默认记录失败（累计冷却次数），不计入熔断失败率、不触发补查；
    # 大批量下无新数据占比超熔断阈值视为系统性接口异常，改按拉取失败处理并进入补查
    #（判定语义见 is_systematic_no_update）。
    if no_update:
        if is_systematic_no_update(len(no_update), len(targets), no_update_guard):
            logger.error(
                "%s：%d/%d 个目标确认无新数据，占比超 %.0f%%——疑似接口批量异常"
                "（如反爬返回空响应），改按拉取失败处理",
                label, len(no_update), len(targets), CIRCUIT_BREAK_FAIL_RATE * 100,
            )
            all_failed.extend(no_update)
        else:
            for item in no_update:
                record_failure(fetch_type, item, no_update_note, stage=STAGE_NO_UPDATE)
            logger.info("%s：%d 个目标确认无新数据，已累计失败次数（满 3 次进入冷却）",
                        label, len(no_update))

    # 失败目标：记录并多轮补查
    if all_failed:
        for item in all_failed:
            record_failure(fetch_type, item, primary_note, stage=STAGE_PRIMARY)
        logger.info("%s失败 %d 个目标，开始补查", label, len(all_failed))
        if backfill_one is not None:
            run_backfill_rounds(fetch_type, all_failed, backfill_one,
                                len(targets), label=label, rounds=2, delay=30)

    return {"new_count": new_count, "total": len(targets),
            "success": success, "no_update": no_update, "failed": all_failed}

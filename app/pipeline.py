"""管线编排（2.0 精简，票 22）：数据基座 → 推荐（recommend_top5）→ 2.0 监控（supervise）。

1.x 推荐/门控自愈/风格反推/监控/进化槽位已随 22 删除——推荐自带健康门
（screen empty 态 data_failure/no_opportunity），监控按推荐对象跟踪。
各槽位互不阻断：推荐失败（LLM 失败/模型缺失）不中断监控盯盘。
"""

import uuid
from collections.abc import Callable
from datetime import datetime

from app.data.foundation import daily_steps
from app.data.foundation import run_pipeline as run_data_foundation
from app.utils.log import get_logger

logger = get_logger("pipeline")


def _new_cid() -> str:
    return uuid.uuid4().hex[:12]


def _run_phases(phases: list[tuple[str, Callable[[], None]]], label: str, cid: str) -> None:
    """通用 phase 编排（全流程 / 数据槽位 / 推荐槽位共用）。"""
    for name, fn in phases:
        _run_phase_safely(name, fn, cid)


def _run_phase_safely(name: str, fn: Callable[[], None], cid: str) -> None:
    """单 phase 容错执行：失败仅记录，不中断后续槽位。"""
    try:
        logger.with_cid(cid).info_event("phase_start", f"{name}开始执行")
        fn()
        logger.with_cid(cid).info_event("phase_done", f"{name}执行完毕")
    except Exception as e:
        logger.with_cid(cid).error_event("phase_failed_continue", f"{name}执行失败，后续槽位继续: {e}",
                                         extra={"phase": name, "error": str(e)}, exc_info=True)


def _run_slot(label: str, today: datetime | None = None) -> tuple[datetime, str]:
    """槽位入口脚手架：日期与 cid 生成 + 启动日志（三个入口共用，cid 随 adapter 绑定）。"""
    today = today or datetime.now()
    cid = _new_cid()
    logger.with_cid(cid).info_event("pipeline_start", f"{label}启动")
    return today, cid


def _run_recommend_safely(cid: str, today: str) -> None:
    """推荐槽位（2.0 票 11/13）：筛选 → LLM 审计 → 剪枝 → Top5 落库。

    screen 自带健康门（候选池空 → no_opportunity；特征缺失 → data_failure），
    空态落 recommend_v2 的空记录由引擎处理，不在这里自审自拦。
    数据新鲜度闸门（审计 P1-2 扩展）：全局净值/指数停更时拦截推荐——
    数据槽位失败不阻断推荐槽位，这里补上"数据没更新就不要推荐"的检查。
    """
    def _run() -> None:
        from app.engine.screen_pipeline import check_data_freshness
        ok, reason = check_data_freshness(today)
        if not ok:
            logger.with_cid(cid).warn_event("recommend_blocked",
                                            f"数据新鲜度闸门拦截推荐：{reason}")
            return
        from app.engine.recommend_v2 import recommend_top5
        recommend_top5(today, cid=cid)
    _run_phase_safely("推荐引擎", _run, cid)


def _run_supervise_safely(cid: str, today: str) -> None:
    """2.0 监控槽位（US22-28）：recommend_v2 对象 → 信号装配 → 状态机转移落库。

    无推荐对象空跑静默返回（监控没有分母，正常）。
    """
    def _run() -> None:
        from app.engine.supervise import run_supervision
        run_supervision(today, cid=cid)
    _run_phase_safely("2.0 监控", _run, cid)


def run(today: datetime | None = None) -> None:
    """全流程（手动触发）：数据基座 → 推荐 → 2.0 监控，各槽位互不阻断。"""
    today, cid = _run_slot("管线", today)
    _run_phase_safely("数据基座", lambda: run_data_foundation(steps=daily_steps()), cid)
    _run_recommend_safely(cid, today.strftime("%Y-%m-%d"))
    _run_supervise_safely(cid, today.strftime("%Y-%m-%d"))


def run_data(today: datetime | None = None) -> None:
    """数据基座槽位（盘前固定时间执行），独立于推荐。"""
    today, cid = _run_slot("数据基座槽位", today)
    _run_phases([
        ("数据基座", lambda: run_data_foundation(steps=daily_steps())),
    ], "数据基座槽位", cid)


def run_recommend(today: datetime | None = None) -> None:
    """推荐槽位（盘中可配时间执行）：推荐 → 2.0 监控，依赖数据槽位产出的特征。"""
    today, cid = _run_slot("推荐槽位", today)
    _run_recommend_safely(cid, today.strftime("%Y-%m-%d"))
    _run_supervise_safely(cid, today.strftime("%Y-%m-%d"))

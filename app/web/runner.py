"""管线运行与调度 module：并发守卫、槽位状态、定时循环。

从 web 渲染层分离：FastAPI 之外可独立测试；页面路由只读 pipeline 状态。
"""

from app.repo import meta_keys as META
import collections
import logging
import threading
import time
from datetime import datetime, timedelta

from app.config import load_settings
from app.pipeline import (run as run_full_pipeline,
                          run_data as run_data_pipeline,
                          run_recommend as run_recommend_pipeline)
from app.utils.trading_calendar import is_trading_day
import app.repo as repo

logger = logging.getLogger("web.runner")


class _PipelineState:
    """管线运行时状态封装，避免模块级可变全局变量。"""

    def __init__(self):
        self.status: dict = {"state": "idle", "message": ""}
        self.logs: collections.deque = collections.deque(maxlen=200)
        self.last_run_date: str | None = None

    def add_log(self, msg: str) -> None:
        now = datetime.now().strftime("%H:%M:%S")
        line = f"{now} [pipeline] {msg}"
        self.logs.append(line)
        print(line, file=__import__("sys").stderr, flush=True)


# 管线运行时状态（供 Web 状态卡 / API 只读消费）
pipeline = _PipelineState()


class _PipelineLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            pipeline.logs.append(self.format(record))
        except Exception:
            pass


# 管线并发守卫：检查+置位临界区加锁，防止手工触发与定时触发同时到达时双跑
pipeline_lock = threading.Lock()


# 调度：单一全流程定时（数据基座→推荐→监控→进化，一次执行）
# T-1 净值已完整公布后跑（建议盘后/盘中），推荐宏观分析用当日完整板块数据。


def slot_last_run(slot: str) -> str | None:
    """槽位最近一次已执行的日期（meta 持久化，按槽位独立去重）。"""
    return repo.get_meta(f"{META.SCHED_LAST_RUN_PREFIX}{slot}")


def mark_slot_run(slot: str, day: str) -> None:
    repo.save_meta(f"{META.SCHED_LAST_RUN_PREFIX}{slot}", day)


def run_pipeline_wrapper(slot: str | None = None) -> None:
    """管线执行入口（调度槽位/手动触发共用）。

    并发守卫：运行中忽略重复触发（修复手工与定时同时到达的双跑竞态）。
    slot 为 None 跑全流程（手动触发），否则跑对应槽位。
    """
    with pipeline_lock:
        if pipeline.status.get("state") == "running":
            pipeline.add_log("[跳过] 管线已在运行中，忽略本次触发")
            return
        pipeline.logs.clear()
        pipeline.status = {"state": "running", "message": "管线启动..."}

    handler = _PipelineLogHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)-7s] %(name)s: %(message)s", datefmt="%H:%M:%S"))
    handler.setLevel(logging.INFO)
    root = logging.getLogger()
    root.addHandler(handler)

    slot_label = {"data": "数据基座", "recommend": "推荐+监控"}.get(slot, "全流程")
    try:
        pipeline.add_log(f"[启动] {slot_label}管线开始执行")
        if slot == "data":
            run_data_pipeline()
        elif slot == "recommend":
            run_recommend_pipeline()
        else:
            run_full_pipeline()
        pipeline.status = {"state": "done", "message": "管线执行完成"}
        pipeline.last_run_date = datetime.now().strftime("%Y-%m-%d")
    except Exception as e:
        pipeline.add_log(f"[错误] 管线执行失败: {e}")
        pipeline.status = {"state": "error", "message": f"管线执行失败: {e}"}
        pipeline.last_run_date = datetime.now().strftime("%Y-%m-%d")
    finally:
        root.removeHandler(handler)
    # 持久化最近执行日期：重启后状态卡仍能显示"已运行"（此前仅存内存，重启即丢失）
    if pipeline.last_run_date:
        mark_slot_run(slot or "manual", pipeline.last_run_date)


def last_run_date() -> str | None:
    """管线最近一次执行的日期：内存优先，meta（各槽位/手动）兜底取最新。"""
    if pipeline.last_run_date:
        return pipeline.last_run_date
    days = [
        d for d in (
            slot_last_run("full"), slot_last_run("recommend"),
            slot_last_run("data"), slot_last_run("manual"),
        ) if d
    ]
    return max(days) if days else None


def next_run_for(slot: str = "full") -> str | None:
    """槽位下次执行时间文本（调度触发与展示页共用单一来源，架构深化 K）。

    口径与 scheduler_loop 一致：hour 与 minute 都非空才视为启用——
    修复：展示页曾只判 hour，仅填小时时显示启用但调度永不触发。
    """
    s = load_settings()
    sched = s.get("scheduler", {}) or {}
    h, m = sched.get("hour", ""), sched.get("minute", "")
    if h == "" or m == "":
        return None
    now = datetime.now()
    run = now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
    if run <= now:
        run = run + timedelta(days=1)
    return f"{run.strftime('%Y-%m-%d %H:%M')}（{slot}）"


def has_run_today(slot: str) -> bool:
    """槽位今日是否已执行（调度去重与展示共用，架构深化 K）。"""
    today = datetime.now().strftime("%Y-%m-%d")
    return slot_last_run(slot) == today


def scheduler_loop() -> None:
    _sched_logger = logging.getLogger("scheduler")

    import os as _os
    enabled = _os.environ.get("ENABLE_SCHEDULER", "true").lower() in ("1", "true", "yes")
    if not enabled:
        _sched_logger.info("调度器已通过 ENABLE_SCHEDULER 环境变量禁用")
        return

    _sched_logger.info("调度器启动，模式: daemon线程（单槽位全流程：数据基座→推荐→监控→进化）")
    while True:
        try:
            s = load_settings()
            sched = s.get("scheduler", {})
            today = datetime.now().strftime("%Y-%m-%d")
            now = datetime.now()
            h, m = sched.get("hour", ""), sched.get("minute", "")
            if h != "" and m != "" and now.hour == int(h) and now.minute == int(m) \
                    and not has_run_today("full"):
                mark_slot_run("full", today)  # 先置位，防同分钟重复触发
                if not is_trading_day(now.date()):
                    _sched_logger.info("定时跳过：%s 非交易日，不启动全流程", today)
                else:
                    _sched_logger.info("定时触发: %s %02d:%02d（全流程）", today, int(h), int(m))
                    run_pipeline_wrapper()  # slot=None → 数据基座→推荐→监控→进化
        except Exception as e:
            _sched_logger.error("调度器异常: %s", e, exc_info=True)
        time.sleep(60)


def is_trading_time(now: datetime | None = None) -> bool:
    """A股交易时段：交易日（akshare 全年交易日历，自动涵盖节假日与调休）内的 9:30-11:30、13:00-15:00。"""
    now = now or datetime.now()
    if not is_trading_day(now.date()):
        return False
    hm = now.hour * 60 + now.minute
    return (9 * 60 + 30 <= hm <= 11 * 60 + 30) or (13 * 60 <= hm <= 15 * 60)

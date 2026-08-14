"""管线编排模块：数据基座槽位 → 推荐/监控槽位（互不阻断）→ 进化（每日结算度量 + 月度重量活）。

监控与推荐解耦：推荐失败（LLM 失败/模型缺失）不再中断监控盯盘，持仓信号链保持连续；
进化每天附加（内部按间隔控制元分析/GA/衰减等重量活），保证推荐满 20 日窗口即被结算度量。
"""

import time
import uuid
from datetime import datetime

from app.data.foundation import run_pipeline as run_data_foundation
from app.data.foundation import update_industry_map, daily_steps
from app.utils.log import get_logger
from app.engine.monitor import run_monitor
from app.engine.recommend import run_recommendation
from app.repo import meta_keys as META
import app.repo as repo

logger = get_logger("pipeline")

# 推荐门控自愈冷却（天）：自愈失败后 24 小时内不再重试，避免限流中的接口被反复白耗
_HEAL_COOLDOWN_DAYS = 1


def _new_cid() -> str:
    return uuid.uuid4().hex[:12]


def _run_phases(phases: list[tuple[str, callable]], label: str, cid: str) -> None:
    """通用 phase 编排（全流程 / 数据槽位 / 推荐槽位共用）。"""
    log = logger.with_cid(cid)
    pipeline_start = time.time()
    for name, fn in phases:
        phase_start = time.time()
        log.info_event("phase_start", f"{name}开始执行", extra={"phase": name})
        try:
            fn()
            phase_ms = (time.time() - phase_start) * 1000
            log.info_event("phase_end", f"{name}执行完毕",
                           extra={"phase": name, "duration_ms": int(phase_ms)})
        except Exception as e:
            phase_ms = (time.time() - phase_start) * 1000
            log.error_event("phase_failed", f"{name}执行失败: {e}",
                            extra={"phase": name, "duration_ms": int(phase_ms), "error": str(e)},
                            exc_info=True)
            raise
    total_ms = (time.time() - pipeline_start) * 1000
    log.info_event("pipeline_end", f"{label}完成", extra={"duration_ms": int(total_ms)})


def _evolve_phase(today: datetime) -> list[tuple[str, callable]]:
    """进化引擎 phase：每日附加（延迟 import 避免循环依赖）。

    内部按 28 天间隔控制重量活（元分析/GA/衰减），每日仅执行幂等的结算与质量度量——
    修复时间窗错位：推荐满 20 日窗口（约 21 条净值）后次日即被结算，不再等月 1 号巧合满窗。
    """
    from app.engine.evolve import run_evolve
    return [("进化引擎", lambda: run_evolve())]


def _run_phase_safely(name: str, fn: callable, cid: str) -> None:
    """单 phase 容错执行：失败仅记录，不中断后续槽位。

    监控信号链的连续性（R2c 连续 3 日确认、WARNING 20 日升级）依赖每日盯盘，
    而推荐引擎在 LLM 失败/模型缺失时会 raise——若串行同槽位，推荐失败会让持仓断盯。
    推荐与监控各自独立容错：推荐失败只影响当天推荐，监控照常扫描持仓。
    """
    try:
        _run_phases([(name, fn)], name, cid)
    except Exception as e:
        logger.with_cid(cid).error_event("phase_failed_continue", f"{name}执行失败，后续槽位继续: {e}",
                                         extra={"phase": name, "error": str(e)}, exc_info=True)


def _run_slot(label: str, today: datetime | None = None) -> tuple[datetime, str]:
    """槽位入口脚手架：日期与 cid 生成 + 启动日志（三个入口共用，cid 随 adapter 绑定）。"""
    today = today or datetime.now()
    cid = _new_cid()
    logger.with_cid(cid).info_event("pipeline_start", f"{label}启动")
    return today, cid


def _ensure_recommend_data_ready() -> bool:
    """推荐门控自愈：数据未就绪时先尝试补齐，复用数据基座幂等/熔断/冷却机制。

    覆盖两类场景：
    - 首次部署自举：持仓为空 → 触发数据基座 Step 4（持仓 + 行业映射）；
    - 行业映射失败缺口：持仓成功但行业映射拉取失败（holdings_last_run 已置位，
      7 天内 Step 4 不重跑）→ 只增量补拉行业映射（未映射股票）。
    自愈失败记录冷却标记，冷却期内直接返回 False（保持拦截，不再白耗重试）。
    自愈在管线编排层执行（引擎不自审自拦，架构深化候选 2 保持），
    数据基座内部自带熔断/限流协调/失败冷却，不会与调度器双跑（runner 层并发锁）。
    就绪判定经 repo.is_recommend_data_ready 单一谓词（异常兜底 False）；
    自愈动作所需计数细节仍由 repo.check_data_ready 提供，读失败按无法自愈处理。
    """
    if repo.is_recommend_data_ready():
        return True
    # 冷却期检查：上次自愈失败未满 _HEAL_COOLDOWN_DAYS 天 → 直接拦截；
    # 间隔窄读（无记录/解析失败返回 None）按未冷却处理，外层 _run_phase_safely 兜底
    heal_gap = repo.get_interval_days(META.RECOMMEND_DATA_HEAL_FAILED)
    if heal_gap is not None and heal_gap <= _HEAL_COOLDOWN_DAYS:
        logger.warning("推荐门控自愈冷却中（上次失败 %s），跳过自愈，保持拦截",
                       repo.get_meta(META.RECOMMEND_DATA_HEAL_FAILED))
        return False
    try:
        # 需区分持仓空/行业映射空以决定自愈动作；计数读取失败视为无法自愈
        status = repo.check_data_ready()
        if status["holdings_cnt"] == 0:
            logger.warning("推荐门控自愈：持仓为空，触发数据基座 Step 4（持仓+行业映射，"
                           "首次自举可能耗时较长）")
            run_data_foundation(steps=[4])
        else:
            logger.warning("推荐门控自愈：行业映射为空（持仓已就绪），增量补拉行业映射")
            update_industry_map()
    except Exception as e:
        logger.error("推荐门控自愈失败: %s", str(e)[:150], exc_info=True)
    if repo.is_recommend_data_ready():
        return True
    try:
        repo.save_meta(META.RECOMMEND_DATA_HEAL_FAILED, datetime.now().strftime("%Y-%m-%d"))
    except Exception:
        pass
    return False


def _run_recommend_safely(cid: str) -> None:
    """推荐阶段：门控自愈与引擎执行统一走安全包装，异常不中断后续槽位（监控照常）。

    门控自身的 DB 读取（谓词/冷却标记）异常也纳入 _run_phase_safely 安全网，
    修复：门控异常曾穿过未保护调用链中断监控/进化槽位（架构深化候选 A）。
    """
    _run_phase_safely("推荐引擎", _run_recommend_gated, cid)


def _run_recommend_gated() -> None:
    """门控 + 推荐引擎：门控未就绪仅跳过推荐（监控不受影响）。"""
    if not _ensure_recommend_data_ready():
        logger.warning("数据基座产出未就绪（持仓/行业映射缺失，自愈后仍空），跳过推荐引擎；监控照常")
        return
    run_recommendation()


def run(today: datetime | None = None) -> None:
    """全流程（手动触发）：数据基座 → 推荐 → 监控 → 进化，各槽位互不阻断。"""
    today, cid = _run_slot("管线", today)
    # 数据基座失败不中断后续槽位：推荐有前置门控（数据就绪才跑）与特征新鲜度护栏、
    # 监控有净值新鲜度护栏（陈旧走数据告警），全流程继续执行
    _run_phase_safely("数据基座", lambda: run_data_foundation(steps=daily_steps()), cid)
    _run_recommend_safely(cid)
    _run_phase_safely("监控引擎", run_monitor, cid)
    _run_phases(_evolve_phase(today), "进化槽位", cid)


def run_data(today: datetime | None = None) -> None:
    """数据基座槽位（盘前固定时间执行）：数据步骤 + 每日进化，独立于推荐。"""
    today, cid = _run_slot("数据基座槽位", today)
    _run_phases([
        ("数据基座", lambda: run_data_foundation(steps=daily_steps())),
    ], "数据基座槽位", cid)
    _run_phases(_evolve_phase(today), "进化槽位", cid)


def run_recommend(today: datetime | None = None) -> None:
    """推荐槽位（盘中可配时间执行）：推荐 → 监控，依赖数据槽位产出的特征。

    推荐前置门控（数据就绪才跑）与监控解耦：门控拦截不影响监控盯盘，持仓信号链连续。
    """
    today, cid = _run_slot("推荐槽位", today)
    _run_recommend_safely(cid)
    _run_phase_safely("监控引擎", run_monitor, cid)

"""进化槽位编排（B 接线）：calibration 结算 + 信号校准 + knowledge 案例回流 + 月末质量度量。

每日 supervise 后由 pipeline._run_evolve_safely 调用 run_evolve(date, cid)：

1. settle：未结算信号按「40 交易日超额收益 < 0」判命中（对齐主标尺超额口径，
   避免绝对跌幅把 beta 当 alpha）；超额<0 = 信号说对了（风险兑现=跑输基准）。
2. assess：各信号 get_signal_history → calibration.assess → 降权/停用判定（记录，
   状态机/信号权重消费待后续接线）。
3. knowledge：今日进入 EXIT 的推荐对象 → make_case(bad) → save_case（喂给已就绪
   的 few-shot 回流 audit prompt）。异常回撤>8% 触发待后续（需 max_drawdown 接线）。
4. 月末 quality：月末调 compute_quality_metrics 落 quality_metrics（web 推荐质量 tab 读）。

设计：每步独立容错（数据不足/某信号失败不阻断其他），与 pipeline 槽位互不阻断一致。
"""

from datetime import datetime, timedelta

from app.utils.log import get_logger

logger = get_logger("evolve")

BENCH_INDEX = "sh000300"
FORWARD_DAYS = 40
SIGNAL_IDS = ("below_ema20", "alpha_neg_days", "valuation_high", "fatal_news")


def _excess_negative(fund: str, date: str) -> bool | None:
    """基金自 date 起 FORWARD_DAYS 交易日超额收益（相对沪深300）是否 < 0。

    None = 数据不足（nav 序列不足 40 日 / 基准缺失），跳过不结算。
    """
    from app.repo.nav import series as nav_series
    from app.repo.base import get_index_series

    navs = nav_series(fund)
    if not navs or len(navs) < 2:
        return None
    # t0 = 第一条 date >= 触发日（触发日可能非交易日，向后对齐）
    t0 = next((i for i, (d, _) in enumerate(navs) if d >= date), None)
    if t0 is None or t0 + FORWARD_DAYS >= len(navs):
        return None
    nav0, nav40 = navs[t0][1], navs[t0 + FORWARD_DAYS][1]
    if not nav0 or not nav40:
        return None
    fund_ret = nav40 / nav0 - 1.0
    d0, d40 = navs[t0][0], navs[t0 + FORWARD_DAYS][0]
    idx = {r[0]: r[1] for r in get_index_series(BENCH_INDEX, ("date", "close"))}
    if d0 not in idx or d40 not in idx:
        return None
    bench_ret = idx[d40] / idx[d0] - 1.0
    return (fund_ret - bench_ret) < 0


def _settle_pending(cid: str) -> None:
    """结算未结算信号：40 日超额<0 → 命中（hit=True）。"""
    from app.repo.tracked_state import get_pending_settlements, settle_signal
    log = logger.with_cid(cid)
    pending = get_pending_settlements()
    if not pending:
        return
    settled = 0
    for p in pending:
        fund = p.get("fund")
        if not fund:
            continue  # 旧数据无 fund，无法算超额
        hit = _excess_negative(fund, p["date"])
        if hit is None:
            continue  # 数据不足，暂不结算（下次再试）
        settle_signal(p["signal_id"], p["ts"], hit)
        settled += 1
        log.info_event("signal_settled",
                       f"{p['signal_id']} {fund} 超额{'<0 命中' if hit else '≥0 未中'}",
                       extra={"signal": p["signal_id"], "fund": fund, "hit": hit})
    log.info_event("settle_done",
                   f"结算 {settled} 条信号（待结算池 {len(pending)}）",
                   extra={"settled": settled, "pending": len(pending)})


def _calibration_assess(cid: str) -> None:
    """各信号命中率 → calibration.assess 降权/停用判定（记录，状态机消费待后续）。"""
    from app.engine.calibration import assess
    from app.repo.tracked_state import get_signal_history
    log = logger.with_cid(cid)
    for sid in SIGNAL_IDS:
        hist = get_signal_history(sid)
        if len(hist) < 10:  # MIN_SAMPLES
            continue
        r = assess(hist)
        action = r.get("action", "")
        hit_rate = r.get("hit_rate")
        log.info_event("calibration_assess",
                       f"{sid} 命中率 {hit_rate}（{len(hist)} 样本）{action}",
                       extra={"signal": sid, "hit_rate": hit_rate,
                              "samples": len(hist), "action": action})


def _knowledge_trigger(cid: str, today: str) -> None:
    """今日 EXIT 推荐对象 → Bad-Case 落库（few-shot 回流 audit prompt）。

    异常回撤>8% 触发待后续（需 max_drawdown 接线）；EXIT 是不可逆强信号，必生成案例。
    """
    from app.engine.knowledge import make_case
    from app.repo.knowledge import save_case
    from app.repo.tracked_state import get_all_tracked_states
    log = logger.with_cid(cid)
    states = get_all_tracked_states(limit=100)
    n = 0
    for s in states:
        if s.get("state") != "EXIT" or s.get("date") != today:
            continue
        code = s.get("object_id") or ""
        if not code:
            continue
        case = make_case("bad", today, code, None, None, {"exit": True, "state": "EXIT"})
        save_case(case)
        n += 1
        log.info_event("knowledge_case",
                       f"{code} EXIT → Bad-Case 落库（回流排雷 prompt）",
                       extra={"code": code})
    if n:
        log.info_event("knowledge_done", f"今日新增 {n} 条 Bad-Case", extra={"count": n})


def _is_month_end(today: str) -> bool:
    try:
        d = datetime.strptime(today, "%Y-%m-%d")
    except ValueError:
        return False
    return (d + timedelta(days=1)).month != d.month


def _monthly_quality(cid: str, today: str) -> None:
    """月末跑 compute_quality_metrics 落 quality_metrics（web 推荐质量 tab 读）。"""
    if not _is_month_end(today):
        return
    log = logger.with_cid(cid)
    try:
        d = datetime.strptime(today, "%Y-%m-%d")
        start = f"{d.year}-{d.month:02d}-01"
        from app.engine.quality import compute_quality_metrics
        from app.repo.quality_metric import save_quality_metrics
        m = compute_quality_metrics(start, today)
        save_quality_metrics(m)
        log.info_event("quality_monthly",
                       f"月末质量度量 {start}~{today} IC={m.get('ic')}",
                       extra={"period_start": start, "period_end": today, "ic": m.get("ic")})
    except Exception as e:
        log.warn_event("quality_monthly_failed", f"月末质量度量失败：{e}", exc_info=True)


def run_evolve(date: str, cid: str = "") -> None:
    """进化槽位入口：settle + assess + knowledge + 月末 quality。各步独立容错。"""
    log = logger.with_cid(cid)
    log.info_event("evolve_start", "进化槽位启动")
    for label, fn in (
        ("信号结算", lambda: _settle_pending(cid)),
        ("信号校准", lambda: _calibration_assess(cid)),
        ("知识回流", lambda: _knowledge_trigger(cid, date)),
        ("月末度量", lambda: _monthly_quality(cid, date)),
    ):
        try:
            fn()
        except Exception as e:
            log.error_event("evolve_step_failed",
                            f"{label}失败，后续步骤继续：{e}",
                            extra={"step": label, "error": str(e)}, exc_info=True)
    log.info_event("evolve_done", "进化槽位完成")

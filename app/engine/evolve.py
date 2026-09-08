"""进化引擎：月度结算 → 批量 LLM 元分析 → 教训入库（Phase 4 重构）。

闭环：sector_selections(趋势) + monitor_events(信号链)
      → LLM 对比成功/失败模式 → evolution_insights
      → 回流 选赛道 LLM + 定论 LLM。

运行：uv run python -m app.engine.evolve [2026-07]
"""

import json
from datetime import datetime, timedelta

import numpy as np

import app.repo as repo
from app import domain
from app.engine.insights import causal_check as _causal_check
from app.engine.insights import decay_insights as _decay_insights
from app.engine.insights import insight_conflicts as _insight_conflicts
from app.engine.insights import save_insight as _save_insight
from app.engine.insights import save_self_fix as _save_self_fix
from app.engine.quality import compute_e2e_metrics, compute_quality_metrics
from app.llm.client import LLMError, call_llm_json
from app.llm.prompts import evolution_analysis_prompt
from app.repo import meta_keys as META
from app.utils.log import get_logger

logger = get_logger("evolve")


# ── 排序自纠偏（保留）──────────────────────────────────────

def _apply_ranking_weights(weights: dict) -> bool:
    """将排序权重写入 meta 表，供 recommend 引擎经 repo.get_ranking_cfg 读取。

    momentum_guard_pct 是风控防线参数，不随进化权重覆盖：写入前强制沿用当前值。
    """
    try:
        cur = repo.get_ranking_cfg()
        guard = cur.get("momentum_guard_pct")
        if guard is not None:
            weights = {**weights, "momentum_guard_pct": guard}
        repo.save_ranking_cfg(weights)
        return True
    except Exception as e:
        logger.warning("写入排序权重失败: %s", str(e)[:120], exc_info=True)
        return False


def _review_ranking_all() -> list[str]:
    rows = repo.get_buyable_feature_stats()
    if len(rows) < 200:
        return []

    mom = np.array([r[1] for r in rows], dtype=float)
    hurst = np.array([r[2] for r in rows], dtype=float)
    calmar = np.array([r[3] for r in rows], dtype=float)
    fixes = []

    corr_hm = np.corrcoef(hurst, mom)[0, 1]
    if corr_hm < 0:
        fixes.append(f"hurst与动量负相关({corr_hm:+.3f})，趋势信号失效")

    corr_cm = np.corrcoef(calmar, mom)[0, 1]
    if corr_cm < 0:
        fixes.append(f"calmar与动量负相关({corr_cm:+.3f})，回撤质量信号失效")

    idx_mom = repo.get_index_momentum()
    rel = mom - idx_mom
    sorted_rel = np.sort(rel)[::-1]
    top10_mean = sorted_rel[:10].mean()
    bot10_mean = sorted_rel[-10:].mean()
    spread = top10_mean - bot10_mean
    if spread < 10:
        fixes.append(f"相对强弱区分度不足(Top10-Bottom10={spread:.1f}pp)")

    if fixes:
        # 不再直接写权重：排分自纠偏只报告信号，权重统一由 GA 调节（见 _ga_adjust）
        logger.info("排分自纠偏信号: %s", "；".join(fixes))
    return fixes


# ── 度量反哺（仅作 GA 紧急触发信号，不再直接调权重） ───────

_MIN_SAMPLE_FOR_ADJUST = 5

# Q4 反馈回路：结算结果每命中一次，相关 sector 洞察置信度调整幅度（clamp [0,1]）。
# 与月度衰减（×0.95）叠加后净效果：胜 +0.10 后衰减 ≈ +5.6%（缓慢上行），
# 负 -0.10 后衰减 ≈ -15.6%（明显下行）——若取 ±0.05 会被衰减完全抵消（净≈0）
_INSIGHT_REWARD_DELTA = 0.10

# Q6：元分析案例超上限时按三类比例抽样（防 token 超限、保证每类都有代表）
_MAX_ANALYSIS_CASES = 40
# 审计 P1-3（2026-09）：元分析最低案例门槛——止损 EXIT 先结算导致积累期样本
# 严重偏负（生产实证：首批 10 条全来自止损退出 1胜9负），案例不足时暂缓分析，
# 防偏斜结论被固化进 prompt。积累期提示对 early_exit 与 full_window 分开看。
_MIN_ANALYSIS_CASES = 20


def plan_param_adjustment(metrics: dict) -> str | None:
    """质量下行检测（纯函数）：赚钱胜率低于五成 → 返回原因（阶段5 赚钱口径）。

    只作为"紧急触发 GA 评估"的信号，不再直接修改权重
    （权重调节统一收敛到 GA 一种机制，避免三套调节器互相竞争）。
    """
    profit_rate = metrics.get("profit_rate")
    sample = metrics.get("sample_count", 0)
    if sample < _MIN_SAMPLE_FOR_ADJUST:
        return None
    if profit_rate is None or profit_rate >= 0.5:
        return None
    return (f"质量下行触发GA紧急评估: 赚钱胜率={profit_rate:.2f}<0.5"
            "（推荐后40日绝对收益>1%的占比不足五成）")


# ── 遗传算法参数寻优 ─────────────────────────────────────

_GA_MIN_INTERVAL_DAYS = 7
"""GA 评估最小间隔（天）：meta 记录 last_ga_run，距上次不足 7 天跳过。

自动管线每月 1 号只调一次 GA（必然 > 7 天），该限制从不拦截自动路径；
保留仅作防御：防止手动频繁调用 run_evolve 时重复跑 5-6 分钟的回测寻优。
"""

_MONTHLY_INTERVAL_DAYS = 28
"""月度重任务（自纠偏/GA/元分析/衰减）最小间隔（天）。

管线每天附加进化 phase，但每日只做幂等的结算与质量度量；
元分析（LLM 调用）与 GA（分钟级回测）按此间隔限频，避免每日重复消耗。
"""


def _monthly_due() -> bool:
    """月度重任务是否到期：距上次 last_monthly_evolve ≥ 28 天；无记录视为到期。"""
    gap = repo.get_interval_days(META.LAST_MONTHLY_EVOLVE)
    if gap is None:
        return True  # 无记录视为到期
    return gap >= _MONTHLY_INTERVAL_DAYS


def _ga_adjust(force: bool = False) -> str | None:
    """GA 寻优排序配置：fast 回测寻优，比当前配置的 fast fitness 显著更好才应用。

    返回调整说明；无改善、间隔未到或不可用返回 None。
    force=True 时跳过 7 天间隔（质量下行等紧急信号触发）。
    """
    # 频率限制：距上次 GA 评估 < 7 天跳过（评估即记录，无论是否应用）
    if not force:
        gap = repo.get_interval_days(META.LAST_GA_RUN)
        if gap is not None and gap < _GA_MIN_INTERVAL_DAYS:
            logger.info("距上次 GA 评估 %d 天(<%d)，跳过寻优", gap, _GA_MIN_INTERVAL_DAYS)
            return None

    try:
        from app.engine.ga import fitness, ga_optimize_ranking
    except Exception as e:
        logger.warning("GA 模块不可用，跳过寻优: %s", str(e)[:120])
        return None

    # P2-10 降噪：月度重任务评估取 repeats=3 回测中位数（噪声 ±8pp → ±4.6pp 量级）；
    # 当前配置与最优配置必须用同一 repeats 评估才可比
    repeats = 3
    best_cfg, best_f = ga_optimize_ranking(repeats=repeats)
    cur_cfg = repo.get_ranking_cfg()
    cur_f = fitness(cur_cfg.to_dict(), repeats=repeats)
    logger.info("GA 对比: 当前 fitness=%.3f vs 最优 fitness=%.3f", cur_f, best_f)
    repo.save_meta(META.LAST_GA_RUN, datetime.now().strftime("%Y-%m-%d"))
    # 显著改善才应用（阶段5 新 fitness = 赚钱胜率×2 + 期望收益%：
    # fast 回测 13 点样本下 profit_rate 噪声 ≈±8pp（fitness ±16），
    # 10.0 ≈ 赚钱胜率提升 5pp 量级（Q5 共识上修：8.0 门槛低于噪声幅度）
    if best_f - cur_f > 10.0:
        if _apply_ranking_weights(best_cfg):
            msg = f"GA寻优应用: fitness {cur_f:.3f}→{best_f:.3f}, 配置 {best_cfg}"
            _save_self_fix(msg)
            _record_ga_applied(best_cfg, cur_f, best_f)
            logger.info("%s", msg)
            return msg
    logger.info("GA 寻优无显著改善，保留当前配置 (Δfitness=%+.3f)", best_f - cur_f)
    return None


# ── 月度结算 ───────────────────────────────────────────────


def _settle_pool_outcomes(pool_sectors: list[str], reco_date: str) -> dict:
    """P1-5 否决反事实：量化池内每赛道取"第一行业命中且动量最高"的代表基金，
    计算其入场后 40 日收益——结算时对比"LLM 选中赛道 vs 池内未选/被否决赛道"，
    度量 LLM 选赛道是否系统性错过上涨方向（否决正确率的数据基础）。
    """
    outcomes: dict = {}
    for sector in pool_sectors:
        try:
            rows = repo.get_sector_candidates([sector])
        except Exception as e:
            logger.debug("反事实赛道 %s 候选查询失败: %s", sector, str(e)[:80])
            continue
        if not rows:
            continue
        best = max(rows, key=lambda r: r.get("momentum_20d") or 0, default=None)
        if best is None:
            continue
        ret = repo.nav.forward_return(best["code"], reco_date)
        if ret is not None and np.isfinite(ret):
            outcomes[sector] = round(float(ret), 6)
    return outcomes


def _settle_outcomes() -> int:
    """更新 sector_selections 的 outcome 字段（全部待定，幂等防漏）。

    - EXIT：用退出时实际收益（return_rate，监控平仓时写入）；
    - 非 EXIT：满 FORWARD_DAYS 交易日（含入场日 41 条净值）才结算，用第 40 条
      净值/入场净值计算 40 日绝对收益——与质量度量/GA fitness 同口径；
      窗口未满保持待定（丢弃未满窗口样本，下月自然补齐）；
    - 标签阈值与 quality 对齐（PROFIT_THRESHOLD=1%）：胜 > 1%、负 ≤ 1%，
      不再产生"平"——元分析案例标签与赚钱口径完全一致。
    - P1-5：结算时对量化池内全部候选赛道回填 40 日收益（pool_outcomes），
      供否决反事实度量（选中 vs 未选）。
    """
    rows = repo.get_pending_sector_selections()
    settled = 0
    today = datetime.now().strftime("%Y-%m-%d")

    for row in rows:
        # 容错解包：repo 契约现为 4 元组（含 pool_sectors，P1-5）；旧 3 元组调用兼容
        ss_id, log_id, used_insight_ids = row[0], row[1], row[2]
        pool_sectors_raw = row[3] if len(row) > 3 else None
        if not log_id:
            continue
        log = repo.get_recommendation_by_id(log_id)
        if not log:
            continue
        code, status, ret, reco_date, _entry_nav = log
        pool_sectors: list[str] = []
        if pool_sectors_raw:
            try:
                pool_sectors = json.loads(pool_sectors_raw)
            except (json.JSONDecodeError, TypeError):
                pool_sectors = []

        if status == domain.SIGNAL_EXIT:
            if ret is None:
                continue  # 退出但无收益数据（入场净值缺失），保守保持待定
            outcome = "胜" if ret > domain.PROFIT_THRESHOLD else "负"
            note = f"退出时收益 {ret*100:+.2f}%"
        else:
            # 满 FORWARD_DAYS 交易日才结算：与质量度量同口径（含入场日 41 条净值），单一来源 repo.nav.forward_return
            ret = repo.nav.forward_return(code, reco_date)
            if ret is None:
                continue
            outcome = "胜" if ret > domain.PROFIT_THRESHOLD else "负"
            note = f"40日收益 {ret*100:+.2f}%"

        pool_outcomes = _settle_pool_outcomes(pool_sectors, reco_date) if pool_sectors else None
        repo.update_sector_selection_outcome(ss_id, outcome, today, note,
                                             pool_outcomes=pool_outcomes)
        # Q4 反馈回路：按结算结果调该赛道用过的 sector 洞察置信度（胜 +、负 -，clamp [0,1]）
        if used_insight_ids:
            delta = _INSIGHT_REWARD_DELTA if outcome == "胜" else -_INSIGHT_REWARD_DELTA
            for iid in json.loads(used_insight_ids):
                repo.adjust_insight_confidence(iid, delta)
        settled += 1

    logger.info("月度结算: %d 条 sector_selections 已更新 outcome", settled)
    return settled


# ── 批量 LLM 元分析 ───────────────────────────────────────

def _collect_cases(last_ss_id: int = 0) -> tuple[list[dict], list[dict], list[dict]]:
    """收集 id > last_ss_id 的已结算案例（增量游标，含回填 outcome + 监控信号链）。

    不再按月过滤：结算由 FORWARD_DAYS 净值窗口决定，晚满窗的案例按 id 游标自然补入——
    修复「月 1 号未满窗、下月按月查不到」导致月中推荐永久丢失的时间窗错位。
    """
    rows = repo.get_settled_cases_after(last_ss_id)

    successes, failures, neutrals = [], [], []
    for r in rows:
        # repo 返回结构化行（键=列名），不再按位置解包裸元组（加列不崩）
        sectors = json.loads(r["recommended_sectors"]) if r.get("recommended_sectors") else []
        code, name = r.get("code"), r.get("name")
        case = {
            "id": r.get("id"),
            "sectors": sectors, "fund": f"{code} {name}" if code else "无",
            "outcome": r.get("outcome"), "note": r.get("outcome_note") or "",
            "reasoning": (r.get("sector_reasoning") or "")[:200],
            "buy_reason": (r.get("buy_reason") or "")[:200],
            "regime": r.get("regime_label") or "",
            "signal": r.get("signal") or "",
            # 审计 P1-3：标注结算方式——early_exit=监控止损提前平仓（退出时收益），
            # full_window=满 40 日窗口结算。止损退出先结算导致积累期样本严重偏负，
            # LLM 元分析需据此分诊而非当成随机样本。
            "exit_mode": "early_exit" if r.get("signal") == domain.SIGNAL_EXIT else "full_window",
            "signal_triggers": {
                "trailing": r.get("trigger_trailing") or 0,
                "drift": r.get("trigger_drift") or 0,
                "sector_adv": r.get("trigger_sector_adv") or 0,
            },
            "logic": {
                "verdict": r.get("logic_verdict") or "",
                "sector_risk": r.get("sector_risk") or 0,
                "holding_risk": r.get("holding_risk") or 0,
                "reason": (r.get("detail") or "")[:200],
            },
        }
        outcome = r.get("outcome")
        if outcome == "胜":
            successes.append(case)
        elif outcome == "负":
            failures.append(case)
        else:
            neutrals.append(case)

    # Q6：整月全量收集后，超上限按三类比例抽样（每类保底 1 条，防 token 超限）
    total = len(successes) + len(failures) + len(neutrals)
    if total > _MAX_ANALYSIS_CASES:
        rng = np.random.default_rng()

        def _sample(cases: list) -> list:
            target = max(1, round(len(cases) * _MAX_ANALYSIS_CASES / total))
            if len(cases) <= target:
                return cases
            idx = rng.choice(len(cases), target, replace=False)
            return [cases[i] for i in sorted(idx)]

        successes = _sample(successes)
        failures = _sample(failures)
        neutrals = _sample(neutrals)

    return successes, failures, neutrals


def _batch_llm_analyze(successes: list, failures: list, neutrals: list | None = None,
                       decision_loss: float | None = None,
                       loss_streak: int = 0) -> list[dict] | None:
    if neutrals is None:
        neutrals = []
    prompt = evolution_analysis_prompt(successes, failures, neutrals,
                                       decision_loss=decision_loss,
                                       loss_streak=loss_streak)

    try:
        result = call_llm_json(prompt, temperature=0.3, max_tokens=16384, fallback=None,
                               caller="evolve_meta_analysis")
    except LLMError as e:
        # 技术失败（候选 7）：软失败，不推进游标，下次重试
        logger.warning("LLM 不可用，跳过洞察分析: %s", str(e)[:120])
        return None
    if isinstance(result, list):
        # P3-11：过滤非 dict / 缺 insight 键的条目，condition 可选透传
        cleaned = []
        for item in result:
            if isinstance(item, dict) and item.get("insight"):
                cleaned.append(item)
        return cleaned if cleaned else None
    if isinstance(result, dict) and "insight" in result:
        return [result]
    logger.warning("LLM 不可用或返回无法解析，跳过洞察分析")
    return None  # None = 分析失败（调用方不推进游标，下次重试）


def _decision_loss_streak(limit: int = 3) -> int:
    """裁决损耗连续为负的月数（Q8：连续 N 个月为负 → 元分析复核 / 降级信号）。

    从最新一次度量往前数；无数据或非负即中断。
    """
    streak = 0
    for r in repo.get_quality_metrics(limit):
        dl = r.get("decision_loss")
        if dl is not None and dl < 0:
            streak += 1
        else:
            break
    return streak


# ── P1-5 否决反事实与空仓率监控 ───────────────────────────

def _veto_stats() -> list[str]:
    """LLM 选赛道环节的偏差监控（月度重任务）：

    1. 空推荐率：近 60 天 空推荐日 / (空推荐日 + 实际推荐日)，过高提示系统过度保守；
    2. 否决反事实：已结算案例中"池内未选赛道均值收益显著高于选中赛道"的比例，
       过高提示 LLM 选赛道系统性错过上涨方向。
    仅输出信号（self-fix 报告），不自动改权重——与排分自纠偏同定位。
    """
    fixes: list[str] = []
    days = 60
    try:
        empty = len(repo.get_empty_reco_dates(days))
        recos = len(repo.get_reco_dates(days))
        total = empty + recos
        if total >= 10 and empty / total > 0.5:
            fixes.append(f"空推荐率过高: 近{days}天 {empty}/{total} ({empty/total*100:.0f}%)，"
                         "LLM/量化池可能过度保守，需检查否决与空仓判定")
    except Exception as e:
        logger.warning("空推荐率统计失败: %s", str(e)[:100])

    try:
        rows = repo.get_pool_outcomes_rows()
        total_cmp = 0
        miss_wins = 0
        for _rid, _date, rec_raw, pool_raw, out_raw in rows:
            try:
                rec = json.loads(rec_raw or "[]")
                pool = json.loads(pool_raw or "[]")
                out = json.loads(out_raw or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            if not rec or not pool or not out:
                continue
            rec_vals = [out[s] for s in rec if s in out]
            miss_vals = [out[s] for s in pool if s not in rec and s in out]
            if not rec_vals or not miss_vals:
                continue
            rec_mean = float(np.mean(rec_vals))
            miss_mean = float(np.mean(miss_vals))
            total_cmp += 1
            # 未选赛道均值显著高于选中（>0.5pp）：LLM 错过上涨方向的证据
            if miss_mean > rec_mean + 0.005:
                miss_wins += 1
        if total_cmp >= 3 and miss_wins / total_cmp > 0.6:
            fixes.append(f"否决反事实: {total_cmp}例中{miss_wins}例未选赛道均值显著高于选中"
                         f"({miss_wins/total_cmp*100:.0f}%)，LLM选赛道可能系统性偏差")
    except Exception as e:
        logger.warning("否决反事实统计失败: %s", str(e)[:100])
    return fixes


# ── 主入口 ─────────────────────────────────────────────────

def _record_ga_applied(cfg: dict, f_before: float, f_after: float) -> None:
    """记录 GA 权重应用来源（meta last_ga_applied）：fitness 快照 + 时间戳。

    可审计：区分真实寻优结果与测试/调试 mock 值（历史 mock fitness 30→45
    曾污染 evolution_insights），应用时留痕便于追溯。
    """
    try:
        repo.save_meta(META.LAST_GA_APPLIED, json.dumps({
            "date": datetime.now().strftime("%Y-%m-%d"),
            "fitness_before": f_before,
            "fitness_after": f_after,
            "cfg": cfg,
        }, ensure_ascii=False))
    except Exception as e:
        logger.warning("记录 GA 应用来源失败: %s", str(e)[:80])


def _last_analysis_ss_id() -> int:
    """上次元分析已覆盖的最大 sector_selections.id（游标，默认 0 = 全量）。"""
    return repo.get_int_cursor(META.LAST_ANALYSIS_SS_ID)


def _record_analysis_outcome(status: str, reason: str = "", cases: int = 0,
                             added: int = 0) -> None:
    """元分析健康度埋点（审计 P1-1）：ok/failed/no_cases 落 meta，连续失败≥3 → error。

    修复"闭环空转不可见"：此前 LLM 软失败只留 warning 日志、游标不动、insights 0 条，
    维护者无从得知"教训回流从未生效过一次"（生产实证：llm_audit 中 evolve 调用 0 条）。
    每次月度重任务的元分析都记录 outcome（含失败原因/案例数），连续失败累计达阈值
    提升为 error 级告警，Web 面板可读取展示"进化是否在真实学习"。
    """
    fail_streak = 0
    if status == "failed":
        # 连续失败计数：仅 LLM/异常失败算失败；ok 复位、no_cases 为正常状态不累计
        try:
            prev_raw = repo.get_meta(META.LAST_ANALYSIS_OUTCOME)
            if prev_raw:
                prev = json.loads(prev_raw)
                fail_streak = (prev.get("fail_streak", 0) + 1
                               if prev.get("status") == "failed" else 1)
            else:
                fail_streak = 1
        except (json.JSONDecodeError, TypeError):
            fail_streak = 1
    outcome = {"status": status, "reason": reason[:200], "cases": cases,
               "added": added, "fail_streak": fail_streak,
               "at": datetime.now().strftime("%Y-%m-%d %H:%M")}
    try:
        repo.save_meta(META.LAST_ANALYSIS_OUTCOME, json.dumps(outcome, ensure_ascii=False))
    except Exception as e:
        logger.warning("元分析健康度埋点失败: %s", str(e)[:120])
    if fail_streak >= 3:
        logger.error("进化元分析连续 %d 次失败（%s）——教训回流可能长期未生效，请检查 LLM 服务/结算数据",
                     fail_streak, reason or "未知原因")


def _run_meta_analysis(metrics: dict | None, degraded: bool) -> None:
    """批量 LLM 元分析（增量游标）：收集 id > last_analysis_ss_id 的已结算案例。

    LLM 有产出（含全部与旧洞察冲突的情况）才推进游标；失败保持游标不动，下次重试。
    每次执行记录健康度 outcome（审计 P1-1）。
    """
    try:
        last_id = _last_analysis_ss_id()
        successes, failures, neutrals = _collect_cases(last_id)
        cases = len(successes) + len(failures) + len(neutrals)
        if cases == 0:
            logger.info("无新结算案例，跳过元分析")
            _record_analysis_outcome("no_cases", "无新结算案例")
            return
        if cases < _MIN_ANALYSIS_CASES:
            # 审计 P1-3：止损 EXIT 先结算导致积累期样本严重偏负（生产实证 1胜9负），
            # 案例不足时暂缓元分析，防偏斜结论被固化进 prompt
            logger.info("元分析案例不足 %d（当前 %d），跳过：止损退出先结算会让积累期样本偏斜",
                        _MIN_ANALYSIS_CASES, cases)
            _record_analysis_outcome("insufficient_cases",
                                     f"案例 {cases} < 门槛 {_MIN_ANALYSIS_CASES}", cases=cases)
            return
        loss_streak = _decision_loss_streak()
        insights = _batch_llm_analyze(
            successes, failures, neutrals,
            decision_loss=metrics.get("decision_loss") if metrics else None,
            loss_streak=loss_streak,
        )
        if insights is None:
            logger.warning("LLM 元分析失败，保持游标待重试（%d 条案例）", cases)
            _record_analysis_outcome("failed", "LLM 不可用或解析失败", cases=cases)
            return
        added = 0
        # P0-2 批次内去重：同一次元分析常产出多条近似洞察（如"回避赛道重合→EXIT"的
        # 三种变体，8-12 曾同日入库 5 条）；批内两两 Jaccard 去重后只保留语义最新的一条，
        # 避免同一案例的模式被重复固化、重复计数 apply/衰减。
        kept: list[dict] = []
        for ins in insights:
            if _insight_conflicts(ins["insight"], [k["insight"] for k in kept]):
                continue
            kept.append(ins)
        for ins in kept:
            if _save_insight(ins, degraded=degraded):
                added += 1
        if len(kept) < len(insights):
            logger.info("批次内去重: %d 条近似洞察合并为 %d 条", len(insights), len(kept))
        logger.info("批量元分析: %d条成功/%d条失败/%d条中性 → 新增 %d 条洞察",
                    len(successes), len(failures), len(neutrals), added)
        _record_analysis_outcome("ok", cases=cases, added=added)
        # 分析成功（LLM 有产出）才推进游标：已结算案例下次不再重复分析
        max_id = max(c["id"] for c in successes + failures + neutrals)
        repo.save_meta(META.LAST_ANALYSIS_SS_ID, str(max_id))
    except Exception as e:
        logger.warning("元分析失败: %s", str(e)[:120], exc_info=True)
        _record_analysis_outcome("failed", str(e)[:120])


def _month_bounds(month: str) -> tuple[str, str]:
    """返回某年月的首日与末日（YYYY-MM → YYYY-MM-DD）。"""
    first = datetime.strptime(month, "%Y-%m").date()
    last = (first.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    return first.strftime("%Y-%m-%d"), last.strftime("%Y-%m-%d")


def _default_evolve_month(now: datetime | None = None) -> str:
    """默认进化的月份：当前日期的上个月（管线每月 1 号触发 = 进化上月数据）。"""
    now = now or datetime.now()
    first_this = now.replace(day=1)
    return (first_this - timedelta(days=1)).strftime("%Y-%m")


def run_evolve(month: str | None = None) -> None:
    """进化引擎主入口。

    每日调用（管线每天附加）：
      - 结算全部待定（幂等，满 FORWARD_DAYS 净值窗口即结算，不再等月 1 号巧合满窗）；
      - 质量度量上月（幂等覆盖：晚满窗的推荐每天重算覆盖，最终收敛到完整样本）。
    月度到期（距上次重任务 ≥28 天）或手动传 month 时追加重任务：
      自纠偏 + GA 寻优 + LLM 元分析（增量游标）+ 置信度衰减。
    month 参数仅用于补算历史月份的质量度量（如 evolve 2026-07）。
    """
    explicit_month = month is not None
    if month is None:
        month = _default_evolve_month()

    # 1. 每日必做：月度结算（全部待定，幂等防漏，满窗即结算）
    _settle_outcomes()

    # 2. 每日必做：推荐质量度量（统计区间 = month 当月首日至末日，幂等覆盖收敛）
    degraded = False
    metrics = None
    try:
        # 当月尚未结束时不计算质量度量：forward 40 日窗口未走完，
        # 月初运行只会产生样本为 0 的空行；历史月份需传入 month 参数补算
        if month == datetime.now().strftime("%Y-%m"):
            logger.info("本月 %s 尚未结束，跳过质量度量（历史月份可运行 evolve YYYY-MM 补算）", month)
        else:
            start, end = _month_bounds(month)
            metrics = compute_quality_metrics(start, end)
            metrics["computed_date"] = datetime.now().strftime("%Y-%m-%d")
            # #2 端到端 P&L：按实际退出日期扣赎回费后的净收益，对比 40 日理论收益
            # （timing_contribution = 监控择时的真实贡献，与选基质量分账）
            metrics.update(compute_e2e_metrics(start, end))
            if metrics.get("sample_count", 0) == 0:
                # 空样本不入库：避免污染 quality_metrics（无推荐月留空行且永不被覆盖）
                logger.info("推荐质量度量无样本，跳过入库: 区间 %s~%s", start, end)
            else:
                repo.save_quality_metrics(metrics)
                logger.info("推荐质量度量已入库: 区间 %s~%s, IC=%s, 赚钱胜率=%s, 裁决损耗=%s",
                            start, end, metrics.get("ic"), metrics.get("profit_rate"),
                            metrics.get("decision_loss"))
                adjustment = plan_param_adjustment(metrics)
                degraded = adjustment is not None
                if adjustment:
                    logger.info("质量下行信号（触发GA紧急评估）: %s", adjustment)
    except Exception as e:
        logger.warning("推荐质量度量失败: %s", str(e)[:120], exc_info=True)

    # 3. 月度重任务（自纠偏 + GA + 元分析 + 衰减）：手动补算或距上次 ≥28 天
    # 修复：heavy 必须用显式传参标志（explicit_month）判断——原 `month is not None`
    # 在 month 已被默认值填充后恒为 True，导致每日路径也执行重任务并每天覆盖
    # LAST_MONTHLY_EVOLVE，使 _monthly_due() 的 28 天限频成为死代码
    heavy = explicit_month or _monthly_due()
    if heavy:
        try:
            # 3a. 排分自纠偏（每月必跑）
            fixes = _review_ranking_all()
            # P1-5：LLM 选赛道偏差监控（空推荐率/否决反事实）追加进自纠偏信号
            fixes += _veto_stats()
            for fix in fixes:
                _save_self_fix(fix)

            # 3b. 遗传算法参数寻优（唯一权重调节器；质量下行或自纠偏信号时 force 跳过间隔）
            try:
                ga_note = _ga_adjust(force=degraded or bool(fixes))
                if ga_note:
                    logger.info("GA 寻优已应用: %s", ga_note)
            except Exception as e:
                logger.warning("GA 寻优失败: %s", str(e)[:120], exc_info=True)

            # 3c. 批量 LLM 元分析（增量游标；质量下行时新洞察以非活跃态入库待审）
            _run_meta_analysis(metrics, degraded)

            # 3c2. 洞察因果验证（#4）：生效后 vs 生效前同类案例胜率比对，仅报告不自动改置信度
            try:
                for fix in _causal_check():
                    _save_self_fix(fix)
            except Exception as e:
                logger.warning("洞察因果验证失败: %s", str(e)[:120], exc_info=True)

            # 3d. 置信度衰减
            _decay_insights()

            repo.save_meta(META.LAST_MONTHLY_EVOLVE, datetime.now().strftime("%Y-%m-%d"))
            logger.info("进化月度重任务完成")
        except Exception as e:
            logger.warning("进化重任务失败: %s", str(e)[:120], exc_info=True)
    else:
        logger.info("距上次重任务不足 %d 天，仅执行每日结算与质量度量", _MONTHLY_INTERVAL_DAYS)

    logger.info("进化完成: 结算+质量度量%s", "+元分析+衰减" if heavy else "")


if __name__ == "__main__":
    import sys
    m = sys.argv[1] if len(sys.argv) > 1 else None
    run_evolve(m)

"""进化引擎：月度结算 → 批量 LLM 元分析 → 教训入库（Phase 4 重构）。

闭环：sector_selections(趋势) + monitor_events(信号链)
      → LLM 对比成功/失败模式 → evolution_insights
      → 回流 选赛道 LLM + 定论 LLM。

运行：uv run python -m app.engine.evolve [2026-07]
"""

import json
import re
import time
from datetime import datetime, timedelta

import numpy as np

from app.utils.log import get_logger
from app.llm.client import call_llm_json
from app.llm.prompts import evolution_analysis_prompt
from app.engine.quality import compute_quality_metrics
from app import domain
import app.repo as repo

logger = get_logger("evolve")


# ── 排序自纠偏（保留）──────────────────────────────────────

def _apply_ranking_weights(weights: dict) -> bool:
    """将排序权重写入 meta 表，供 recommend._load_ranking_cfg 读取。"""
    try:
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
        new = dict(domain.DEFAULT_RANKING_CFG)
        new.update({"rel_strength_weight": 0.3, "hurst_weight": 0.05})
        _apply_ranking_weights(new)
    return fixes


# ── 度量反哺 ───────────────────────────────────────────────

_MIN_SAMPLE_FOR_ADJUST = 5
_MODEL_WEIGHT_FLOOR = 0.1


def plan_param_adjustment(metrics: dict, cfg: dict) -> dict | None:
    """质量下行时规划参数调整：IC 为负或超额胜率低于五成 → 降低模型权重。

    返回 {"cfg": 新权重, "reason": 说明}；证据不足或质量健康时返回 None。
    """
    ic = metrics.get("ic")
    win_rate = metrics.get("excess_win_rate")
    sample = metrics.get("sample_count", 0)
    if sample < _MIN_SAMPLE_FOR_ADJUST:
        return None
    degraded = (ic is not None and ic < 0) or (win_rate is not None and win_rate < 0.5)
    if not degraded:
        return None
    new_cfg = dict(cfg)
    new_model = max(_MODEL_WEIGHT_FLOOR, new_cfg["model_weight"] * 0.5)
    if new_model == new_cfg["model_weight"]:
        return None
    new_cfg["model_weight"] = new_model
    triggers = []
    if ic is not None and ic < 0:
        triggers.append(f"IC={ic:.3f}<0（预测与实现负相关）")
    if win_rate is not None and win_rate < 0.5:
        triggers.append(f"超额胜率={win_rate:.2f}<0.5")
    return {
        "cfg": new_cfg,
        "reason": (f"质量下行触发参数调整: {'、'.join(triggers)}；"
                   f"模型权重 {cfg['model_weight']}→{new_model}"),
    }


def apply_param_adjustment(metrics: dict) -> str | None:
    """度量反哺入口：按质量指标调整排序权重并留痕，返回调整说明。"""
    plan = plan_param_adjustment(metrics, repo.get_ranking_cfg())
    if plan is None:
        return None
    if not _apply_ranking_weights(plan["cfg"]):
        return None
    _save_self_fix(plan["reason"])
    return plan["reason"]


# ── 月度结算 ───────────────────────────────────────────────

def _settle_outcomes(month: str) -> int:
    """更新 sector_selections 的 outcome 字段。"""
    rows = repo.get_pending_sector_selections(month)
    settled = 0
    today = datetime.now().strftime("%Y-%m-%d")

    for ss_id, log_id in rows:
        if not log_id:
            continue
        log = repo.get_recommendation_by_id(log_id)
        if not log:
            continue
        status, ret, reco_date = log

        if status in (domain.SIGNAL_EXIT, domain.SIGNAL_HOLD, domain.SIGNAL_BUY_MORE, domain.SIGNAL_WARNING):
            reco_dt = datetime.strptime(reco_date, "%Y-%m-%d")
            days = (datetime.now() - reco_dt).days
            if days < domain.FORWARD_DAYS and status != domain.SIGNAL_EXIT:
                continue

        outcome, note = "平", ""
        if status == domain.SIGNAL_EXIT:
            if ret is not None:
                if ret > 0.02:
                    outcome, note = "胜", f"退出时收益 {ret*100:+.2f}%"
                elif ret < -0.05:
                    outcome, note = "负", f"退出时亏损 {ret*100:.2f}%"
                else:
                    outcome, note = "平", f"退出时收益 {ret*100:+.2f}%"
        else:
            if ret is not None:
                if ret > 0.02:
                    outcome, note = "胜", f"运行{days}日收益 {ret*100:+.2f}%"
                elif ret < -0.05:
                    outcome, note = "负", f"运行{days}日亏损 {ret*100:.2f}%"

        repo.update_sector_selection_outcome(ss_id, outcome, today, note)
        settled += 1

    logger.info("月度结算: %d 条 sector_selections 已更新 outcome", settled)
    return settled


# ── 批量 LLM 元分析 ───────────────────────────────────────

def _collect_cases(month: str) -> tuple[list[dict], list[dict], list[dict]]:
    """收集当月推荐案例（含回填 outcome 后的结果 + 监控信号链）。"""
    rows = repo.get_monthly_cases(month)

    successes, failures, neutrals = [], [], []
    for r in rows:
        (_, log_id, sectors_json, reasoning, regime, outcome, note, buy_reason, code, name,
         signal, trig_trail, trig_drift, trig_sector, logic_v, sector_r, holding_r, detail) = r
        sectors = json.loads(sectors_json) if sectors_json else []
        case = {
            "sectors": sectors, "fund": f"{code} {name}" if code else "无",
            "outcome": outcome, "note": note or "",
            "reasoning": (reasoning or "")[:200],
            "buy_reason": (buy_reason or "")[:200],
            "regime": regime or "",
            "signal": signal or "",
            "signal_triggers": {
                "trailing": trig_trail or 0, "drift": trig_drift or 0,
                "sector_adv": trig_sector or 0,
            },
            "logic": {
                "verdict": logic_v or "", "sector_risk": sector_r or 0,
                "holding_risk": holding_r or 0, "reason": (detail or "")[:200],
            },
        }
        if outcome == "胜":
            successes.append(case)
        elif outcome == "负":
            failures.append(case)
        else:
            neutrals.append(case)

    return successes, failures, neutrals


def _batch_llm_analyze(successes: list, failures: list, neutrals: list | None = None) -> list[dict]:
    if neutrals is None:
        neutrals = []
    prompt = evolution_analysis_prompt(successes, failures, neutrals)

    result = call_llm_json(prompt, temperature=0.3, max_tokens=1536, fallback=None)
    if isinstance(result, list):
        return result
    if isinstance(result, dict) and "insight" in result:
        return [result]
    logger.warning("LLM 不可用或返回无法解析，跳过洞察分析")
    return []


def _keywords(text: str) -> set:
    text = re.sub(r"[^\w\u4e00-\u9fff]", " ", text)
    return set(t for t in text.split() if len(t) >= 2)


def _insight_conflicts(new_insight: str, existing: list) -> bool:
    """用关键词级 Jaccard 判断洞察是否与已有记录重复。"""
    new_kw = _keywords(new_insight)
    if not new_kw:
        return True
    for ei in existing:
        ei_kw = _keywords(ei)
        if not ei_kw:
            continue
        overlap = len(new_kw & ei_kw) / len(new_kw | ei_kw)
        if overlap > 0.5:
            return True
    return False


def _save_insight(insight: dict, degraded: bool = False) -> bool:
    """入库洞察；质量下行（degraded）时以非活跃状态入库（待审），不自动启用。"""
    existing = repo.get_all_insights()
    if _insight_conflicts(insight["insight"], existing):
        return False
    active = 0 if degraded else 1
    repo.insert_insight(insight["insight"], insight.get("type", "sector"),
                        datetime.now().strftime("%Y-%m-%d"), active)
    logger.info("新洞察入库: [%s] %s (active=%s)",
                insight.get("type", "?"), insight["insight"][:60], active)
    return True


# ── 置信度衰减 ─────────────────────────────────────────────

def _decay_insights() -> int:
    """降低旧洞察置信度，长期无用则标记非活跃。"""
    rows = repo.list_active_insights()
    decayed = 0
    for rid, conf, cnt in rows:
        new_conf = float(conf) * 0.95
        active = 1 if new_conf > 0.2 else 0
        repo.update_insight_confidence(rid, new_conf, active)
        decayed += 1
    logger.info("置信度衰减: %d 条洞察已更新", decayed)
    return decayed


# ── 主入口 ─────────────────────────────────────────────────

def _save_self_fix(fix: str) -> None:
    repo.insert_insight(fix, "ranking", datetime.now().strftime("%Y-%m-%d"), active=1)
    logger.info("排分自纠偏: %s", fix[:60])


def _month_bounds(month: str) -> tuple[str, str]:
    """返回某年月的首日与末日（YYYY-MM → YYYY-MM-DD）。"""
    first = datetime.strptime(month, "%Y-%m").date()
    last = (first.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    return first.strftime("%Y-%m-%d"), last.strftime("%Y-%m-%d")


def run_evolve(month: str | None = None) -> None:
    """进化引擎主入口。month 为待进化月份（如 '2026-07'），未传则默认当前月。"""
    if month is None:
        month = datetime.now().strftime("%Y-%m")

    # 1. 排分自纠偏（每月必跑）
    fixes = _review_ranking_all()
    for fix in fixes:
        _save_self_fix(fix)

    # 2. 月度结算
    _settle_outcomes(month)

    # 3. 推荐质量度量（统计区间 = month 当月首日至末日，与结算/元分析同月口径；
    #    度量先于元分析执行，质量下行信号可供洞察采纳判断使用）
    degraded = False
    try:
        # 当月尚未结束时不计算质量度量：forward 20 日窗口未走完，
        # 月初运行只会产生样本为 0 的空行；历史月份需传入 month 参数补算
        if month == datetime.now().strftime("%Y-%m"):
            logger.info("本月 %s 尚未结束，跳过质量度量（历史月份可运行 evolve YYYY-MM 补算）", month)
        else:
            start, end = _month_bounds(month)
            metrics = compute_quality_metrics(start, end)
            metrics["computed_date"] = datetime.now().strftime("%Y-%m-%d")
            repo.save_quality_metrics(metrics)
            logger.info("推荐质量度量已入库: 区间 %s~%s, IC=%s, 超额胜率=%s",
                        start, end, metrics.get("ic"), metrics.get("excess_win_rate"))
            adjustment = apply_param_adjustment(metrics)
            degraded = adjustment is not None
            if adjustment:
                logger.info("度量反哺: %s", adjustment)
    except Exception as e:
        logger.warning("推荐质量度量失败: %s", str(e)[:120], exc_info=True)

    # 4. 批量 LLM 元分析（质量下行时新洞察以非活跃态入库，待审不自动启用）
    successes, failures, neutrals = _collect_cases(month)
    if not successes and not failures and not neutrals:
        logger.info("当月无可分析案例，跳过元分析")
    else:
        insights = _batch_llm_analyze(successes, failures, neutrals)
        added = 0
        for ins in insights:
            if _save_insight(ins, degraded=degraded):
                added += 1
        logger.info("批量元分析: %d条成功/%d条失败/%d条中性 → 新增 %d 条洞察",
                    len(successes), len(failures), len(neutrals), added)

    # 5. 置信度衰减
    _decay_insights()

    logger.info("进化完成: 结算+质量度量+元分析+衰减")


if __name__ == "__main__":
    import sys
    m = sys.argv[1] if len(sys.argv) > 1 else None
    run_evolve(m)

"""推荐质量度量模块：赚钱胜率、期望绝对收益、盈亏比（阶段5 赚钱口径）。

度量回答"进化后推荐质量是否提升"：profit_rate = 推荐后 40 日绝对收益 > 1%
（覆盖申赎成本）的占比；mean_abs_ret = 期望绝对收益；payoff_ratio = 盈亏比。
IC（预测分与实现收益的秩相关）保留为排序能力辅助指标。

窗口：与 FORWARD_DAYS（40 交易日）单一来源对齐，
推荐后 41 条净值（含入场日），取第 0 与第 40 条。

IC 的 Spearman 秩相关用 numpy 手写（含并列平均秩），不依赖 scipy。
"""

import json
from collections.abc import Sequence
from datetime import datetime

import numpy as np

from app import domain, repo
from app.features.fees import redemption_fee_pct
from app.utils.log import get_logger

logger = get_logger("quality")


def _rankdata(x: np.ndarray) -> np.ndarray:
    """计算平均秩（tie 取平均），与 scipy.stats.rankdata(average) 一致。"""
    x = np.asarray(x, dtype=float)
    n = x.size
    sorter = np.argsort(x, kind="stable")
    inv: np.ndarray = np.empty(n, dtype=np.intp)
    inv[sorter] = np.arange(n)
    sx = x[sorter]
    obs = np.concatenate(([True], sx[1:] != sx[:-1]))
    dense = obs.cumsum()[inv]
    group_idx = np.flatnonzero(obs)
    counts = np.diff(np.concatenate((group_idx, [n])))
    avg_rank = group_idx + 1 + 0.5 * (counts - 1)
    return avg_rank[dense - 1]


def spearman(x: list[float], y: list[float]) -> float | None:
    """Spearman 秩相关；常数序列（无秩差异）返回 None。"""
    rx = _rankdata(np.asarray(x, dtype=float))
    ry = _rankdata(np.asarray(y, dtype=float))
    with np.errstate(invalid="ignore", divide="ignore"):
        corr = np.corrcoef(rx, ry)[0, 1]
    return float(corr) if not np.isnan(corr) else None


def profit_stats(rets: Sequence[float | None], threshold: float = domain.PROFIT_THRESHOLD) -> dict:
    """对收益序列算赚钱口径（单一来源）：名义胜率 / 赚钱胜率 / 盈亏比。

    赚钱 = 绝对收益 > threshold（覆盖申赎成本）。quality 度量与回测汇总共用，
    序列与阈值单位一致即可（小数序列配 0.01，百分数序列配 1.0）。
    返回 {win_rate, profit_rate, payoff_ratio, mean}；空序列全 None。
    """
    clean = [float(r) for r in rets if r is not None]
    if not clean:
        return {"win_rate": None, "profit_rate": None, "payoff_ratio": None, "mean": None}
    gains = [r for r in clean if r > threshold]
    losses = [r for r in clean if r <= threshold]
    loss_mean = (sum(losses) / len(losses)) if losses else 0.0
    payoff = ((sum(gains) / len(gains)) / abs(loss_mean)
              if gains and abs(loss_mean) > 1e-12 else None)
    return {
        "win_rate": sum(1 for r in clean if r > 0) / len(clean),
        "profit_rate": len(gains) / len(clean),
        "payoff_ratio": payoff,
        "mean": sum(clean) / len(clean),
    }


def compute_metrics_from_pairs(pairs: list[tuple[float, float]]) -> dict:
    """从 (预测分, 实现绝对收益) 序列计算质量指标（阶段5：赚钱口径）。

    profit_rate = 绝对收益 > 阈值(1%) 的占比（扣费后真赚钱）；
    payoff_ratio = 平均盈利 / 平均亏损绝对值（盈亏比）；
    ic 保留为辅助（预测分与实现绝对收益的秩相关）。样本 <2 时 IC 为 None。
    """
    if not pairs:
        return {"ic": None, "excess_win_rate": None, "mean_excess": None,
                "cum_excess": 0.0, "profit_rate": None, "mean_abs_ret": None,
                "payoff_ratio": None, "sample_count": 0}
    scores = [p[0] for p in pairs]
    rets = [p[1] for p in pairs]
    ic = spearman(scores, rets) if len(pairs) >= 2 else None
    if ic is not None and np.isnan(ic):
        ic = None  # 常数序列无秩相关，视为不可用
    ps = profit_stats(rets)  # 赚钱口径单一来源（名义胜率/赚钱胜率/盈亏比）
    return {
        "ic": round(ic, 4) if ic is not None else None,
        "excess_win_rate": round(ps["win_rate"], 4),
        "mean_excess": round(ps["mean"], 6),
        "cum_excess": round(ps["mean"] * len(rets), 6),
        "profit_rate": round(ps["profit_rate"], 4),
        "mean_abs_ret": round(ps["mean"], 6),
        "payoff_ratio": round(ps["payoff_ratio"], 3) if ps["payoff_ratio"] is not None else None,
        "sample_count": len(pairs),
    }


def compute_quality_metrics(period_start: str, period_end: str) -> dict:
    """统计区间内推荐的 40 日实际绝对收益并计算质量指标（阶段5：赚钱口径）。

    对每条推荐取入场后 41 条净值（含入场日），用第 0 与第 40 条计算基金绝对收益
    （end_nav / start_nav - 1，不再减指数——与训练目标/回测主标尺同口径）。
    数据不足（净值 <41 条）的样本跳过。
    全部读取经 repo 统一数据 seam（推荐决策域 read），可独立单测。
    """
    rows = repo.get_quality_sample_rows(period_start, period_end)

    pairs: list[tuple[float, float]] = []
    pairs_by_path: dict[str, list[tuple[float, float]]] = {}
    points: list[dict] = []
    decision_losses: list[float] = []
    gaps_best: list[float] = []
    for code, reco_date, score, candidate_codes, reco_path in rows:
        # 绝对收益口径（单一来源：repo.nav.forward_return，与结算/训练样本/回测一致）；
        # 窗口不足/净值异常返回 None，样本跳过
        abs_ret = repo.nav.forward_return(code, reco_date)
        if abs_ret is None:
            continue
        if not np.isfinite(abs_ret):
            continue
        # Q5 裁决损耗：LLM 选中基金 vs 候选池均值（回查候选 40 日收益，排除选中基金自比）；
        # P1-4 回滚后扩展：同时算选中 vs 候选池最优（combo 最高的候选，若其净值可查）——
        # 回答"LLM 是否不如纯量化最优"，与均值口径同一套月度样本，零新增表。
        decision_loss = None
        decision_gap_best = None
        if candidate_codes:
            cand_rets = []
            for cc in json.loads(candidate_codes):
                if cc == code:
                    continue
                # 40 日绝对收益单一来源（架构深化 C）：与结算/反事实同口径
                cr = repo.nav.forward_return(cc, reco_date)
                if cr is not None and np.isfinite(cr):
                    cand_rets.append(cr)
            if cand_rets:
                cand_mean = sum(cand_rets) / len(cand_rets)
                decision_loss = abs_ret - cand_mean
                decision_losses.append(decision_loss)
                cand_best = max(cand_rets)
                decision_gap_best = abs_ret - cand_best
                gaps_best.append(decision_gap_best)
        pairs.append((float(score), abs_ret))
        pairs_by_path.setdefault(reco_path or "sector", []).append((float(score), abs_ret))
        points.append({"date": reco_date, "code": code, "reco_path": reco_path or "sector",
                       "score": round(float(score), 6), "abs_ret": round(abs_ret, 6),
                       "decision_loss": round(decision_loss, 6) if decision_loss is not None else None,
                       "decision_gap_best": round(decision_gap_best, 6) if decision_gap_best is not None else None})

    metrics = compute_metrics_from_pairs(pairs)
    # 分口径度量：按 reco_path（sector 主路径 / degrade 降级路径）分组评估赚钱质量差异
    metrics["by_path"] = {
        p: compute_metrics_from_pairs(ps) for p, ps in pairs_by_path.items()
    }
    # 分桶赚钱率（模型校准观测 #1）：按预测分分桶统计真实赚钱率，验证 L1 回归
    # （条件中位数分）与 profit_rate 胜率口径的错位程度——若 score∈[1%,2%) 的真实
    # 胜率显著低于目标（如 <50%），说明中位数>1%≠大概率赚钱，再决策是否改二分类目标。
    bucket_stats: dict[str, dict] = {}
    for s, r in pairs:
        b = bucket_stats.setdefault(_score_bucket(s), {"sample_count": 0, "profit_count": 0})
        b["sample_count"] += 1
        if r > domain.PROFIT_THRESHOLD:
            b["profit_count"] += 1
    metrics["by_score_bucket"] = {
        b: {"sample_count": d["sample_count"],
            "profit_rate": round(d["profit_count"] / d["sample_count"], 6)}
        for b, d in bucket_stats.items()
    }
    metrics["decision_loss"] = (round(sum(decision_losses) / len(decision_losses), 6)
                                 if decision_losses else None)
    metrics["decision_gap_best"] = (round(sum(gaps_best) / len(gaps_best), 6)
                                     if gaps_best else None)
    # 累计收益曲线：按时间序累加
    cum = 0.0
    for p in points:
        cum += p["abs_ret"]
        p["cum_abs_ret"] = round(cum, 6)
    metrics["points"] = points
    metrics["period_start"] = period_start
    metrics["period_end"] = period_end
    logger.info("推荐质量度量: 区间 %s~%s, 样本 %d 条, 赚钱胜率=%s",
                period_start, period_end, metrics["sample_count"],
                metrics.get("profit_rate"))
    return metrics


def _score_bucket(score: float) -> str:
    """预测分分桶（模型校准观测 #1）：L1 回归分与真实赚钱率的对齐观测。"""
    if score < 0:
        return "<0%"
    if score < 0.01:
        return "0-1%"
    if score < 0.02:
        return "1-2%"
    if score < 0.05:
        return "2-5%"
    return ">=5%"


def _hold_days(start: str, end: str) -> int:
    """持有自然日（exit - entry）；解析失败按 30 日档兑底（避免费用错配）。"""
    try:
        s = datetime.strptime(start, "%Y-%m-%d")
        e = datetime.strptime(end, "%Y-%m-%d")
        return max(0, (e - s).days)
    except (TypeError, ValueError):
        return 30


def compute_e2e_metrics(period_start: str, period_end: str) -> dict:
    """端到端 P&L 度量（#2）：按实际退出日期算净收益（扣赎回费），对比 40 日理论收益。

    口径：
    - EXIT：gross = return_rate（监控平仓时写入 = 退出净值/入场净值 - 1）；
    - 持有中：gross = 最新净值/入场净值 - 1；
    - e2e_ret = gross - 赎回费（按持有自然日分段）；
    - timing_contribution = mean(e2e_ret) - mean(forward_return)：监控择时的真实贡献——
      正 = 提前退出整体赚了（止损保命），负 = 砍掉了本会回本的持仓（择时帮倒忙）。

    理论收益与 quality.forward_return 同源（repo.nav.forward_return 单一来源）。
    """
    rows = repo.get_e2e_sample_rows(period_start, period_end)
    e2e_rets: list[float] = []
    theo_rets: list[float] = []
    points: list[dict] = []
    today = datetime.now().strftime("%Y-%m-%d")

    for code, reco_date, entry_nav, status, exit_date, return_rate in rows:
        if not entry_nav or entry_nav <= 0:
            continue
        # 理论 40 日收益（与质量度量同源；窗口不足/异常返回 None → 跳过）
        theo = repo.nav.forward_return(code, reco_date)
        if theo is None:
            continue
        # 实际退出收益
        if status == domain.SIGNAL_EXIT and return_rate is not None:
            gross = return_rate
            end_day = exit_date or today
        else:
            latest = repo.nav.latest(code)
            if not latest or latest <= 0:
                continue
            gross = latest / entry_nav - 1.0
            end_day = today
        hold_days = _hold_days(reco_date, end_day)
        e2e = gross - redemption_fee_pct(hold_days) / 100.0
        e2e_rets.append(e2e)
        theo_rets.append(theo)
        points.append({"code": code, "reco_date": reco_date, "status": status,
                       "e2e_ret": round(e2e, 6), "theo_ret": round(theo, 6),
                       "hold_days": hold_days})

    ps = profit_stats(e2e_rets)
    timing = ((sum(e2e_rets) / len(e2e_rets)) - (sum(theo_rets) / len(theo_rets))
              if e2e_rets else None)
    return {
        "e2e_profit_rate": ps["profit_rate"],
        "e2e_mean_ret": ps["mean"],
        "e2e_payoff_ratio": ps["payoff_ratio"],
        "timing_contribution": round(timing, 6) if timing is not None else None,
        "e2e_sample_count": len(e2e_rets),
        "e2e_points": points,
    }

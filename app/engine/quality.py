"""推荐质量度量模块：赚钱胜率、期望绝对收益、盈亏比（阶段5 赚钱口径）。

度量回答"进化后推荐质量是否提升"：profit_rate = 推荐后 20 日绝对收益 > 1%
（覆盖申赎成本）的占比；mean_abs_ret = 期望绝对收益；payoff_ratio = 盈亏比。
IC（预测分与实现收益的秩相关）保留为排序能力辅助指标。

IC 的 Spearman 秩相关用 numpy 手写（含并列平均秩），不依赖 scipy。
"""

import json

import numpy as np

from app import domain
from app import repo
from app.utils.log import get_logger

logger = get_logger("quality")


def _rankdata(x: np.ndarray) -> np.ndarray:
    """计算平均秩（tie 取平均），与 scipy.stats.rankdata(average) 一致。"""
    x = np.asarray(x, dtype=float)
    n = x.size
    sorter = np.argsort(x, kind="stable")
    inv = np.empty(n, dtype=np.intp)
    inv[sorter] = np.arange(n)
    sx = x[sorter]
    obs = np.concatenate(([True], sx[1:] != sx[:-1]))
    dense = obs.cumsum()[inv]
    group_idx = np.flatnonzero(obs)
    counts = np.diff(np.concatenate((group_idx, [n])))
    avg_rank = group_idx + 1 + 0.5 * (counts - 1)
    return avg_rank[dense - 1]


def spearman(x, y) -> float | None:
    """Spearman 秩相关；常数序列（无秩差异）返回 None。"""
    rx = _rankdata(np.asarray(x, dtype=float))
    ry = _rankdata(np.asarray(y, dtype=float))
    with np.errstate(invalid="ignore", divide="ignore"):
        corr = np.corrcoef(rx, ry)[0, 1]
    return float(corr) if not np.isnan(corr) else None


def profit_stats(rets, threshold=domain.PROFIT_THRESHOLD) -> dict:
    """对收益序列算赚钱口径（单一来源）：名义胜率 / 赚钱胜率 / 盈亏比。

    赚钱 = 绝对收益 > threshold（覆盖申赎成本）。quality 度量与回测汇总共用，
    序列与阈值单位一致即可（小数序列配 0.01，百分数序列配 1.0）。
    返回 {win_rate, profit_rate, payoff_ratio, mean}；空序列全 None。
    """
    rets = [float(r) for r in rets if r is not None]
    if not rets:
        return {"win_rate": None, "profit_rate": None, "payoff_ratio": None, "mean": None}
    gains = [r for r in rets if r > threshold]
    losses = [r for r in rets if r <= threshold]
    loss_mean = (sum(losses) / len(losses)) if losses else 0.0
    payoff = ((sum(gains) / len(gains)) / abs(loss_mean)
              if gains and abs(loss_mean) > 1e-12 else None)
    return {
        "win_rate": sum(1 for r in rets if r > 0) / len(rets),
        "profit_rate": len(gains) / len(rets),
        "payoff_ratio": payoff,
        "mean": sum(rets) / len(rets),
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
    """统计区间内推荐的 20 日实际绝对收益并计算质量指标（阶段5：赚钱口径）。

    对每条推荐取入场后 21 条净值（含入场日），用第 0 与第 20 条计算基金绝对收益
    （end_nav / start_nav - 1，不再减指数——与训练目标/回测主标尺同口径）。
    数据不足（净值 <21 条）的样本跳过。
    全部读取经 repo 统一数据 seam（推荐决策域 read），可独立单测。
    """
    rows = repo.get_quality_sample_rows(period_start, period_end)

    pairs: list[tuple[float, float]] = []
    points: list[dict] = []
    decision_losses: list[float] = []
    gaps_best: list[float] = []
    for code, reco_date, score, candidate_codes in rows:
        # 绝对收益口径（单一来源：repo.nav.forward_return，与结算/训练样本/回测一致）；
        # 窗口不足/净值异常返回 None，样本跳过
        abs_ret = repo.nav.forward_return(code, reco_date)
        if abs_ret is None:
            continue
        if not np.isfinite(abs_ret):
            continue
        # Q5 裁决损耗：LLM 选中基金 vs 候选池均值（回查候选 20 日收益，排除选中基金自比）；
        # P1-4 回滚后扩展：同时算选中 vs 候选池最优（combo 最高的候选，若其净值可查）——
        # 回答"LLM 是否不如纯量化最优"，与均值口径同一套月度样本，零新增表。
        decision_loss = None
        decision_gap_best = None
        if candidate_codes:
            cand_rets = []
            for cc in json.loads(candidate_codes):
                if cc == code:
                    continue
                cand_navs = repo.nav.series(cc, since=reco_date, limit=domain.FORWARD_DAYS + 1)
                if len(cand_navs) < domain.FORWARD_DAYS + 1:
                    continue
                cs, ce = cand_navs[0][1], cand_navs[domain.FORWARD_DAYS][1]
                if cs and ce and cs > 0:
                    cr = ce / cs - 1.0
                    if np.isfinite(cr):
                        cand_rets.append(cr)
            if cand_rets:
                cand_mean = sum(cand_rets) / len(cand_rets)
                decision_loss = abs_ret - cand_mean
                decision_losses.append(decision_loss)
                cand_best = max(cand_rets)
                decision_gap_best = abs_ret - cand_best
                gaps_best.append(decision_gap_best)
        pairs.append((float(score), abs_ret))
        points.append({"date": reco_date, "code": code,
                       "abs_ret": round(abs_ret, 6),
                       "decision_loss": round(decision_loss, 6) if decision_loss is not None else None,
                       "decision_gap_best": round(decision_gap_best, 6) if decision_gap_best is not None else None})

    metrics = compute_metrics_from_pairs(pairs)
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

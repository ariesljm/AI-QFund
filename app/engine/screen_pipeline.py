"""票 11：模块一全市场初筛管线。

买池（主动权益）→ 硬过滤（facts）→ 特征（最新快照）→ 打分 → Top30 落库
（保留特征快照，供审计与复盘）。

打分：简单多因子截面排序（sharpe_60d + mom_250d + ttr_60d 反向，等权百分位），
2026-09-17 回测确认优于 15 维 LGBM（docs/backtest/120d-multifactor-rebuild.md）。
scorer 可注入（测试用 fake）；空态区分：无候选 → no_opportunity；有池但打分
全无 → data_failure（与票 25 闸门语义一致）。
"""

import numpy as np

from app.engine.screen import active_equity_pool, apply_hard_filters, top_n
from app.repo import decision as decision_repo
from app.repo.base import get_fund_basics, get_latest_features_batch, get_restriction_facts


def multifactor_scores(feats: dict[str, dict]) -> dict[str, float]:
    """简单多因子截面打分：sharpe_60d + mom_250d + ttr_60d（反向）等权百分位。

    对同一截面（全市场候选）做 rank 归一化，返回 {code: score(0~1)}。
    缺省值：sharpe/mom 缺失 → 0.0（中性）；ttr 缺失 → 60.0（差）。
    """
    codes = [c for c in feats if feats[c]]
    n = len(codes)
    if n == 0:
        return {}

    def _rank(x: np.ndarray) -> np.ndarray:
        return np.argsort(np.argsort(x)) / (n - 1) if n > 1 else np.zeros_like(x)

    sharpe = np.array([feats[c].get("sharpe_60d") or 0.0 for c in codes], dtype=float)
    mom = np.array([feats[c].get("mom_250d") or 0.0 for c in codes], dtype=float)
    ttr = np.array([feats[c].get("ttr_60d")
                    if feats[c].get("ttr_60d") is not None else 60.0
                    for c in codes], dtype=float)
    score = (_rank(sharpe) + _rank(mom) + (1.0 - _rank(ttr))) / 3.0
    return {c: float(s) for c, s in zip(codes, score, strict=True)}


def screen_top30(today: str, scorer=None, limit: int = 30) -> dict:
    """全市场初筛 → Top30 候选池落库。

    scorer: 逐只打分函数（features: dict -> float | None），测试注入用；
    缺省用 multifactor_scores（截面多因子排序，生产）。
    返回 {"date", "count", "codes"}；空态返回 {"date", "empty": reason}。
    """
    basics = get_fund_basics()
    pool = active_equity_pool([{"code": c, "type": t} for c, t in basics])
    if not pool:
        return {"date": today, "empty": "no_opportunity"}
    facts = get_restriction_facts(pool)
    pool = apply_hard_filters(pool, facts)
    # N+1 收敛：一次查询全部最新特征（全市场 12,900 只不可逐只查）
    feats = get_latest_features_batch(pool)
    if scorer is not None:
        ranked: list[dict] = []
        for c in pool:
            feat = feats.get(c)
            if not feat:
                continue
            sc = scorer(feat)
            if sc is None:
                continue
            ranked.append({"code": c, "score": sc, "features": feat})
    else:
        scores = multifactor_scores(feats)
        ranked = [{"code": c, "score": scores[c], "features": feats[c]}
                  for c in pool if c in scores]
    if not ranked:
        return {"date": today, "empty": "data_failure"}
    top = top_n(ranked, limit)
    decision_repo.save_screen_candidates(today, top)
    return {"date": today, "count": len(top),
            "codes": [t["code"] for t in top]}


def is_no_opportunity(result: dict) -> bool:
    """空态语义判定（票 11）：no_opportunity（市场判断）vs 其他。"""
    return result.get("empty") == "no_opportunity"


def is_data_failure(result: dict) -> bool:
    """data_failure（数据故障）：有池但打分/特征全部缺失。"""
    return result.get("empty") == "data_failure"

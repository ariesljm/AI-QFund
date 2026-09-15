"""票 11：模块一全市场初筛管线（接线骨架）。

买池（主动权益）→ 硬过滤（facts）→ 特征（最新快照）→ 打分 → Top30 落库
（保留特征快照，供审计与复盘）。scorer 可注入（测试用 fake，生产默认
model.score）。空态区分：无候选 → "no_opportunity"；有池但打分全无 →
"data_failure"（数据故障，与票 25 闸门语义一致）。

2.0 特征（票 08）与超额标签模型（票 09）回填完成后替换特征/打分来源，
管线外壳不变。
"""

from app import model
from app.engine.screen import active_equity_pool, apply_hard_filters, top_n
from app.repo import decision as decision_repo
from app.repo.base import get_fund_basics, get_latest_features_batch, get_restriction_facts


def screen_top30(today: str, scorer=None, limit: int = 30) -> dict:
    """全市场初筛 → Top30 候选池落库。

    scorer: 打分函数（features: dict -> float | None）；缺省用 model.score。
    返回 {"date", "count", "codes"}；空态返回 {"date", "empty": reason}。
    """
    basics = get_fund_basics()
    pool = active_equity_pool([{"code": c, "type": t} for c, t in basics])
    if not pool:
        return {"date": today, "empty": "no_opportunity"}
    facts = get_restriction_facts(pool)
    pool = apply_hard_filters(pool, facts)
    scorer = scorer or model.score
    # N+1 收敛：一次查询全部最新特征（全市场 12,900 只不可逐只查）
    feats = get_latest_features_batch(pool)
    ranked: list[dict] = []
    for c in pool:
        feat = feats.get(c)
        if not feat:
            continue
        sc = scorer(feat)
        if sc is None:
            continue
        ranked.append({"code": c, "score": sc, "features": feat})
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

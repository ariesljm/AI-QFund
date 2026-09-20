"""票 11：模块一全市场初筛引擎（纯函数核心）。

池子定义 / 硬过滤链组合 / Top30 截断——纯函数可测（替换 1.x 赛道漏斗）。
模型打分、Top30 落库、空推荐日语义的**接线**由决策周期入口负责
（数据回填后接入 recommend 主流程，见票 11 Comments）。

池子：主动权益 = 混合型 + 股票型（**剔指数型**）；同类组 n≥10 是主标尺
装配时的要求（benchmark.MIN_PEER_SAMPLES），不在池子层。
"""

from app.domain import hard_filter_violations

# 主动权益类型白名单（fund_basic.type 实测枚举：混合型/指数型/股票型）
ACTIVE_EQUITY_TYPES = {"混合型", "股票型"}
POOL_SIZE = 30


def active_equity_pool(funds: list[dict]) -> list[str]:
    """主动权益池：混合型 + 股票型（剔指数型）。

    funds: [{"code": str, "type": str}, ...]；未知类型剔除（不进池）。
    """
    return [f["code"] for f in funds if f.get("type") in ACTIVE_EQUITY_TYPES]


def apply_hard_filters(codes: list[str], facts: dict) -> list[str]:
    """硬过滤链（票 06 四条谓词组合）：任一违规即剔除。

    facts: {code: {"aum", "purchase_status", "daily_limit", "nav_count"}}。
    缺数据不误杀口径（票 06）：aum 缺失/状态未知不判违规；nav_count 缺失
    （无净值）判 short_history 违规（没净值历史的基金不进池）。
    """
    out: list[str] = []
    for c in codes:
        f = facts.get(c) or {}
        v = hard_filter_violations(f.get("aum"), f.get("purchase_status", "unknown"),
                                   f.get("daily_limit"), f.get("nav_count", 0))
        if not v:
            out.append(c)
    return out


def top_n(ranked: list[dict], n: int = POOL_SIZE) -> list[dict]:
    """按 score 降序取 TopN（Top30 候选池）。ranked: [{"code", "score", ...}]。"""
    return sorted(ranked, key=lambda x: x["score"], reverse=True)[:n]


def select_diversified(
    ranked: list[dict],
    corr_fn,
    peer_fn,
    max_corr: float = 0.85,
    max_same_peer: int = 2,
    n: int = 5,
) -> list[dict]:
    """贪心去相关 + 同类≤2 选 n 只（组合层约束，票 02）。

    ranked: [{"code","final_score",...}] 已按 final_score 降序。
    corr_fn(c1,c2) -> 相关性 | None（None/不可得 → 不约束相关性）。
    peer_fn(code) -> 同类标签 | None（RBSA 第一行业；None → 不约束同类）。
    规则：按分数降序依次纳入；与任一已选相关性 > max_corr、或同类已达
    max_same_peer 只 → 跳过；凑不齐 n → 回退按分数补满（不因约束导致推荐不足）。
    """
    selected: list[dict] = []
    peer_count: dict[str, int] = {}
    for cand in ranked:
        if len(selected) >= n:
            break
        code = cand["code"]
        peer = peer_fn(code)
        if peer is not None and peer_count.get(peer, 0) >= max_same_peer:
            continue
        if any(_over_corr(corr_fn(code, s["code"]), max_corr) for s in selected):
            continue
        selected.append(cand)
        if peer is not None:
            peer_count[peer] = peer_count.get(peer, 0) + 1
    if len(selected) < n:                      # 回退纯分数补满
        chosen = {c["code"] for c in selected}
        for cand in ranked:
            if len(selected) >= n:
                break
            if cand["code"] not in chosen:
                selected.append(cand)
                chosen.add(cand["code"])
    return selected[:n]


def _over_corr(c, threshold: float) -> bool:
    """相关性是否超阈（None/不可得 → False，不约束）。"""
    return c is not None and float(c) > threshold

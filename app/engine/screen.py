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

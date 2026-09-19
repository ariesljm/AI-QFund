"""信号数学纯函数（无 IO）：状态机信号判定核心。

supervise 做数据装配（净值/估值分位/脱轨/致命公告/止损），本模块只做
"给定序列 → 信号布尔/计数"。数据 adapter 与信号数学分离：测试直测此模块，
不碰数据 seam。
"""

import numpy as np

from app.features.calculator import _ema_series
from app.features.excess import daily_excess

# Alpha 连续负天数的观察阈值（relative_weak 子条件；唯一来源，勿在别处重复定义）
ALPHA_NEG_STREAK = 5
# 动量观察窗口（交易日）
MOMENTUM_DAYS = 5


def ema20_below(navs: list[float]) -> bool:
    """最新净值是否 < EMA20（跌破均线 → 离场信号）。"""
    if not navs or len(navs) < 20:
        return False                      # 序列不足，不判定
    ema = _ema_series(np.asarray(navs, dtype=float), span=20)
    return bool(navs[-1] < ema[-1])


def alpha_neg_streak(fund_navs: list[float], bench_navs: list[float],
                     n: int = ALPHA_NEG_STREAK) -> int:
    """重叠日相对基准的超额（fund_ret − bench_ret）连续负天数（最新往回数）。"""
    ex = daily_excess(fund_navs, bench_navs)
    overlap = len(ex)
    if overlap < n:
        return 0
    streak = 0
    for i in range(overlap - 1, 0, -1):
        if ex[i] < 0:
            streak += 1
        else:
            break
    return streak


def momentum_pos(fund_navs: list[float], window: int = MOMENTUM_DAYS) -> bool:
    """最近 window 个交易日累计收益 > 0（转正 = 非离场条件）。"""
    if len(fund_navs) < window + 1:
        return False
    seg = fund_navs[-(window + 1):]
    if seg[0] <= 0:
        return False
    return seg[-1] / seg[0] - 1.0 > 0


def valuation_high(weighted_pctile: float | None, high: float = 85.0) -> bool:
    """估值分位 ≥ 高阈值 → 观察信号。无分位不触发。"""
    return weighted_pctile is not None and weighted_pctile >= high

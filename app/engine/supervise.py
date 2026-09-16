"""2.0 每日监控接线（US22-28）：recommend_v2 推荐对象 → 信号装配 → 状态机转移落库。

7 信号源对照 spec 状态机（票 15）signals 结构：
- valuation_pctile：重仓股加权 PE 分位（票 05 weighted_valuation_percentile，PIT 口径）
- drifted：基金日收益 vs 沪深300 代理（票 16 drift_check 阈值，|corr|<0.40 / r2<0.25）
- below_ema20：最新净值 < EMA20（calculator._ema_series span=20，单一来源）
- alpha_neg_days：相对基准日超额连续负天数（>=5 → 触发观察）
- momentum_pos：最近 5 个重叠日收益和 > 0（配合极端估值 → 离场）
- fatal_news：重仓股致命负面公告（切片二数据源；收割成本高，本轮缺省
  False——审计层已覆盖风险提示，接入作为后续增强）
- 净值陈旧（stale）不入 signals：数据问题 ≠ 风险信号（沿 1.x 区分）

缺省安全：数据不足的信号不触发转移（None/False），宁缺勿误（状态机
本就不读 NULL——无信号 = HOLD 维持，不会误降级）。
"""

from typing import Any

import numpy as np

from app.engine.state_machine import apply_transition
from app.features.calculator import _ema_series

# Alpha 连续负天数的观察阈值（状态机默认 ALPHA_NEG_DAYS=5；此处允许接线层收紧）
ALPHA_NEG_STREAK = 5
# 动量观察窗口（交易日）
MOMENTUM_DAYS = 5
# 基准指数（代理同类；RBSA 行业 proxy 为后续增强）
BENCH_INDEX = "sh000300"


# ── 纯函数信号（可注入数据直测）─────────────────────────────

def ema20_below(navs: list[float]) -> bool:
    """最新净值是否 < EMA20（跌破均线 → 离场信号）。"""
    if not navs or len(navs) < 20:
        return False                      # 序列不足，不判定
    ema = _ema_series(np.asarray(navs, dtype=float), span=20)
    return bool(navs[-1] < ema[-1])


def daily_returns(navs: list[float]) -> list[float]:
    """净值序列 → 日收益序列（同长度首元素 0，与 1.x 口径一致）。"""
    out: list[float] = [0.0]
    for i in range(1, len(navs)):
        prev = navs[i - 1]
        out.append(float(navs[i] / prev - 1.0) if prev > 0 else 0.0)
    return out


def alpha_neg_streak(fund_navs: list[float], bench_navs: list[float],
                     n: int = 5) -> int:
    """重叠日相对基准的超额（fund_ret − bench_ret）连续负天数（最新往回数）。"""
    f = daily_returns(fund_navs)
    b = daily_returns(bench_navs)
    # 按日期对齐：两者取自同一交易日序列（ts 同日），直接取共同尾部
    overlap = min(len(f), len(b))
    if overlap < n:
        return 0
    streak = 0
    for i in range(overlap - 1, 0, -1):
        if f[i] - b[i] < 0:
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


# ── 数据装配（接线层）─────────────────────────────────────

def _fund_navs(code: str, days: int = 250) -> list[float] | None:
    """基金最近 days 交易日净值（升序）。"""
    from app.repo import nav
    rows = nav.series(code, until=None)
    if not rows:
        return None
    return [float(r[1]) for r in rows[-days:]]


def _index_navs(index: str = BENCH_INDEX, days: int = 250) -> list[float] | None:
    """指数最近 days 交易日收盘（升序，指数行 (date, close, volume)）。"""
    from app.repo.base import get_index_rows
    rows = get_index_rows(index)
    if not rows:
        return None
    return [float(r[1]) for r in rows[-days:]]


def _pe_pctile(code: str, limit: int = 10) -> float | None:
    """重仓股加权 PE 分位（PIT 口径：只看 disclosure_date <= 最新披露的持仓）。"""
    from app.features.valuation import weighted_valuation_percentile
    from app.repo.base import get_holdings, get_pe_histories
    holdings = get_holdings(code, limit)
    if not holdings:
        return None
    stock_codes = [h["stock_code"] for h in holdings if h.get("stock_code")]
    if not stock_codes:
        return None
    pe_histories = get_pe_histories(stock_codes)
    return weighted_valuation_percentile(holdings, pe_histories)


def build_signals(code: str, fund_navs: list[float] | None,
                  bench_navs: list[float] | None,
                  weighted_pctile: float | None = None,
                  fatal_news: bool = False) -> dict[str, Any]:
    """装配状态机 signals（缺省安全：数据不足的信号不触发，键固定可预测试）。

    drifted 缺省 False：宽基指数 proxy 对主动基金会系统性误报脱轨
    （风格偏离大盘的基金天然低相关）；RBSA 行业 proxy 为后续增强。
    """
    s: dict[str, Any] = {
        "drifted": False,
        "below_ema20": False,
        "alpha_neg_days": 0,
        "momentum_pos": False,
        "fatal_news": bool(fatal_news),
    }
    if fund_navs and bench_navs:
        s["below_ema20"] = ema20_below(fund_navs)
        s["alpha_neg_days"] = alpha_neg_streak(fund_navs, bench_navs)
        s["momentum_pos"] = momentum_pos(fund_navs)
    if weighted_pctile is not None:
        s["valuation_pctile"] = weighted_pctile
    return s


def run_supervision(date: str, limit: int = 50) -> dict:
    """每日监控接线：recommend_v2 全部推荐对象 → 装配信号 → 状态机转移落库。

    返回 {"tracked": n, "moved": {code: (old, new)}, "empty": bool}。
    无推荐对象 → 空（监控没有分母，正常静默）。
    """
    from app.repo import decision
    codes = decision.get_recommend_v2_codes(limit)
    if not codes:
        return {"tracked": 0, "moved": {}, "empty": True}
    bench_navs = _index_navs()
    moved: dict[str, tuple[str, str]] = {}
    n = 0
    for code in codes:
        fund_navs = _fund_navs(code)
        s = build_signals(code, fund_navs, bench_navs, _pe_pctile(code))
        old, new = apply_transition("fund", code, s, date)
        n += 1
        if old != new:
            moved[code] = (old, new)
    return {"tracked": n, "moved": moved, "empty": False}

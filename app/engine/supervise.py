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
from app.utils.log import get_logger

logger = get_logger("supervise")

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


def _drift_check(code: str) -> bool:
    """持仓虚拟组合脱轨检测（票 16）：基金日收益 vs 持仓虚拟组合日收益。

    数据不足（nav<20 / 无持仓 / 无个股日线）→ False（不误报，状态机按未知处理）。
    """
    from app.engine.drift import drift_check, proxy_returns, normalize_weights
    from app.repo.base import get_holdings, get_stock_daily
    from app.repo import nav as nav_repo
    nav_rows = nav_repo.series(code, until=None)
    nav_rows = nav_rows[-120:] if nav_rows else []
    if not nav_rows or len(nav_rows) < 20:
        return False
    fund_rets: dict[str, float] = {}
    prev = None
    for d, v in nav_rows:
        if prev is not None and prev > 0 and v:
            fund_rets[d] = v / prev - 1.0
        prev = v
    holdings = get_holdings(code, 10)
    weights = normalize_weights([{"stock_code": h["stock_code"], "weight": h["weight"]}
                                  for h in holdings if h.get("stock_code")])
    if not weights:
        return False
    stock_dailies = {c: get_stock_daily(c, 120) for c in weights}
    proxy = proxy_returns(weights, stock_dailies)
    if not proxy:
        return False
    r = drift_check(fund_rets, proxy)
    return bool(r and r.get("is_drifted"))


def _fatal_news(code: str) -> bool:
    """重仓股致命负面公告（事件风险，触发直通 EXIT）。

    fetch_announcements 成本高（东财 np-anotice API），取 top5 重仓 + 近 14 天控制；容错失败返回 False。
    """
    try:
        from app.data.announcements import fetch_announcements, is_negative
        from app.repo.base import get_holdings
        for h in get_holdings(code, 5):
            sc = h.get("stock_code")
            if not sc:
                continue
            for a in (fetch_announcements(sc, days=14) or []):
                if is_negative(a.get("title", "")):
                    return True
    except Exception:
        pass
    return False


def _drawdown_stop(code: str) -> bool:
    """推荐日至今最大峰谷回撤 > 8% → 止损（保护本金，触发直通 EXIT）。"""
    try:
        from app.repo.recommend_log import get_fund_detail
        from app.repo.nav import series as nav_series
        from app.engine.valuation import max_drawdown
        first = (get_fund_detail(code) or {}).get("first_date")
        if not first:
            return False
        navs = nav_series(code, until=None)
        pts = [r[1] for r in navs if r[0] >= first]
        if len(pts) < 2 or not pts[0]:
            return False
        return max_drawdown([p / pts[0] * 100 for p in pts]) > 8.0
    except Exception:
        return False


def build_signals(code: str, fund_navs: list[float] | None,
                  bench_navs: list[float] | None,
                  weighted_pctile: float | None = None,
                  fatal_news: bool = False,
                  drifted: bool = False,
                  drawdown_stop: bool = False) -> dict[str, Any]:
    """装配状态机 signals（缺省安全：数据不足的信号不触发，键固定可预测试）。

    relative_weak 合并 drifted（持仓虚拟组合脱轨）与 alpha_neg 连续负（前兆）为单一相对弱势维度——
    去冗余：一个维度只记一路账（calibration）。drifted/alpha_neg_days 仍保留供溯源但不独立触发。
    """
    s: dict[str, Any] = {
        "relative_weak": False,
        "below_ema20": False,
        "alpha_neg_days": 0,
        "momentum_pos": False,
        "fatal_news": bool(fatal_news),
        "drawdown_stop": bool(drawdown_stop),
    }
    if fund_navs and bench_navs:
        s["below_ema20"] = ema20_below(fund_navs)
        s["alpha_neg_days"] = alpha_neg_streak(fund_navs, bench_navs)
        s["momentum_pos"] = momentum_pos(fund_navs)
    # relative_weak：持仓脱轨 OR 超额连续负（前兆）——合并为单一相对弱势维度
    if drifted or (s.get("alpha_neg_days", 0) >= ALPHA_NEG_STREAK):
        s["relative_weak"] = True
    if weighted_pctile is not None:
        s["valuation_pctile"] = weighted_pctile
    return s


def assemble_signals(code: str, bench_navs: list[float] | None = None) -> dict[str, Any]:
    """跟踪对象 → signals 装配 seam：净值窗口 / PE 分位 / 纯信号 / 脱轨 / 致命公告 / 止损一次性收口。

    drifted 由持仓虚拟组合 proxy 计算（_drift_check）；fatal_news 由重仓公告判定（_fatal_news）；
    drawdown_stop 由推荐日至今回撤判定（_drawdown_stop）。均数据不足 False 不误报。
    """
    fund_navs = _fund_navs(code)
    drifted = _drift_check(code)
    fatal = _fatal_news(code)
    dd_stop = _drawdown_stop(code)
    return build_signals(code, fund_navs, bench_navs, _pe_pctile(code),
                         fatal_news=fatal, drifted=drifted, drawdown_stop=dd_stop)


def run_supervision(date: str, limit: int = 50, cid: str = "") -> dict:
    """每日监控接线：recommend_v2 全部推荐对象 → 装配信号 → 状态机转移落库。

    返回 {"tracked": n, "moved": {code: (old, new)}, "empty": bool}。
    无推荐对象 → 空（监控没有分母，正常静默）。
    cid: correlation id，贯穿 pipeline，供 web 报告按批次聚合。
    """
    log = logger.with_cid(cid)
    from app.repo import decision
    codes = decision.get_recommend_v2_codes(limit)
    if not codes:
        log.info_event("supervise_empty", "无推荐对象，监控空跑")
        return {"tracked": 0, "moved": {}, "empty": True}
    bench_navs = _index_navs()
    moved: dict[str, tuple[str, str]] = {}
    n = 0
    for code in codes:
        s = assemble_signals(code, bench_navs)
        old, new = apply_transition("fund", code, s, date)
        n += 1
        if old != new:
            moved[code] = (old, new)
            triggers = []
            sig_ids = []
            if s.get("below_ema20"): triggers.append("跌破EMA20"); sig_ids.append("below_ema20")
            if s.get("relative_weak"): triggers.append("相对弱势"); sig_ids.append("relative_weak")
            if s.get("valuation_pctile") is not None and s["valuation_pctile"] >= 85: triggers.append(f"估值{s['valuation_pctile']:.0f}分位"); sig_ids.append("valuation_high")
            if s.get("fatal_news"): triggers.append("致命公告"); sig_ids.append("fatal_news")
            if s.get("drawdown_stop"): triggers.append("止损8%"); sig_ids.append("drawdown_stop")
            # 校准层记账：触发信号落 signal_outcomes，evolve T+40 按超额<0 结算命中
            from app.repo.tracked_state import record_signal_trigger
            for sid in sig_ids:
                record_signal_trigger(sid, date, code)
            log.info_event("state_transition", f"{code} {old}→{new} 触发：{','.join(triggers) or '阈值'}",
                           extra={"code": code, "old": old, "new": new, "triggers": triggers})
    log.info_event("supervise_done", f"监控 {n} 只对象，{len(moved)} 只状态转移",
                   extra={"tracked": n, "moved_count": len(moved), "moved": {c: f"{o}>{n2}" for c, (o, n2) in moved.items()}})
    return {"tracked": n, "moved": moved, "empty": False}

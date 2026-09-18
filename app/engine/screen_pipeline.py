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


# ── 推荐前数据新鲜度闸门（审计 P1-2 扩展）────────────────
# 与 mark_stale_funds（相对全局滞后打标）互补：后者只抓"个别基金停更"，
# 抓不到"全局停更"——净值下载整体失败时所有基金同停 T-2，相对滞后=0
# 打标失效，但数据已陈旧，推荐会照常用陈旧特征跑（pipeline 数据槽位失败
# 不阻断推荐槽位，恰放大了这个洞）。本闸门在推荐槽位入口拦一道。


def check_data_freshness(today: str | None = None, max_lag: int = 3) -> tuple[bool, str]:
    """推荐前数据新鲜度检查。返回 (ok, reason)。

    ok=False 时调用方应把推荐标记为 data_failure（数据停更，非市场判断）。
    检查：净值全局最新日期、指数（sh000300）最新日期是否滞后期望交易日
    超过 max_lag（默认 3 个交易日，与 foundation._check_index_freshness 同口径）。
    无交易日历缓存时返回 ok=True（不误报，与指数新鲜度核查同口径）。
    """
    from app.repo.base import get_index_rows, get_nav_time_state
    from app.utils.trading_calendar import expected_trade_date, trading_day_lag

    expected = expected_trade_date(today)
    if expected is None:
        return True, ""

    ranges, dates = get_nav_time_state()
    if not ranges:
        return False, "无净值数据（fund_nav 空）"
    global_max = max((v[1] for v in ranges.values() if v[1]), default="")
    if not global_max:
        return False, "无净值数据（fund_nav 空）"
    nav_lag = trading_day_lag(global_max, expected, days=set(dates))
    if nav_lag > max_lag:
        return False, f"净值全局停更（最新 {global_max}，滞后期望交易日 {nav_lag} 天 > {max_lag}）"

    rows = get_index_rows("sh000300")
    if not rows:
        return False, "无指数数据（index_daily 缺 sh000300）"
    idx_latest = rows[-1][0]
    idx_lag = trading_day_lag(idx_latest, expected)
    if idx_lag > max_lag:
        return False, f"指数停更（最新 {idx_latest}，滞后期望交易日 {idx_lag} 天 > {max_lag}）"

    return True, ""

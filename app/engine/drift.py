"""虚拟组合脱轨检测（票 16）：虚拟组合 vs 实际净值的滚动拟合偏离度。

接缝：无——归一化、组合收益、相关性/R² 都是纯函数（含零方差退化与缺失值
处置边界），构造序列直测。数据装配（get_holdings as_of / get_stock_daily）
由状态机（票 15）接线。

口径（票 16/2.0 §4.1 参考实现纠偏）：
- 不去用 np.corrcoef：输入含 NaN 时它返回 NaN 而不报错，静默污染下游。
  这里显式处置——任一值缺失/NaN → 该项判 0；零方差（std==0）→ r2=0。
- 停牌日对齐：某日仅部分股票有收益 → 权重在可用子集重新归一化；
  当日无任何股票有收益 → 该日跳过（不产生 0 收益的伪样本）。
"""

import math

from app.domain import adjusted_returns

# 阈值（票 16）：corr < 0.40 或 r2 < 0.25 → 脱轨。配置化归票 15 统一做。
DRIFT_CORR_THRESHOLD = 0.40
DRIFT_R2_THRESHOLD = 0.25
ROLLING_WINDOW = 15


def normalize_weights(holdings: list[dict]) -> dict[str, float]:
    """Top-N 持仓权重归一化：{code: w/Σw}（Σ=1）。

    空 / 全部非正权重 / 无 code → {}（调用方回退"无法构建虚拟组合"）。
    """
    total = sum(float(h.get("weight") or 0.0) for h in holdings)
    if total <= 0:
        return {}
    out: dict[str, float] = {}
    for h in holdings:
        code = h.get("stock_code")
        w = float(h.get("weight") or 0.0)
        if code and w > 0:
            out[code] = w / total
    return out


def proxy_returns(weights: dict[str, float],
                  stock_dailies: dict[str, dict[str, float]]) -> dict[str, float]:
    """虚拟组合日收益 {date: ret}。

    weights: {code: 归一化权重}；stock_dailies: {code: {date: 前复权收盘价}}。
    某日部分股票停牌（无数据）→ 权重在可用子集重新归一化（除以可用权重和）；
    当日无任何可用股票 → 该日跳过。
    """
    rets: dict[str, dict[str, float]] = {}
    for code, closes in stock_dailies.items():
        w = weights.get(code)
        if w and w > 0 and closes:
            rets[code] = adjusted_returns(closes)
    if not rets:
        return {}
    dates = sorted({d for r in rets.values() for d in r})
    out: dict[str, float] = {}
    for d in dates:
        num = den = 0.0
        for code, r in rets.items():
            if d in r:
                num += weights[code] * r[d]
                den += weights[code]
        if den > 0:
            out[d] = num / den
    return out


def corr_r2(x: list[float], y: list[float]) -> tuple[float, float]:
    """两个等长序列的 Pearson 相关与 R²。

    显式退化处置：长度 < 2 / 任一 NaN / 任一零方差 → (0.0, 0.0)——不抛异常、
    不产生 NaN（对照 np.corrcoef 的静默 NaN 污染）。
    """
    if len(x) != len(y) or len(x) < 2:
        return 0.0, 0.0
    if any(v is None for v in x + y) or any(
            isinstance(v, float) and math.isnan(v) for v in x + y):
        return 0.0, 0.0
    n = len(x)
    mx, my = sum(x) / n, sum(y) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx == 0 or syy == 0:
        return 0.0, 0.0
    corr = sxy / math.sqrt(sxx * syy)
    corr = max(-1.0, min(1.0, corr))  # 浮点截断
    return corr, corr * corr


def drift_check(fund_rets: dict[str, float], proxy_rets: dict[str, float],
                window: int = ROLLING_WINDOW) -> dict | None:
    """最近 window 个重叠交易日的脱轨判定。

    返回 {"corr", "r2", "is_drifted", "samples"}；重叠样本 < window → None
    （数据不足不判定，由状态机按"未知"处理，不误报）。
    is_drifted = corr < 0.40 or r2 < 0.25（票 16 阈值）。
    """
    overlap = sorted(set(fund_rets) & set(proxy_rets))
    if len(overlap) < window:
        return None
    days = overlap[-window:]
    corr, r2 = corr_r2([fund_rets[d] for d in days], [proxy_rets[d] for d in days])
    # |corr| < 0.40：脱轨是“拟合弱”，完美负相关（corr=-1, r2=1）是强关系
    # （β<0 反向复制），不是弱拟合——票单字面 corr<0.40 会误判，已修
    return {"corr": corr, "r2": r2,
            "is_drifted": abs(corr) < DRIFT_CORR_THRESHOLD or r2 < DRIFT_R2_THRESHOLD,
            "samples": window}

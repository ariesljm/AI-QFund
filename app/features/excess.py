"""超额收益纯函数（无 IO）：基金 vs 基准的日收益/日超额数学核心。

消费方各自聚合：
- supervise.alpha_neg_streak：日超额连续负天数（最新往回数）；
- evolve._excess_negative：窗口累计超额 < 0；
- valuation.alpha_series / dashboard.alpha_block：累计超额曲线。

本模块只提供"日收益/日超额"数学核心，不碰数据获取（nav/index 由调用方取）。
"""


def daily_returns(navs: list[float]) -> list[float]:
    """净值序列 → 日收益序列（同长度首元素 0，与 1.x 口径一致）。"""
    out: list[float] = [0.0]
    for i in range(1, len(navs)):
        prev = navs[i - 1]
        out.append(float(navs[i] / prev - 1.0) if prev > 0 else 0.0)
    return out


def daily_excess(fund_navs: list[float], bench_navs: list[float]) -> list[float]:
    """日超额序列（fund_ret − bench_ret），按重叠尾部对齐（两者同交易日序列）。"""
    f = daily_returns(fund_navs)
    b = daily_returns(bench_navs)
    overlap = min(len(f), len(b))
    return [f[i] - b[i] for i in range(overlap)]

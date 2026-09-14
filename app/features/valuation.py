"""个股估值派生特征：重仓股加权 PE 分位（票 05）。

纯函数，不碰 IO：入参是已取好的持仓权重与各股票 PE 历史序列。
接缝（票单）：分位与加权都是纯函数——复用 `domain.percentile_of` 单一来源，
不另写分位实现。

PEG 匹配度公式与数据源尚未定（见票 05 Comments），暂不实现，避免拍脑袋。
"""

from app.domain import percentile_of


def weighted_valuation_percentile(
    holdings: list[dict],
    pe_histories: dict[str, list[float]],
) -> float | None:
    """重仓股加权 PE 分位（0~100）。

    对每只可见持仓股票，取当前 PE 在自身历史（含当前日，末尾即当前值）中的
    分位，再按季报持仓权重加权平均。权重归一化到**有估值**的持仓子集：缺失
    估值的股票从分子分母一并剔除——把缺失当 0 会系统性压低分位，属于静默失真。

    holdings: [{"stock_code": str, "weight": float}, ...]；调用方已按 PIT
              （disclosure_date <= d）过滤后传入。
    pe_histories: stock_code -> 该股 PE 历史序列（末位 = 当前日）。
    全部缺失/无正权重 → None（调用方回退缺省特征值）。
    """
    num = 0.0
    den = 0.0
    for h in holdings:
        code = h.get("stock_code")
        hist = pe_histories.get(code) if code else None
        if not hist:
            continue
        w = float(h.get("weight") or 0.0)
        if w <= 0:
            continue
        num += w * percentile_of(hist, hist[-1])
        den += w
    return num / den if den > 0 else None

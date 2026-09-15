"""票 08：2.0 特征集（三个维度，纯函数直测）。

维度：
- **反追高与过热**：动量加速度 `R_1m/(R_6m/6)`、均线乖离率 `Bias_60`、份额激增度 `AUM_surge`
- **底层性价比**：重仓股加权 PE 分位（复用票 05 `features.valuation`）、PEG 匹配度 = 加权 PE / 加权增速
- **收益质量**：上下行捕获比（复用 1.x 口径）、回撤修复周期

每个特征纯函数不碰 IO，退化路径显式（零方差 / 样本不足 / 除零 → None 或中性 1.0）。

**接入时机**：本模块与 1.x 特征集先并存；数据回填（04/05/06）完成后，
由 calculator.py 按 2.0 维度重组特征流水线并升级 `domain.FEATURE_COLS`
（见票 08 Comments：capture_up/capture_down 保留重组，其余逐项评估）。
"""


def momentum_acceleration(r_1m: float, r_6m: float) -> float | None:
    """动量加速度 R_1m / (R_6m/6)：近 1 月相对近 6 月平均月度的热度。

    R_6m <= 0 → None（基数为负/零时比值无意义，过热无从度量）。
    """
    if r_6m <= 0:
        return None
    return r_1m / (r_6m / 6.0)


def bias_60(close: float, ma60: float) -> float | None:
    """均线乖离率 (close − ma60) / ma60。ma60 非正 → None。"""
    if not ma60 or ma60 <= 0:
        return None
    return close / ma60 - 1.0


def aum_surge(shares: list[float]) -> float | None:
    """份额激增度：最新与上一报告期的份额环比（激增 = 资金涌入信号）。

    不足 2 期或上期份额非正 → None。
    """
    if len(shares) < 2 or shares[-2] <= 0:
        return None
    return shares[-1] / shares[-2] - 1.0


def peg_match(weighted_pe: float, weighted_growth: float) -> float | None:
    """持仓 PEG 匹配度 = 加权 PE / 加权增速（票 08 定义）。

    加权增速（盈利增速）非正 → None：增速为负时 PEG 分子分母同号失真，
    宁缺勿用（不产出"越贵越便宜"的假信号）。
    """
    if weighted_growth <= 0:
        return None
    return weighted_pe / weighted_growth


def capture_ratios(fund_rets: list[float], index_rets: list[float]) -> tuple[float, float]:
    """上下行捕获比 (up, down)——1.x calculator.py:548 同口径，提取为纯函数。

    up/down 期按**基准**正负划分；基金同向期均值 ÷ 基准同向期均值。
    无正/负期 → (1.0, 1.0)（中性，不误伤）；长度不等/空 → 中性。
    """
    if len(fund_rets) != len(index_rets) or not fund_rets:
        return 1.0, 1.0
    up_idx = [i for i, r in enumerate(index_rets) if r > 0]
    down_idx = [i for i, r in enumerate(index_rets) if r < 0]
    if not up_idx or not down_idx:
        return 1.0, 1.0
    up = sum(fund_rets[i] for i in up_idx) / len(up_idx)
    down = sum(fund_rets[i] for i in down_idx) / len(down_idx)
    idx_up = sum(index_rets[i] for i in up_idx) / len(up_idx)
    idx_down = sum(index_rets[i] for i in down_idx) / len(down_idx)
    if idx_up == 0 or idx_down == 0:
        return 1.0, 1.0
    return up / idx_up, down / idx_down


def drawdown_recovery(navs: list[float]) -> int | None:
    """回撤修复周期：最大回撤触底 → 净值首次收复（超过触底前峰值）的天数。

    单调上涨（无触底）、触底后未修复、样本 < 3 → None（尚未修复/无法判定）。
    """
    if len(navs) < 3:
        return None
    peak = navs[0]
    trough_idx: int | None = None
    trough_peak = navs[0]
    max_dd = 0.0
    for i, v in enumerate(navs):
        if v > peak:
            peak = v
        if peak > 0:
            dd = v / peak - 1.0
            if dd < max_dd:
                max_dd = dd
                trough_idx = i
                trough_peak = peak
    if trough_idx is None or trough_idx >= len(navs) - 1:
        return None
    for i in range(trough_idx + 1, len(navs)):
        if navs[i] > trough_peak:
            return i - trough_idx
    return None

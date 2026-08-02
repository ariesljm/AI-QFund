"""领域常量与纯函数单一来源：跨引擎/回测/Web 共用的口径与状态机。

避免同一领域事实（前向窗口、信号枚举、大盘状态判定、排序权重 schema）
在多处各自硬编码导致漂移。
"""

# ── 推荐周期 ─────────────────────────────────────────────
# 一次推荐对应的预测/结算/质量度量共用的前向窗口（交易日）
FORWARD_DAYS = 20

# ── 信号枚举（监控引擎/推荐引擎/质量度量共用） ──────────
SIGNAL_HOLD = "HOLD"
SIGNAL_BUY_MORE = "BUY_MORE"
SIGNAL_WARNING = "WARNING"
SIGNAL_EXIT = "EXIT"
SIGNAL_REJECT = "REJECT"
# 持仓状态集合（监控引擎与 repo 查询共用）
HOLDING_STATES = (SIGNAL_HOLD, SIGNAL_BUY_MORE, SIGNAL_WARNING)

# ── 大盘状态机 ──────────────────────────────────────────
REGIME_BULL = "BULL"
REGIME_BEAR = "BEAR"
REGIME_NEUTRAL = "NEUTRAL"


def regime_from_close_ma60(close, ma60) -> str:
    """沪深300 close vs MA60 → BULL/BEAR/NEUTRAL（回测/生产共用，避免两套判定漂移）。"""
    if close is None or ma60 is None or ma60 <= 0:
        return REGIME_NEUTRAL
    return REGIME_BULL if close > ma60 else REGIME_BEAR


def normalize_regime_label(label: str | None) -> str:
    """LLM 输出的大盘 regime label 归一化：bull*→BULL、bear*→BEAR，其余 NEUTRAL。"""
    raw = (label or "").strip().lower()
    if raw.startswith("bull"):
        return REGIME_BULL
    if raw.startswith("bear"):
        return REGIME_BEAR
    return REGIME_NEUTRAL


def display_score(combo: float | None, raw_score: float | None) -> int:
    """推荐分 → 0-100 展示分：有 combo 时 10 倍偏移，否则 500 倍偏移。"""
    if combo is not None:
        return min(max(int(combo * 10 + 50), 0), 100)
    return min(max(int((raw_score or 0) * 500 + 50), 0), 100) if raw_score else 0


# ── 排序权重默认 schema（repo 读取与进化自纠偏共用） ────
DEFAULT_RANKING_CFG = {
    "model_weight": 0.5,
    "rel_strength_weight": 0.15,
    "calmar_weight": 0.1,
    "hurst_weight": 0.1,
    "momentum_guard_pct": -15.0,
}


def index_window_slice(pos: int, window: int = 60) -> slice:
    """宽基指数滚动窗口切片：取 pos 及其前 window-1 个交易日（回测/主路径共用）。"""
    return slice(pos - (window - 1), pos + 1)

"""模块三：HOLD / WATCH / EXIT 三级状态机（票 15）。

接缝：转移是**纯函数**——`transition(current, signals)` 不读库、不读全局，
全路径可单测；数据装配（票 16 的 drifted、票 05 的估值分位、EMA 状态、
LLM 公告）由调用方组装 signals 后传入。

语义要点：
- **EXIT 不可逆**（current == EXIT → 恒为 EXIT）
- HOLD 下致命信号（跌破 EMA20 / 极端估值+动量转负 / 致命公告）先进 WATCH，
  **无跨级直通 EXIT**（先观察再退出的渐进风格）
- WATCH 下 EXIT 信号**优先于**修复信号（多信号同现按 EXIT > 修复 判定）
- 净值陈旧（stale_nav）不入 signals 转移逻辑（数据问题 ≠ 信号，沿用 1.x 区分）

阈值（票 15“进配置 + 标定报告”）：本模块常量是默认值，接线层可参数化覆盖；
护栏类阈值允许“更严”方向自动切换、放宽需人工——由接线层执行该策略。
"""

HOLD = "HOLD"
WATCH = "WATCH"
EXIT = "EXIT"

# 默认阈值（票 15）
VALUATION_HIGH_PCT = 85.0    # HOLD→WATCH：估值分位 ≥ 85%
VALUATION_EXTREME_PCT = 90.0  # WATCH→EXIT：估值 > 90% 且 5 日动量由正转负
ALPHA_NEG_DAYS = 5            # HOLD→WATCH：Alpha 连续 5 日为负


def is_watch_signal(s: dict) -> bool:
    """HOLD→WATCH 触发（任一条）：脱轨 / 估值高分位 / Alpha 连续负 5 日。"""
    if s.get("drifted"):
        return True
    if (s.get("valuation_pctile") or 0) >= VALUATION_HIGH_PCT:
        return True
    if (s.get("alpha_neg_days") or 0) >= ALPHA_NEG_DAYS:
        return True
    return False


def is_exit_signal(s: dict) -> bool:
    """WATCH→EXIT 触发（任一条）：跌破 EMA20 / 极端估值且动量转负 / 致命公告。"""
    if s.get("below_ema20") or s.get("fatal_news"):
        return True
    if (s.get("valuation_pctile") or 0) > VALUATION_EXTREME_PCT and not s.get("momentum_pos"):
        return True
    return False


def transition(current: str, s: dict) -> str:
    """三级状态转移纯函数：current + signals → next_state。"""
    if current == EXIT:
        return EXIT
    exit_sig = is_exit_signal(s)
    watch_sig = is_watch_signal(s)
    if current == WATCH:
        if exit_sig:
            return EXIT
        if not watch_sig:
            return HOLD          # 指标修复，解除预警
        return WATCH
    if current == HOLD:
        if exit_sig or watch_sig:
            return WATCH
        return HOLD
    raise ValueError(f"未知状态: {current!r}")

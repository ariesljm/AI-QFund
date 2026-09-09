"""加仓信号（BUY_MORE）链路测试：R2d 量化上行候选、R4 枚举校验、压制可见化。

回归背景：加仓信号此前仅能由 R4 LLM 复核层产出，且被 WARNING 压制时无任何
留痕，无法事后复盘"错过"的加仓建议；R2d 补上量化层正向候选，
压制在 detail 显式记录。
"""

from app.engine.monitor import (
    DefenseContext,
    DefenseRule,
    LogicVerificationRule,
    ModelUpsideRule,
    _apply_defense_chain,
)


def _ctx(**kw) -> DefenseContext:
    base = dict(code="001428",
                scores_series=[("2026-08-08", 0.20, "v1")],
                entry_score=0.10,
                navs=[1.0],          # 数据不足预热期 → ema60_exit 不触发
                recent_signals=[])
    base.update(kw)
    return DefenseContext(**base)


def _seq(score, ver="v1"):
    return [("2026-08-08", score, ver)]


# ───────────────────────────────────────────
# R2d：模型上行加仓候选
# ───────────────────────────────────────────

class TestModelUpsideRule:
    def test_fires_when_score_up_50pct_and_trend_healthy(self):
        """当前分 >0 且 > 买入分的150%，趋势健康 → BUY_MORE。"""
        result = ModelUpsideRule().check(_ctx())
        assert result is not None
        assert result.signal == "BUY_MORE"
        assert "显著走强" in result.reason

    def test_not_fires_when_below_150pct(self):
        """涨幅未超 50% 阈值 → 不触发。"""
        assert ModelUpsideRule().check(_ctx(scores_series=_seq(0.14))) is None

    def test_not_fires_when_negative(self):
        """当前分为负 → 不触发（负向由 R2c 处理）。"""
        assert ModelUpsideRule().check(_ctx(scores_series=_seq(-0.02))) is None

    def test_suppressed_by_ema_exit(self):
        """趋势不健康（EMA60 趋势退出触发）→ 不加仓。"""
        navs = [1.0] * 62 + [0.5, 0.4]  # 预热后连续 2 日 < EMA60
        assert ModelUpsideRule().check(_ctx(navs=navs)) is None

    def test_rate_limited_by_recent_buy_more(self):
        """近期已发过 BUY_MORE → 不重复提出（避免刷屏并干扰 WARNING 升级序列）。"""
        ctx = _ctx(recent_signals=[("2026-08-07", "BUY_MORE"), ("2026-08-06", "HOLD")])
        assert ModelUpsideRule().check(ctx) is None

    def test_recent_signals_none_skips_rate_limit(self):
        """recent_signals 未装配（单规则测试/旧构造）→ 不做限频检查，照常判定。"""
        ctx = _ctx(recent_signals=None)
        assert ModelUpsideRule().check(ctx) is not None


# ───────────────────────────────────────────
# R4 枚举校验：非法输出按解析失败跳过
# ───────────────────────────────────────────

class TestR4EnumValidation:
    def test_invalid_hint_skips_rule(self):
        """signal_hint 非法值（如旧别名 ADD）→ 跳过该防线，不静默降级 HOLD。"""
        ctx = _ctx()
        ctx.r4_precomputed = True
        ctx.r4_logic = {"logic_verdict": "维持", "signal_hint": "ADD", "reason": "x"}
        assert LogicVerificationRule().check(ctx) is None
        assert ctx.r4_skipped is True

    def test_invalid_verdict_skips_rule(self):
        """logic_verdict 非法值 → 同样按解析失败处理。"""
        ctx = _ctx()
        ctx.r4_precomputed = True
        ctx.r4_logic = {"logic_verdict": "部分维持", "signal_hint": "HOLD"}
        assert LogicVerificationRule().check(ctx) is None
        assert ctx.r4_skipped is True

    def test_missing_hint_keeps_verdict_semantics(self):
        """hint 缺失视为无提示：verdict=断裂 仍正常产出 EXIT（旧数据兼容）。"""
        ctx = _ctx()
        ctx.r4_precomputed = True
        ctx.r4_logic = {"logic_verdict": "断裂", "reason": "重仓全部退出"}
        result = LogicVerificationRule().check(ctx)
        assert result is not None and result.signal == "EXIT"

    def test_valid_buy_more_passes(self):
        """合法 BUY_MORE 提示 → 正常产出加仓信号。"""
        ctx = _ctx()
        ctx.r4_precomputed = True
        ctx.r4_logic = {"logic_verdict": "维持", "signal_hint": "BUY_MORE",
                        "sector_risk": False, "holding_risk": False, "reason": "核心重仓增持"}
        result = LogicVerificationRule().check(ctx)
        assert result is not None and result.signal == "BUY_MORE"


# ───────────────────────────────────────────
# 压制可见化：候选被更高优先级信号压下时留痕
# ───────────────────────────────────────────

class _StubRule(DefenseRule):
    def __init__(self, severity: int, signal: str):
        super().__init__()
        self.severity = severity
        self._signal = signal

    def check(self, ctx: DefenseContext):
        if self._signal == "HOLD":
            return None
        from app.engine.monitor import DefenseResult
        return DefenseResult(signal=self._signal, reason=f"{self._signal}@{self.severity}")


class TestSuppressionVisibility:
    def test_buy_more_overridden_by_warning_leaves_trace(self):
        """WARNING 与 BUY_MORE 并存 → 最终 WARNING，detail 记录压制。"""
        signal, detail, *_ = _apply_defense_chain(
            _ctx(), rules=[_StubRule(10, "WARNING"), _StubRule(40, "BUY_MORE")])
        assert signal == "WARNING"
        assert "（加仓建议被WARNING压制）" in detail

    def test_buy_more_surviving_has_no_trace(self):
        """仅 BUY_MORE → 正常产出，无压制文案。"""
        signal, detail, *_ = _apply_defense_chain(_ctx(), rules=[_StubRule(40, "BUY_MORE")])
        assert signal == "BUY_MORE"
        assert "压制" not in detail

    def test_chain_with_real_rules_no_false_trace(self):
        """生产防线全量注入：健康持仓无加仓候选（分数涨幅未达阈值），不得误标压制文案。"""
        ctx = _ctx(scores_series=_seq(0.12))  # 0.12 < 买入分 0.10 的150% → R2d 不触发
        signal, detail, *_ = _apply_defense_chain(ctx)
        assert signal == "HOLD"
        assert "压制" not in detail

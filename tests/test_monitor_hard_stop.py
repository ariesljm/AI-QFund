"""Ticket 03：监控回撤硬止损测试。

净值距持仓期最高点回撤 ≥8% → 无条件 EXIT（先于慢速 EMA60/模型信号生效）；
入场后 7 自然日内豁免（净值 ≤7 条保守不触发，避开惩罚赎回费）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.engine.monitor import DefenseContext, HardStopRule, ModelSignalRule, _apply_defense_chain


def _ctx(navs):
    return DefenseContext(code="X", navs=navs)


class TestHardStopRule:
    def test_trigger_at_threshold(self):
        """平稳期后崩盘（回撤 7% ≥ 波动自适应阈值 6%）且已过豁免期 → EXIT。"""
        navs = [1.0] * 20 + [0.93]
        r = HardStopRule().check(_ctx(navs))
        assert r is not None
        assert r.signal == "EXIT"
        assert "回撤" in r.reason

    def test_exempt_within_seven_navs(self):
        """入场 7 自然日内（≤7 条净值）回撤再大也不触发（避惩罚赎回费）。"""
        navs = [1.2, 1.0]
        assert HardStopRule().check(_ctx(navs)) is None

    def test_no_trigger_below_threshold(self):
        """回撤不足自适应阈值 → 不触发。"""
        navs = [1.0] * 7 + [0.98]
        assert HardStopRule().check(_ctx(navs)) is None

    def test_high_vol_tolerates_big_pullback(self):
        """高波动序列：8% 回撤不触发（T04 核心：不再固定 8% 一刀切）。"""
        navs = [1.0]
        for _ in range(10):
            navs.append(navs[-1] * 1.01)
        for _ in range(5):
            navs.append(navs[-1] * 0.985)
        r = HardStopRule().check(_ctx(navs))
        assert r is None

    def test_boundary_exact_threshold(self):
        """回撤恰为自适应阈值（6% floor）→ 触发（≥ 含边界）。"""
        navs = [1.0] * 20 + [0.94]
        r = HardStopRule().check(_ctx(navs))
        assert r is not None and r.signal == "EXIT"

    def test_insufficient_data_none(self):
        """净值不足 2 条非正 → 不触发（防御）。"""
        assert HardStopRule().check(_ctx([])) is None
        assert HardStopRule().check(_ctx([1.0])) is None

    def test_exit_outranks_warning_from_other_rules(self):
        """回撤 EXIT 与模型信号 WARNING 并存时，链最终输出 EXIT。"""
        navs = [1.0] * 20 + [0.93]  # 回撤 7% → 硬止损 EXIT
        ctx = _ctx(navs)
        # 注入会给出 WARNING 的模型信号序列：当前分转负
        ctx.entry_score = 0.05
        ctx.scores_series = [("2026-09-04", -0.01, "v1")]
        signal, detail, *_ = _apply_defense_chain(ctx)
        assert signal == "EXIT"
        assert "回撤硬止损" in detail

    def test_default_chain_contains_hard_stop(self):
        """默认链行为已由 test_exit_outranks 覆盖（默认链须输出 EXIT 才可能含硬止损）。

        此处仅保留最精简断言：硬止损 severity 高于模型信号规则（优先级保证 EXIT 胜出）。"""
        assert HardStopRule.severity > ModelSignalRule.severity

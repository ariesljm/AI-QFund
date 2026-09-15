"""票 15：三级状态机（HOLD/WATCH/EXIT）全转移路径测试。

覆盖：HOLD→WATCH 三条触发线（含阈值边界）、WATCH 修复回 HOLD、
WATCH→EXIT 三条线（含 >90 精确边界）、EXIT 不可逆、多信号优先级
（EXIT > 修复；HOLD 无跨级直通）、净值陈旧不计入升级。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.engine.state_machine import EXIT, HOLD, WATCH, transition


def _s(**kw):
    base = {"drifted": False, "valuation_pctile": 50.0, "alpha_neg_days": 0,
            "below_ema20": False, "momentum_pos": True, "fatal_news": False}
    base.update(kw)
    return base


class TestHoldStaysHold:
    def test_clean(self):
        assert transition(HOLD, _s()) == HOLD

    def test_stale_nav_not_an_upgrade_signal(self):
        """净值陈旧是数据问题不是信号：不触发升级。"""
        assert transition(HOLD, _s(stale_nav=True)) == HOLD


class TestHoldToWatch:
    def test_drifted(self):
        assert transition(HOLD, _s(drifted=True)) == WATCH

    def test_valuation_high_boundary(self):
        assert transition(HOLD, _s(valuation_pctile=85.0)) == WATCH
        assert transition(HOLD, _s(valuation_pctile=84.99)) == HOLD

    def test_alpha_neg_days_boundary(self):
        assert transition(HOLD, _s(alpha_neg_days=5)) == WATCH
        assert transition(HOLD, _s(alpha_neg_days=4)) == HOLD


class TestHoldNoDirectExit:
    def test_fatal_exit_signals_enter_watch_first(self):
        """HOLD 下致命信号不跨级直通 EXIT，先进 WATCH。"""
        assert transition(HOLD, _s(below_ema20=True)) == WATCH
        assert transition(HOLD, _s(fatal_news=True)) == WATCH
        assert transition(HOLD, _s(valuation_pctile=95.0, momentum_pos=False)) == WATCH


class TestWatchToHold:
    def test_fixed(self):
        assert transition(WATCH, _s()) == HOLD


class TestWatchToExit:
    def test_below_ema20(self):
        assert transition(WATCH, _s(below_ema20=True)) == EXIT

    def test_fatal_news(self):
        assert transition(WATCH, _s(fatal_news=True)) == EXIT

    def test_valuation_extreme_and_momentum_turn(self):
        assert transition(WATCH, _s(valuation_pctile=91.0, momentum_pos=False)) == EXIT

    def test_extreme_boundary_requires_strictly_above_90(self):
        assert transition(WATCH, _s(valuation_pctile=90.0, momentum_pos=False)) == WATCH

    def test_extreme_but_momentum_positive_not_exit(self):
        assert transition(WATCH, _s(valuation_pctile=95.0, momentum_pos=True)) == WATCH


class TestPriorities:
    def test_watch_exit_beats_fix(self):
        """WATCH 下 EXIT 信号与修复同现 → EXIT 优先。"""
        assert transition(WATCH, _s(below_ema20=True)) == EXIT

    def test_exit_irreversible(self):
        """EXIT 不可逆：任何信号都回不去。"""
        assert transition(EXIT, _s()) == EXIT
        assert transition(EXIT, _s(below_ema20=False, fatal_news=False)) == EXIT

    def test_unknown_state_raises(self):
        with pytest.raises(ValueError):
            transition("UNKNOWN", _s())

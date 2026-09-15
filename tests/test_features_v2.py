"""票 08：2.0 特征纯函数（每个特征一条单测，含退化路径）。

覆盖：动量加速度（基数非正退化）、Bias_60、份额激增度、PEG 匹配度
（增速非正退化）、上下行捕获比（无正/负期中性）、回撤修复周期
（单调/未修复退化）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.features.v2 import (
    aum_surge,
    bias_60,
    capture_ratios,
    drawdown_recovery,
    momentum_acceleration,
    peg_match,
)


class TestMomentumAcceleration:
    def test_normal(self):
        # 近 1 月 +10%，近 6 月 +30% → 10%/(30%/6) = 2.0（近月热度为均值两倍）
        assert momentum_acceleration(0.10, 0.30) == pytest.approx(2.0)

    def test_flat_six_month_returns_none(self):
        """R_6m <= 0（无正基数）→ None，不产出除零/负加速度。"""
        assert momentum_acceleration(0.05, 0.0) is None
        assert momentum_acceleration(0.05, -0.1) is None


class TestBias60:
    def test_normal(self):
        assert bias_60(110.0, 100.0) == pytest.approx(0.10)
        assert bias_60(95.0, 100.0) == pytest.approx(-0.05)

    def test_nonpositive_ma(self):
        assert bias_60(10.0, 0.0) is None
        assert bias_60(10.0, -5.0) is None


class TestAumSurge:
    def test_surge(self):
        assert aum_surge([10.0, 20.0]) == pytest.approx(1.0)      # 100% 激增

    def test_insufficient(self):
        assert aum_surge([10.0]) is None
        assert aum_surge([]) is None

    def test_prev_nonpositive(self):
        assert aum_surge([0.0, 5.0]) is None


class TestPegMatch:
    def test_normal(self):
        assert peg_match(30.0, 0.20) == pytest.approx(150.0)

    def test_nonpositive_growth(self):
        """增速非正 → None：负增速下 PEG 分子分母同号失真，宁缺勿用。"""
        assert peg_match(30.0, 0.0) is None
        assert peg_match(30.0, -0.1) is None


class TestCaptureRatios:
    def test_normal(self):
        # 基准 +1/-1 交替；基金跟涨不跟跌 → up=1.0, down=0.0
        fund = [0.01, 0.0, 0.01, 0.0]
        index = [0.01, -0.01, 0.01, -0.01]
        up, down = capture_ratios(fund, index)
        assert up == pytest.approx(1.0)
        assert down == pytest.approx(0.0)

    def test_no_negative_period_neutral(self):
        """基准全正 → 无 down 期 → 中性 (1.0, 1.0)，不误伤。"""
        assert capture_ratios([0.01] * 3, [0.02] * 3) == (1.0, 1.0)

    def test_mismatched_length(self):
        assert capture_ratios([1.0], [2.0, 3.0]) == (1.0, 1.0)


class TestDrawdownRecovery:
    def test_recovery_days(self):
        # 100 → 120（峰）→ 90（触底）→ 100 → 125（收复 120）: 3 天修复
        navs = [100.0, 120.0, 95.0, 90.0, 100.0, 110.0, 125.0]
        assert drawdown_recovery(navs) == 3

    def test_monotonic_no_trough(self):
        assert drawdown_recovery([100.0, 110.0, 120.0]) is None

    def test_unrecovered(self):
        assert drawdown_recovery([100.0, 120.0, 90.0, 95.0]) is None

    def test_too_short(self):
        assert drawdown_recovery([100.0, 90.0]) is None

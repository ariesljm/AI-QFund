"""票 18：校准层命中率记账与降权/停用判定。

覆盖：命中率与样本量门槛（不足不标注/不降权）、连续失灵计数与打断、
降权（3）/停用（5）边界、置信度文案格式。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.engine.calibration import (
    MIN_SAMPLES,
    STREAK_FAIL_TO_DISABLE,
    STREAK_FAIL_TO_DOWNGRADE,
    assess,
    confidence_note,
    fail_streak,
    hit_rate,
)


class TestHitRate:
    def test_normal(self):
        assert hit_rate(6, 10) == pytest.approx(0.6)

    def test_insufficient_samples_none(self):
        """样本不足 → None（不可信，不产生置信度标注）。"""
        assert hit_rate(5, 5) is None
        assert hit_rate(0, 0) is None

    def test_boundary(self):
        assert hit_rate(3, MIN_SAMPLES) == pytest.approx(0.3)   # python 变量注入


class TestConfidenceNote:
    def test_format(self):
        assert confidence_note(19, 30) == "该信号历史命中 63%（30 样本）"

    def test_insufficient_empty(self):
        assert confidence_note(2, 3) == ""


class TestFailStreak:
    def test_consecutive_failures(self):
        assert fail_streak([True, False, False, True, False, False, False]) == 3

    def test_zero_streak(self):
        assert fail_streak([True, True]) == 0

    def test_empty(self):
        assert fail_streak([]) == 0

    def test_all_fail(self):
        assert fail_streak([False, False, False]) == 3


class TestAssess:
    def test_insufficient_samples_never_downgrades(self):
        """票 18 明示：避免用 3 个样本停掉一个信号。"""
        got = assess([False, False, False])
        assert got["action"] == "hold" and got["hit_rate"] is None

    def test_downgrade_at_streak(self):
        got = assess([True] * 7 + [False] * STREAK_FAIL_TO_DOWNGRADE)   # 10 样本
        assert got["action"] == "downgrade"

    def test_disable_at_streak(self):
        got = assess([True] * 5 + [False] * STREAK_FAIL_TO_DISABLE)     # 10 样本
        assert got["action"] == "disable"

    def test_recent_hit_breaks_streak(self):
        got = assess([True] * 7 + [False] * (STREAK_FAIL_TO_DISABLE - 1) + [True])
        assert got["action"] == "hold"

    def test_healthy_hold(self):
        got = assess([True] * 10)
        assert got["action"] == "hold"
        assert got["hit_rate"] == pytest.approx(1.0)
        assert got["note"] == "该信号历史命中 100%（10 样本）"

    def test_samples_tracked(self):
        got = assess([True, True, False, False])
        assert got["samples"] == 4

"""票 20：规则退役机制（否决类不可逆 / 信号类可退役，提案+留痕）。

覆盖：否决类永不可被退役路径触及；信号类命中率低且样本足够才提案退役；
样本不足不提案（避免小样本误杀）；退役记录保留可恢复性与校准依据。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.engine.calibration import (
    MIN_SAMPLES,
    RETIREMENT_MIN_HIT_RATE,
    retirement_decision,
    retirement_record,
)


class TestRetirementDecision:
    def test_veto_never_eligible(self):
        """否决类不可逆：即使命中率极低也永不提案退役。"""
        got = retirement_decision("veto", 0.10, 100)
        assert got == {"eligible": False, "propose": False,
                       "reason": "否决类规则单向增严，不可逆（ADR-0009）"}

    def test_signal_low_hit_rate_proposes(self):
        got = retirement_decision("signal", 0.30, MIN_SAMPLES)
        assert got["eligible"] is True and got["propose"] is True
        assert "长期低于阈值" in got["reason"]

    def test_signal_insufficient_samples_no_proposal(self):
        """样本不足不提案（票 20 验收：避免小样本误杀）。"""
        got = retirement_decision("signal", 0.0, 3)
        assert got["propose"] is False and "样本不足" in got["reason"]

    def test_signal_healthy_no_proposal(self):
        got = retirement_decision("signal", 0.8, MIN_SAMPLES)
        assert got["propose"] is False and got["reason"] == "命中率正常"

    def test_boundary_hit_rate(self):
        """命中率恰在阈值处不算'低于'。"""
        got = retirement_decision("signal", RETIREMENT_MIN_HIT_RATE, MIN_SAMPLES)
        assert got["propose"] is False

    def test_unknown_class_raises(self):
        with pytest.raises(ValueError):
            retirement_decision("mystery", 0.5, 10)


class TestRetirementRecord:
    def test_record_keeps_evidence_and_recoverability(self):
        """退役是提案+留痕：对象/依据/时间/可恢复性都在记录里。"""
        rec = retirement_record("drift_corr", "signal", 0.30, 30,
                                "命中率 30% 长期低于阈值", "2026-09-15")
        assert rec["rule_id"] == "drift_corr"
        assert rec["hit_rate"] == 0.30 and rec["samples"] == 30
        assert rec["date"] == "2026-09-15"
        assert rec["recoverable"] is True          # 信号类可恢复

    def test_veto_record_not_recoverable(self):
        rec = retirement_record("veto_news", "veto", None, 0, "不可逆", "2026-09-15")
        assert rec["recoverable"] is False

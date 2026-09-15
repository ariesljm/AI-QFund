"""票 19：C 层影子闸门判定（必须能失败）。

覆盖：噪声场景必须不通过闸门（票 19 明示）；三段中一段劣化即拒绝；
标尺版本不一致拒绝；子区间不足拒绝；合并区间显著性边界。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


from app.engine.gate import gate_decision, paired_t_pvalue, regime_no_worse


def _regimes(challenger, champion):
    return [{"regime": r, "challenger": c, "champion": b}
            for r, c, b in zip(["bull", "bear", "sideways"],
                               challenger, champion, strict=True)]


class TestRegimeNoWorse:
    def test_all_no_worse(self):
        ok, _ = regime_no_worse(_regimes([0.5, 0.3, 0.2], [0.4, 0.2, 0.1]))
        assert ok

    def test_one_worse_rejected(self):
        """三段中一段劣化即拒绝（票 19 明示）。"""
        ok, why = regime_no_worse(_regimes([0.5, 0.1, 0.2], [0.4, 0.2, 0.1]))
        assert not ok and "劣化" in why

    def test_insufficient_regimes(self):
        ok, why = regime_no_worse(
            [{"regime": "bull", "challenger": 0.5, "champion": 0.4}])
        assert not ok and "不足" in why


class TestPairedTPvalue:
    def test_consistent_improvement_significant(self):
        """challenger 每窗口稳定高于 champion → 显著（p 小）。"""
        p = paired_t_pvalue([1.0, 1.1, 1.2, 1.3, 1.4, 1.5],
                            [1.0, 1.0, 1.0, 1.0, 1.0, 1.0])
        assert p < 0.05

    def test_noise_not_significant(self):
        """噪声场景：均值略高但窗口间大幅波动 → 不显著（必须不通过闸门）。"""
        challenger = [1.0, -1.0, 1.0, -1.0, 1.0, -1.0]
        champion = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        assert paired_t_pvalue(challenger, champion) >= 0.05

    def test_too_few_windows(self):
        assert paired_t_pvalue([1.0], [1.0]) == 1.0


class TestGateDecision:
    VERSION = "excess_rbsa40d_v1"
    GOOD_C = [0.5, 0.3, 0.2]
    GOOD_B = [0.4, 0.2, 0.1]
    WIN_C = [1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.0, 1.1, 1.2, 1.3, 1.4, 1.5]
    WIN_B = [1.0] * len(WIN_C)

    def test_all_gates_pass(self):
        got = gate_decision(_regimes(self.GOOD_C, self.GOOD_B),
                            self.WIN_C, self.WIN_B, self.VERSION, self.VERSION)
        assert got["passed"] is True, got["reasons"]

    def test_noise_challenger_rejected(self):
        """噪声场景必须不通过闸门（票 19 最不能漏的测试）。"""
        noisy = [1.0, -1.0, 1.0, -1.0, 1.0, -1.0, 1.0, -1.0, 1.0, -1.0, 1.0, -1.0]
        got = gate_decision(_regimes(self.GOOD_C, self.GOOD_B),
                            noisy, [0.0] * len(noisy), self.VERSION, self.VERSION)
        assert got["passed"] is False
        assert any("不显著" in r for r in got["reasons"])

    def test_one_regime_worse_rejected(self):
        got = gate_decision(_regimes([0.5, 0.1, 0.2], self.GOOD_B),
                            self.WIN_C, self.WIN_B, self.VERSION, self.VERSION)
        assert got["passed"] is False

    def test_version_mismatch_rejected(self):
        got = gate_decision(_regimes(self.GOOD_C, self.GOOD_B),
                            self.WIN_C, self.WIN_B, "v_old", self.VERSION)
        assert got["passed"] is False
        assert any("版本不一致" in r for r in got["reasons"])

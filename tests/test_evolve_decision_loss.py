"""Q8 裁决损耗自省测试。

- _decision_loss_streak：连续为负月数计数（非负中断、无数据为 0）；
- evolution_analysis_prompt：损耗观测段注入（当月值 + 连续为负提示）。
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.engine import evolve
from app.llm.prompts import evolution_analysis_prompt


class TestDecisionLossStreak:
    """裁决损耗连续为负月数。"""

    def test_streak_counts_negative_runs(self, monkeypatch):
        monkeypatch.setattr(evolve.repo, "get_quality_metrics",
                            lambda limit=6: [
                                {"decision_loss": -0.01},   # 最新
                                {"decision_loss": -0.02},
                                {"decision_loss": 0.005},   # 非负中断
                                {"decision_loss": -0.03},
                            ])
        assert evolve._decision_loss_streak() == 2

    def test_no_data_zero(self, monkeypatch):
        monkeypatch.setattr(evolve.repo, "get_quality_metrics", lambda limit=6: [])
        assert evolve._decision_loss_streak() == 0

    def test_none_loss_breaks(self, monkeypatch):
        monkeypatch.setattr(evolve.repo, "get_quality_metrics",
                            lambda limit=6: [
                                {"decision_loss": None},   # 最新无损耗数据 → 中断
                                {"decision_loss": -0.02},
                            ])
        assert evolve._decision_loss_streak() == 0


class TestEvolutionPromptDecisionLoss:
    """Q8：损耗观测注入元分析 prompt。"""

    def _prompt(self, **kw):
        return evolution_analysis_prompt([], [], [], **kw)

    def test_injects_loss_section(self):
        p = self._prompt(decision_loss=-0.015, loss_streak=3)
        assert "定论环节裁决损耗观测" in p
        assert "-1.50pp" in p
        assert "连续 3 个月为负" in p

    def test_no_section_when_no_loss(self):
        p = self._prompt()
        assert "定论环节裁决损耗观测" not in p

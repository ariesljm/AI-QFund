"""T5 度量反哺测试。

核心：质量指标下行（IC<0 且样本足够）→ 自动降低模型权重并留痕；健康时不动。
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.engine import evolve
import app.database as db_mod
from app import repo


class TestPlanParamAdjustment:
    """纯函数：规划参数调整。"""

    def _cfg(self, model_weight=0.5):
        return {
            "model_weight": model_weight, "rel_strength_weight": 0.15,
            "calmar_weight": 0.1, "hurst_weight": 0.1,
            "momentum_guard_pct": -15.0,
        }

    def test_healthy_no_adjust(self):
        m = {"ic": 0.3, "sample_count": 10, "excess_win_rate": 0.6}
        assert evolve.plan_param_adjustment(m, self._cfg()) is None

    def test_insufficient_sample_no_adjust(self):
        m = {"ic": -0.5, "sample_count": 3}
        assert evolve.plan_param_adjustment(m, self._cfg()) is None

    def test_ic_none_no_adjust(self):
        m = {"ic": None, "sample_count": 10}
        assert evolve.plan_param_adjustment(m, self._cfg()) is None

    def test_negative_ic_triggers(self):
        plan = evolve.plan_param_adjustment(
            {"ic": -0.4, "sample_count": 10}, self._cfg())
        assert plan is not None
        assert plan["cfg"]["model_weight"] == 0.25
        assert "负相关" in plan["reason"]

    def test_model_weight_floor(self):
        plan = evolve.plan_param_adjustment(
            {"ic": -0.4, "sample_count": 10}, self._cfg(model_weight=0.2))
        assert plan["cfg"]["model_weight"] == 0.1

    def test_win_rate_low_triggers(self):
        """IC 健康但超额胜率低于五成 → 也触发调整。"""
        plan = evolve.plan_param_adjustment(
            {"ic": 0.2, "excess_win_rate": 0.3, "sample_count": 10}, self._cfg())
        assert plan is not None
        assert "超额胜率" in plan["reason"]

    def test_ic_none_win_rate_low_triggers(self):
        plan = evolve.plan_param_adjustment(
            {"ic": None, "excess_win_rate": 0.2, "sample_count": 10}, self._cfg())
        assert plan is not None


class TestApplyParamAdjustment:
    """集成：调整持久化 + 留痕。"""

    def test_triggers_and_records(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "test.db")
        reason = evolve.apply_param_adjustment(
            {"ic": -0.5, "sample_count": 10, "excess_win_rate": 0.3})
        assert reason
        assert repo.get_ranking_cfg()["model_weight"] == 0.25
        insights = repo.get_active_insights(5)
        assert any("负相关" in s for s in insights)

    def test_healthy_noop(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "test.db")
        assert evolve.apply_param_adjustment(
            {"ic": 0.3, "sample_count": 10, "excess_win_rate": 0.6}) is None
        assert repo.get_ranking_cfg()["model_weight"] == 0.5

    def test_degraded_insight_inactive(self, monkeypatch, tmp_path):
        """质量下行时新洞察以非活跃态入库（待审），不自动启用。"""
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "test.db")
        assert evolve._save_insight({"insight": "质量下行时的新规则", "type": "sector"},
                                    degraded=True) is True
        assert evolve._save_insight({"insight": "质量健康时的新规则", "type": "sector"},
                                    degraded=False) is True
        active = repo.get_active_insights(5)
        assert any("质量健康时的新规则" in s for s in active)
        assert not any("质量下行时的新规则" in s for s in active)

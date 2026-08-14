"""T5 质量下行信号测试。

核心：质量指标下行（IC<0 或胜率<0.5 且样本足够）→ 返回紧急触发信号；
权重调节统一收敛到 GA，本模块不再直接修改权重。
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.engine import evolve


class TestPlanParamAdjustment:
    """纯函数：质量下行检测（阶段5：赚钱胜率 <50% 触发；只返回信号，不规划/修改权重）。"""

    def test_healthy_no_signal(self):
        m = {"ic": 0.3, "sample_count": 10, "profit_rate": 0.6}
        assert evolve.plan_param_adjustment(m) is None

    def test_insufficient_sample_no_signal(self):
        m = {"ic": -0.5, "sample_count": 3}
        assert evolve.plan_param_adjustment(m) is None

    def test_ic_none_no_signal(self):
        m = {"ic": None, "sample_count": 10}
        assert evolve.plan_param_adjustment(m) is None

    def test_profit_rate_low_triggers(self):
        """赚钱胜率低于五成 → 触发质量下行信号。"""
        note = evolve.plan_param_adjustment(
            {"ic": 0.2, "profit_rate": 0.3, "sample_count": 10})
        assert note is not None
        assert "赚钱胜率" in note

    def test_profit_rate_none_no_signal(self):
        note = evolve.plan_param_adjustment(
            {"ic": None, "profit_rate": None, "sample_count": 10})
        assert note is None

    def test_model_weight_not_touched(self):
        """信号检测不修改任何配置（权重统一由 GA 调节）。"""
        note = evolve.plan_param_adjustment({"profit_rate": 0.3, "sample_count": 10})
        assert note is not None
        assert "model_weight" not in note  # 信号只描述问题，不指定新权重

    def test_apply_param_adjustment_removed(self):
        """apply_param_adjustment 已移除：不再存在直接写权重的入口。"""
        assert not hasattr(evolve, "apply_param_adjustment")

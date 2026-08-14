"""模型信号防线测试：预测 20 日绝对收益转负 → WARNING / 连续确认 → EXIT（阶段二序列版）。

架构深化候选 3：ctx 预装配模式——scores_series/entry_score 直接构造，不 mock DB 函数。
"""

from app.engine import monitor as monitor_mod
from app.engine.monitor import DefenseContext, ModelSignalRule


def _make_ctx(code: str = "001428", scores_series=None, entry_score=None) -> DefenseContext:
    return DefenseContext(code=code, scores_series=scores_series or [], entry_score=entry_score)


def _seq(*scores, ver="2026-08-06|t"):
    """构造 (date, score, model_version) 序列（倒序，最新在前）。"""
    return [(f"2026-08-{8 - i:02d}", s, ver) for i, s in enumerate(scores)]


class TestModelSignalRule:
    def test_negative_score_warns(self):
        """单日转负 → WARNING，并带买入分对比。"""
        result = ModelSignalRule().check(_make_ctx(scores_series=_seq(-0.0312), entry_score=0.0450))
        assert result is not None
        assert result.signal == "WARNING"
        assert "转负" in result.reason
        assert "买入时 0.0450" in result.reason

    def test_positive_score_holds(self):
        """预测仍为正 → 无信号。"""
        result = ModelSignalRule().check(_make_ctx(scores_series=_seq(0.0123)))
        assert result is None

    def test_missing_score_skips(self):
        """无序列（模型/特征缺失）→ 防线跳过，不误报。"""
        result = ModelSignalRule().check(_make_ctx(scores_series=[]))
        assert result is None

    def test_three_day_negative_exits(self):
        """连续 3 日转负 → EXIT（确认期跨过惩罚赎回费率带）。"""
        result = ModelSignalRule().check(_make_ctx(scores_series=_seq(-0.02, -0.01, -0.03)))
        assert result is not None
        assert result.signal == "EXIT"
        assert "连续3日转负" in result.reason

    def test_two_day_negative_still_warns(self):
        """仅连续 2 日转负 → 仍为 WARNING（确认期不足）。"""
        result = ModelSignalRule().check(_make_ctx(scores_series=_seq(-0.02, -0.01, 0.05)))
        assert result is not None
        assert result.signal == "WARNING"

    def test_version_change_resets_count(self):
        """确认期内模型重训（版本变化）→ 连续计数重置，仅当日单点判断。"""
        series = [
            ("2026-08-08", -0.02, "v2"),  # 今日（新模型）
            ("2026-08-07", -0.01, "v1"),  # 昨日（旧模型）
            ("2026-08-06", -0.03, "v1"),
        ]
        result = ModelSignalRule().check(_make_ctx(scores_series=series))
        assert result is not None
        assert result.signal == "WARNING"  # 版本跳变，不作连续确认

    def test_buy_score_halved_warns(self):
        """相对买入分下降 >50% → WARNING（即使未转负）。"""
        result = ModelSignalRule().check(_make_ctx(scores_series=_seq(0.02), entry_score=0.10))
        assert result is not None
        assert result.signal == "WARNING"
        assert "相对买入分下降" in result.reason

    def test_in_defense_chain(self):
        """防线链中：模型信号转负参与聚合（不短路）。"""
        signal, detail, *_ = monitor_mod._apply_defense_chain(
            _make_ctx(scores_series=_seq(-0.02, -0.01, -0.03)), rules=[ModelSignalRule()])
        assert signal == "EXIT"
        assert "连续3日转负" in detail

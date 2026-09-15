"""票 13：审计 JSON 校验/剪枝/复合分纯函数。

覆盖：schema 校验正反用例（非法 verdict/越界/非数字/VETO 无理由/超长）、
剪枝边界（risk=60 vs 60.01）、复合分计算、TopN 排序。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.llm.audit import composite_score, prune, top_n, validate_audit


def _ok(verdict="PASS", risk=30, reasons=None, summary="正常"):
    return {"audit_verdict": verdict, "risk_score": risk,
            "veto_reasons": reasons, "recommendation_summary": summary}


class TestValidateAudit:
    def test_valid_pass(self):
        ok, err = validate_audit(_ok())
        assert ok and err == ""

    def test_invalid_verdict_rejected(self):
        ok, err = validate_audit(_ok(verdict="VOTE"))
        assert not ok and "audit_verdict 非法" in err

    def test_missing_verdict_rejected(self):
        ok, _ = validate_audit(_ok(verdict=None))
        assert not ok

    def test_risk_out_of_range(self):
        assert not validate_audit(_ok(risk=-1))[0]
        assert not validate_audit(_ok(risk=101))[0]
        assert validate_audit(_ok(risk=0))[0] and validate_audit(_ok(risk=100))[0]

    def test_risk_non_numeric(self):
        ok, err = validate_audit(_ok(risk="high"))
        assert not ok and "非数字" in err

    def test_veto_requires_reasons(self):
        assert not validate_audit(_ok(verdict="VETO", reasons=None))[0]
        assert validate_audit(_ok(verdict="VETO", reasons=["重仓股立案"]))[0]

    def test_summary_too_long(self):
        ok, err = validate_audit(_ok(summary="长" * 51))
        assert not ok and "超长" in err

    def test_conditional_pass_valid(self):
        assert validate_audit(_ok(verdict="CONDITIONAL_PASS"))[0]


class TestPrune:
    def test_veto_pruned(self):
        assert prune("VETO", 10) is True

    def test_risk_boundary(self):
        assert prune("PASS", 60.0) is False       # 边界 60 保留
        assert prune("PASS", 60.01) is True       # 60.01 剔除

    def test_low_risk_pass_kept(self):
        assert prune("PASS", 30) is False
        assert prune("CONDITIONAL_PASS", 40) is False


class TestCompositeScore:
    def test_formula(self):
        assert composite_score(1.0, 0) == pytest.approx(1.0)
        assert composite_score(1.0, 50) == pytest.approx(0.5)
        assert composite_score(0.8, 100) == pytest.approx(0.0)

    def test_higher_risk_lowers_score(self):
        assert composite_score(0.9, 20) > composite_score(0.9, 80)


class TestTopN:
    def test_sorts_and_truncates(self):
        scored = [{"code": c, "final_score": s} for c, s in
                  [("A", 0.9), ("B", 0.7), ("C", 0.8), ("D", 0.6)]]
        got = top_n(scored, n=2)
        assert [g["code"] for g in got] == ["A", "C"]

    def test_empty(self):
        assert top_n([]) == []

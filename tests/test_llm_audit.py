"""票 13：审计 JSON 校验/剪枝/复合分纯函数。

覆盖：schema 校验正反用例（非法 verdict/越界/非数字/VETO 无理由/超长）、
剪枝边界（risk=60 vs 60.01）、复合分计算、TopN 排序。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.llm.audit import (
    cache_fingerprint,
    composite_score,
    is_cache_valid,
    prune,
    should_call_llm,
    top_n,
    validate_audit,
)


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


class TestAuditCache:
    """票 14：失效条件三元组——每个条件单独变化都必须导致失效重跑。"""

    _BASE = {"holdings_period": "2026-06-30", "stock_fingerprint": "abc",
             "event_version": "v3"}

    def _current(self, **kw):
        c = dict(self._BASE)
        c.update(kw)
        return c

    def test_fingerprint_changes_with_each_condition(self):
        f1 = cache_fingerprint("2026-06-30", "abc", "v3")
        assert cache_fingerprint("2026-03-31", "abc", "v3") != f1
        assert cache_fingerprint("2026-06-30", "xyz", "v3") != f1
        assert cache_fingerprint("2026-06-30", "abc", "v4") != f1

    def test_all_matching_is_valid(self):
        assert is_cache_valid(dict(self._BASE), self._current()) is True

    def test_no_cache_invalid(self):
        assert is_cache_valid(None, self._current()) is False

    def test_each_condition_change_invalidates(self):
        """每个失效条件单独变化都必须导致失效重跑（票 14 明示最容易写错处）。"""
        cases = [
            self._current(holdings_period="2026-03-31"),   # 持仓期次变
            self._current(stock_fingerprint="zzz"),        # 重仓股集合指纹变
            self._current(event_version="v2"),             # 风险事件版本变
        ]
        for cur in cases:
            assert is_cache_valid(dict(self._BASE), cur) is False

    def test_should_call_rules(self):
        cur = self._current()
        assert should_call_llm(is_new_entry=True, cached=dict(self._BASE), current=cur) is True
        assert should_call_llm(is_new_entry=False, cached=dict(self._BASE), current=cur) is False
        assert should_call_llm(is_new_entry=False, cached=None, current=cur) is True
        assert should_call_llm(is_new_entry=False, cached=dict(self._BASE),
                               current=self._current(stock_fingerprint="changed")) is True

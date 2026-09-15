"""票 17：知识库纯函数（归因分叉/案例结构/条件检索/回流装配）。

覆盖：归因分叉边界（缺失/超阈/正常）；案例条件字段与出处；条件检索
（类别/行业过滤且不改原列表）；回流装配确定性输出与字符预算截断。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.engine.knowledge import (
    FEATURE_LAG_TRADE_DAYS,
    assemble_few_shot,
    attribute_case,
    make_case,
    retrieve_cases,
)


class TestAttributeCase:
    def test_missing_feature_is_stale(self):
        assert attribute_case(None) == "feature_stale"

    def test_over_lag_is_stale(self):
        assert attribute_case(FEATURE_LAG_TRADE_DAYS + 1) == "feature_stale"

    def test_boundary_lag_is_hidden_risk(self):
        """滞后恰好 = 阈值 → 不算特征失效（边界）。"""
        assert attribute_case(FEATURE_LAG_TRADE_DAYS) == "hidden_risk"

    def test_normal_lag_is_hidden_risk(self):
        assert attribute_case(3) == "hidden_risk"


class TestMakeCase:
    def test_structure(self):
        c = make_case("bad", "2026-09-14", "F001", "白酒",
                      {"veto_reasons": ["重仓股立案"]}, {"ret_40d": -0.09})
        assert c["type"] == "bad" and c["decision_date"] == "2026-09-14"
        assert c["industry"] == "白酒" and c["audit"]["veto_reasons"] == ["重仓股立案"]


class TestRetrieveCases:
    def _cases(self):
        return [make_case("bad", "2026-09-14", "F1", "白酒", None, {}),
                make_case("good", "2026-09-14", "F2", "白酒", None, {}),
                make_case("bad", "2026-09-13", "F3", "新能源", None, {})]

    def test_filter_by_type(self):
        got = retrieve_cases(self._cases(), case_type="bad")
        assert [c["fund"] for c in got] == ["F1", "F3"]

    def test_filter_by_industry(self):
        got = retrieve_cases(self._cases(), industry="白酒")
        assert [c["fund"] for c in got] == ["F1", "F2"]

    def test_combined(self):
        got = retrieve_cases(self._cases(), case_type="bad", industry="白酒")
        assert [c["fund"] for c in got] == ["F1"]

    def test_does_not_mutate(self):
        cases = self._cases()
        retrieve_cases(cases, case_type="bad")
        assert len(cases) == 3

    def test_limit_takes_latest(self):
        got = retrieve_cases(self._cases(), limit=1)
        assert len(got) == 1 and got[0]["fund"] == "F3"   # 入库顺序取最近


class TestAssembleFewShot:
    def test_deterministic(self):
        cases = [make_case("bad", "2026-09-14", "F1", "白酒",
                           {"veto_reasons": ["立案"]}, {"ret_40d": -0.09})]
        t1 = assemble_few_shot(cases)
        t2 = assemble_few_shot(cases)
        assert t1 == t2
        assert "[BAD]" in t1 and "F1" in t1 and "立案" in t1

    def test_char_budget_truncates(self):
        cases = [make_case("bad", "2026-09-14", f"F{i}", "行业", None, {"r": i})
                 for i in range(500)]
        text = assemble_few_shot(cases)
        assert len(text) <= 2000 + len("…（已截断）")
        assert "已截断" in text

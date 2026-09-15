"""票 11：模块一筛选引擎纯函数核心（池子/硬过滤/Top30）。

覆盖：主动权益池（混合+股票、剔指数、未知类型）、硬过滤链（任一违规剔除、
缺数据不误杀口径）、Top30 排序截断。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


from app.engine.screen import (
    ACTIVE_EQUITY_TYPES,
    POOL_SIZE,
    active_equity_pool,
    apply_hard_filters,
    top_n,
)


class TestActiveEquityPool:
    def test_mixed_and_stock_included(self):
        funds = [{"code": "A", "type": "混合型"}, {"code": "B", "type": "股票型"}]
        assert active_equity_pool(funds) == ["A", "B"]

    def test_index_excluded(self):
        funds = [{"code": "A", "type": "混合型"}, {"code": "I", "type": "指数型"}]
        assert active_equity_pool(funds) == ["A"]

    def test_unknown_type_excluded(self):
        assert active_equity_pool([{"code": "X", "type": "QDII"}]) == []

    def test_types_are_exactly_expected(self):
        """池子白名单必须含混合+股票、不含指数（票 11 验收）。"""
        assert ACTIVE_EQUITY_TYPES == {"混合型", "股票型"}


class TestApplyHardFilters:
    def _facts(self, overrides=None):
        f = {"A": {"aum": 500_000_000, "purchase_status": "normal",
                   "daily_limit": None, "nav_count": 200},
             "B": {"aum": 500_000_000, "purchase_status": "normal",
                   "daily_limit": None, "nav_count": 200}}
        if overrides:
            for code, patch in overrides.items():
                f[code].update(patch)
        return f

    def test_all_pass_kept(self):
        assert apply_hard_filters(["A", "B"], self._facts()) == ["A", "B"]

    def test_any_violation_removed(self):
        cases = [
            {"B": {"aum": 10_000}},                          # 规模过小
            {"B": {"purchase_status": "suspended"}},         # 暂停申购
            {"B": {"daily_limit": 500}},                     # 单日上限过小
            {"B": {"nav_count": 10}},                        # 短历史
        ]
        for patch in cases:
            assert "B" not in apply_hard_filters(["A", "B"], self._facts(patch))

    def test_missing_facts_no_nav_excluded(self):
        """facts 缺失（无净值）→ short_history 违规 → 剔除；有净值者保留。"""
        facts = {"A": {"aum": None, "purchase_status": "unknown",
                       "daily_limit": None, "nav_count": 200}}
        assert apply_hard_filters(["A", "NOPE"], facts) == ["A"]

    def test_zero_nav_count_is_short_history(self):
        """nav_count=0 → short_history 违规（没净值历史的基金不进池）。"""
        facts = {"A": {"aum": None, "purchase_status": "unknown",
                       "daily_limit": None, "nav_count": 0}}
        assert apply_hard_filters(["A"], facts) == []


class TestTopN:
    def test_sorts_and_truncates(self):
        ranked = [{"code": c, "score": s} for c, s in
                  [("A", 0.9), ("B", 0.7), ("C", 0.8), ("D", 0.6)]]
        got = top_n(ranked, n=2)
        assert [g["code"] for g in got] == ["A", "C"]

    def test_pool_size_30(self):
        assert POOL_SIZE == 30

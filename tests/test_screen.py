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
    select_diversified,
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


class TestSelectDiversified:
    """票 02：贪心去相关 + 同类≤2 纯函数。"""

    @staticmethod
    def _ranked(codes_scores):
        return [{"code": c, "final_score": s} for c, s in codes_scores]

    def test_no_constraint_falls_back_to_topn(self):
        """无相关性、无同类 → 等价于纯分数 TopN。"""
        ranked = self._ranked([("A", 0.9), ("B", 0.8), ("C", 0.7),
                              ("D", 0.6), ("E", 0.5)])
        got = select_diversified(ranked, lambda *_: None, lambda *_: None, n=5)
        assert [g["code"] for g in got] == ["A", "B", "C", "D", "E"]

    def test_same_peer_capped_at_two(self):
        """同类超限跳过 → 高分同行业者被低分异行业者顶替（拆散聚堆）。"""
        # 3 白酒（高分）+ 3 医药 + 3 科技，n=5、同类≤2：白酒只取 A/B，
        # C 被拆，由 G（科技）顶替。
        ranked = self._ranked([("A", 0.90), ("B", 0.85), ("C", 0.80),
                              ("D", 0.75), ("E", 0.70), ("F", 0.65),
                              ("G", 0.60), ("H", 0.55), ("I", 0.50)])
        peers = {"A": "白酒", "B": "白酒", "C": "白酒",
                 "D": "医药", "E": "医药", "F": "医药",
                 "G": "科技", "H": "科技", "I": "科技"}
        got = select_diversified(ranked, lambda *_: None, lambda c: peers[c],
                                 max_same_peer=2, n=5)
        codes = [g["code"] for g in got]
        assert codes[:2] == ["A", "B"]
        assert "C" not in codes    # 白酒满 2 被拆
        assert "G" in codes        # 低分科技顶替高分白酒
        assert len(codes) == 5

    def test_high_correlation_skipped(self):
        """相关性超阈跳过 → 高相关聚堆被拆，低相关者入选。"""
        # A 与 B 高相关（0.95）；C/D/E/F 互不相关、与 A 也不相关。
        ranked = self._ranked([("A", 0.90), ("B", 0.80), ("C", 0.70),
                              ("D", 0.60), ("E", 0.50), ("F", 0.40)])
        def corr(c1, c2):
            if {c1, c2} == {"A", "B"}:
                return 0.95
            return 0.0
        got = select_diversified(ranked, corr, lambda *_: None, max_corr=0.85, n=5)
        codes = [g["code"] for g in got]
        assert codes[0] == "A"
        assert "B" not in codes    # 与 A 高相关被拆
        assert "F" in codes        # 低分低相关者顶替
        assert len(codes) == 5

    def test_fallback_fills_when_constraints_block(self):
        """约束卡死凑不齐 → 回退纯分数补满（不因约束推荐不足）。"""
        ranked = self._ranked([("A", 0.9), ("B", 0.8), ("C", 0.7)])
        peer = lambda c: "白酒"  # 全同类，max 2 后卡死
        got = select_diversified(ranked, lambda *_: None, peer, max_same_peer=2, n=5)
        codes = [g["code"] for g in got]
        assert len(codes) == 3   # 只有 3 只可入选，全部补满

    def test_none_corr_no_constraint(self):
        """corr_fn 返回 None → 不约束（数据缺失不误杀）。"""
        ranked = self._ranked([("A", 0.9), ("B", 0.8)])
        got = select_diversified(ranked, lambda *_: None, lambda *_: None, n=2)
        assert [g["code"] for g in got] == ["A", "B"]

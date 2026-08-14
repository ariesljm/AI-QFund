"""R4 报告期守卫 + 对称切片测试（推荐当天不自相矛盾）。

回归根因：推荐当天监控立刻运行，R4 逻辑证伪拿「推荐时锚点（前5大）」对比
「最新前十大」——两者实为同一报告期的不同切片，第 6-10 名的非核心行业股票
被 LLM 放大为"持仓混杂风险"，与推荐理由（只看前5大）同日互相矛盾。
修复：
  1. 报告期守卫：最新持仓报告期 == 锚点报告期（无新披露数据）→ R4 跳过，
     不调 LLM、不产出信号（规则层照常）；
  2. 对称切片：报告期真变化时，锚点持仓取锚点报告期前 10 大（与最新前 10 大对称），
     历史数据缺失时回退快照内 top_holdings（前 5）。
"""

from app.engine.monitor import (DefenseContext, LogicVerificationRule,
                                _format_anchor_holdings, _build_defense_context)

import app.engine.monitor as mon


def _ctx(**kw) -> DefenseContext:
    base = dict(code="A", buy_reason="重仓半导体", sector="半导体",
                entry_snapshot={
                    "rbsa_industry_1": "半导体",
                    "top_holdings": [
                        {"stock_code": "688256", "stock_name": "寒武纪", "weight": 8.0},
                    ],
                    "holdings_report_date": "2026-06-30",
                },
                r4_no_new_data=False, latest_report_date="2026-06-30")
    base.update(kw)
    return DefenseContext(**base)


class TestR4ReportDateGuard:
    """报告期守卫：同报告期跳过证伪，不同报告期正常执行。"""

    def test_same_report_date_skips_without_llm(self, monkeypatch):
        """推荐当天（最新报告期 == 锚点报告期）→ R4 跳过且不调 LLM。"""
        ctx = _ctx(r4_no_new_data=True)
        called = []
        monkeypatch.setattr(mon, "_check_logic_enhanced",
                            lambda c: called.append(c) or {"logic_verdict": "断裂"})
        result = LogicVerificationRule().check(ctx)
        assert result is None
        assert called == []

    def test_same_report_date_skips_even_if_precomputed(self):
        """即使 r4_precomputed=True，守卫仍优先：同报告期不产出信号。"""
        ctx = _ctx(r4_no_new_data=True)
        ctx.r4_precomputed = True
        ctx.r4_logic = {"logic_verdict": "断裂"}
        result = LogicVerificationRule().check(ctx)
        assert result is None

    def test_new_report_date_still_verifies(self, monkeypatch):
        """新报告期数据出现 → R4 正常执行证伪（mock LLM 结果）。"""
        ctx = _ctx(r4_no_new_data=False)
        monkeypatch.setattr(mon, "_check_logic_enhanced",
                            lambda c: {"logic_verdict": "断裂", "reason": "重仓全部退出"})
        result = LogicVerificationRule().check(ctx)
        assert result is not None
        assert result.signal == "EXIT"

    def test_new_report_date_warning_passes_through(self, monkeypatch):
        """新报告期下 LLM 判定 sector_risk → WARNING（守卫只挡无新数据场景）。"""
        ctx = _ctx(r4_no_new_data=False)
        monkeypatch.setattr(mon, "_check_logic_enhanced", lambda c: {
            "logic_verdict": "维持", "signal_hint": "WARNING",
            "sector_risk": True, "reason": "行业暴露下降"})
        result = LogicVerificationRule().check(ctx)
        assert result is not None
        assert result.signal == "WARNING"


class TestAssembleReportDateGuard:
    """装配层：报告期比较 → r4_no_new_data 标志。"""

    def _build(self, monkeypatch, snapshot, latest_date):
        monkeypatch.setattr(mon, "_check_nav_freshness", lambda c, d: (False, ""))
        monkeypatch.setattr(mon, "_nav_since", lambda c, s: [])
        monkeypatch.setattr(mon, "get_latest_features", lambda c: {"date": "2026-08-10"})
        monkeypatch.setattr(mon, "get_entry_feature_snapshot", lambda c: snapshot)
        monkeypatch.setattr(mon, "get_latest_holdings_date", lambda c: latest_date)
        monkeypatch.setattr(mon, "_entry_rbsa", lambda c, d, s: ("半导体", 30.0))
        monkeypatch.setattr(mon, "get_entry_sector_anchor", lambda c, s: None)
        monkeypatch.setattr(mon, "get_entry_score", lambda c: None)
        monkeypatch.setattr(mon, "_current_model_score", lambda f: None)
        monkeypatch.setattr(mon, "get_recent_scores", lambda c, n: [])
        monkeypatch.setattr(mon, "build_holdings_text", lambda c, n: "")
        monkeypatch.setattr(mon, "_rbsa_distribution", lambda f: "")
        return mon._build_defense_context(
            {"code": "A", "name": "甲", "reco_date": "2026-08-13",
             "buy_reason": "逻辑", "sector": "半导体"},
            "2026-08-13", ["2026-08-13"], ["半导体"])

    def test_same_report_date_sets_flag(self, monkeypatch):
        """推荐当天：锚点报告期 == 最新报告期 → r4_no_new_data=True。"""
        ctx = self._build(monkeypatch, {"holdings_report_date": "2026-06-30"}, "2026-06-30")
        assert ctx.r4_no_new_data is True
        assert ctx.latest_report_date == "2026-06-30"

    def test_new_report_date_clears_flag(self, monkeypatch):
        """新季报披露：锚点 2026-06-30 vs 最新 2026-09-30 → 执行证伪。"""
        ctx = self._build(monkeypatch, {"holdings_report_date": "2026-06-30"}, "2026-09-30")
        assert ctx.r4_no_new_data is False

    def test_missing_anchor_report_date_clears_flag(self, monkeypatch):
        """旧快照无 holdings_report_date → 不设守卫（回退原逻辑）。"""
        ctx = self._build(monkeypatch, {"sector": "半导体"}, "2026-06-30")
        assert ctx.r4_no_new_data is False


class TestSymmetricAnchorSlice:
    """对称切片：锚点持仓取锚点报告期前 10，与最新前 10 对称。"""

    def test_anchor_report_slice_used(self, monkeypatch):
        """库中有锚点报告期数据 → 取前 10（对称），不退回快照前 5。"""
        rows = [{"stock_name": f"股{i}", "weight": 10 - i} for i in range(10)]
        monkeypatch.setattr(mon, "get_holdings_at_report",
                            lambda c, d, n: rows if (c, d, n) == ("A", "2026-03-31", 10) else [])
        snapshot = {
            "rbsa_industry_1": "半导体", "holdings_report_date": "2026-03-31",
            "top_holdings": [{"stock_name": "快照股", "weight": 5.0}],
        }
        sector, text, rdate = _format_anchor_holdings(snapshot, "A")
        assert sector == "半导体" and rdate == "2026-03-31"
        assert "股0(10.0%)" in text and "股9(1.0%)" in text
        assert "快照股" not in text

    def test_fallback_to_snapshot_when_missing(self, monkeypatch):
        """库中无锚点报告期数据 → 回退快照 top_holdings（前 5）。"""
        monkeypatch.setattr(mon, "get_holdings_at_report", lambda c, d, n: [])
        snapshot = {
            "rbsa_industry_1": "半导体", "holdings_report_date": "2026-03-31",
            "top_holdings": [
                {"stock_name": "寒武纪", "weight": 8.0},
                {"stock_name": "中际旭创", "weight": 6.0},
            ],
        }
        sector, text, rdate = _format_anchor_holdings(snapshot, "A")
        assert "寒武纪(8.0%)" in text and "中际旭创(6.0%)" in text
        assert rdate == "2026-03-31"

    def test_empty_snapshot_returns_empty(self):
        """无快照 → 空三元组。"""
        assert _format_anchor_holdings(None, "A") == ("", "", "")
        assert _format_anchor_holdings({}, None) == ("", "", "")

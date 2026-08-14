"""架构深化 B：监控装配器（_build_defense_context）单元测试。

回归根因：防线判定是真纯函数且已测，但装配层（位置解包、13 字段快照、
打分先落库再取序列的时序不变量、净值陈旧分支）零测试——真 bug 栖息在
不可测的编排里；get_holding_codes 曾返回裸位置元组，列序是隐式 interface。
修复：装配器收敛为独立可测 module，输入结构化持仓行 → DefenseContext。
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.engine.monitor as mon


def _row(**kw) -> dict:
    base = {"code": "A", "name": "甲", "reco_date": "2026-08-01",
            "buy_reason": "逻辑", "sector": "半导体"}
    base.update(kw)
    return base


class TestAssembleContext:
    """装配器：结构化行 → DefenseContext，字段与 helper 依赖一一对应。"""

    def test_basic_assembly(self, monkeypatch):
        monkeypatch.setattr(mon, "_check_nav_freshness", lambda c, d: (False, ""))
        monkeypatch.setattr(mon, "_nav_since", lambda c, s: [1.0, 1.1])
        monkeypatch.setattr(mon, "get_latest_features", lambda c: {"date": "2026-08-10", "rbsa_industry_1": "半导体"})
        monkeypatch.setattr(mon, "get_entry_feature_snapshot", lambda c: {"sector": "半导体"})
        monkeypatch.setattr(mon, "_entry_rbsa", lambda c, d, s: ("半导体", 30.0))
        monkeypatch.setattr(mon, "get_entry_sector_anchor", lambda c, s: (["半导体"], [], "理由"))
        monkeypatch.setattr(mon, "get_entry_score", lambda c: 0.62)
        monkeypatch.setattr(mon, "get_sector_momentum_median", lambda s, d: 5.0)
        monkeypatch.setattr(mon, "_current_model_score", lambda f: 0.11)
        monkeypatch.setattr(mon, "insert_monitor_score", lambda *a, **k: None)
        monkeypatch.setattr(mon, "get_recent_scores", lambda c, n: [("2026-08-10", 0.11, "v1")])
        monkeypatch.setattr(mon, "build_holdings_text", lambda c, n: "持仓文本")
        monkeypatch.setattr(mon, "_rbsa_distribution", lambda f: "分布")

        ctx = mon._build_defense_context(_row(), "2026-08-10", ["2026-08-10"], ["半导体"])
        assert ctx is not None
        assert ctx.code == "A"
        assert ctx.buy_reason == "逻辑"
        assert ctx.sector == "半导体"
        assert ctx.navs == [1.0, 1.1]
        assert ctx.entry_rbsa == ("半导体", 30.0)
        assert ctx.anchor == (["半导体"], [], "理由")
        assert ctx.entry_score == 0.62
        assert ctx.sector_median == 5.0
        assert ctx.holdings_text == "持仓文本"
        assert ctx.available_sectors == ["半导体"]

    def test_score_persisted_before_series(self, monkeypatch):
        """时序不变量：当日打分先入库，再取序列（序列首位必须是当日分）。"""
        order = []
        monkeypatch.setattr(mon, "_check_nav_freshness", lambda c, d: (False, ""))
        monkeypatch.setattr(mon, "_nav_since", lambda c, s: [])
        monkeypatch.setattr(mon, "get_latest_features", lambda c: {"date": "2026-08-10"})
        monkeypatch.setattr(mon, "get_entry_feature_snapshot", lambda c: {})
        monkeypatch.setattr(mon, "_entry_rbsa", lambda c, d, s: (None, None))
        monkeypatch.setattr(mon, "get_entry_sector_anchor", lambda c, s: None)
        monkeypatch.setattr(mon, "get_entry_score", lambda c: None)
        monkeypatch.setattr(mon, "_current_model_score", lambda f: 0.11)

        def _insert(*a, **k):
            order.append("insert")
        monkeypatch.setattr(mon, "insert_monitor_score", _insert)

        def _series(c, n):
            order.append("series")
            return []
        monkeypatch.setattr(mon, "get_recent_scores", _series)
        monkeypatch.setattr(mon, "build_holdings_text", lambda c, n: "")
        monkeypatch.setattr(mon, "_rbsa_distribution", lambda f: "")

        mon._build_defense_context(_row(), "2026-08-10", ["2026-08-10"], [])
        assert order == ["insert", "series"]

    def test_no_score_skips_persist(self, monkeypatch):
        """当日无模型分（无特征/无模型）→ 不落库、序列照取。"""
        monkeypatch.setattr(mon, "_check_nav_freshness", lambda c, d: (False, ""))
        monkeypatch.setattr(mon, "_nav_since", lambda c, s: [])
        monkeypatch.setattr(mon, "get_latest_features", lambda c: None)
        monkeypatch.setattr(mon, "get_entry_feature_snapshot", lambda c: {})
        monkeypatch.setattr(mon, "_entry_rbsa", lambda c, d, s: (None, None))
        monkeypatch.setattr(mon, "get_entry_sector_anchor", lambda c, s: None)
        monkeypatch.setattr(mon, "get_entry_score", lambda c: None)
        monkeypatch.setattr(mon, "_current_model_score", lambda f: None)
        calls = []
        monkeypatch.setattr(mon, "insert_monitor_score", lambda *a, **k: calls.append(a))
        monkeypatch.setattr(mon, "get_recent_scores", lambda c, n: [])
        monkeypatch.setattr(mon, "build_holdings_text", lambda c, n: "")
        monkeypatch.setattr(mon, "_rbsa_distribution", lambda f: "")

        ctx = mon._build_defense_context(_row(), "2026-08-10", ["2026-08-10"], [])
        assert calls == []
        assert ctx is not None

    def test_stale_returns_none_with_event(self, monkeypatch):
        """净值陈旧 → 返回 None（数据告警事件已记，不参与防线链）。"""
        events = []
        monkeypatch.setattr(mon, "_check_nav_freshness",
                            lambda c, d: (True, "净值陈旧: 数据断裂"))
        monkeypatch.setattr(mon, "_log_monitor_event",
                            lambda *a, **k: events.append((a, k)))
        ctx = mon._build_defense_context(_row(), "2026-08-10", ["2026-08-10"], [])
        assert ctx is None
        assert len(events) == 1
        assert events[0][1].get("is_stale") is True

    def test_sector_median_skipped_when_no_sector(self, monkeypatch):
        """无赛道 → 不查赛道动量中位数。"""
        called = []
        monkeypatch.setattr(mon, "_check_nav_freshness", lambda c, d: (False, ""))
        monkeypatch.setattr(mon, "_nav_since", lambda c, s: [])
        monkeypatch.setattr(mon, "get_latest_features", lambda c: {"date": "2026-08-10"})
        monkeypatch.setattr(mon, "get_entry_feature_snapshot", lambda c: {})
        monkeypatch.setattr(mon, "_entry_rbsa", lambda c, d, s: (None, None))
        monkeypatch.setattr(mon, "get_entry_sector_anchor", lambda c, s: None)
        monkeypatch.setattr(mon, "get_entry_score", lambda c: None)
        monkeypatch.setattr(mon, "_current_model_score", lambda f: None)
        monkeypatch.setattr(mon, "get_sector_momentum_median",
                            lambda s, d: called.append(s))
        monkeypatch.setattr(mon, "get_recent_scores", lambda c, n: [])
        monkeypatch.setattr(mon, "build_holdings_text", lambda c, n: "")
        monkeypatch.setattr(mon, "_rbsa_distribution", lambda f: "")

        ctx = mon._build_defense_context(_row(sector=None), "2026-08-10", ["2026-08-10"], [])
        assert ctx is not None
        assert called == []

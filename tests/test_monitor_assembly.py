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
        monkeypatch.setattr(mon.nav, "series",
                            lambda c, since=None, **kw:
                            [("2026-08-0{:d}".format(i + 1), 1.0 + i * 0.1) for i in range(2)])
        monkeypatch.setattr(mon, "_nav_pre_entry", lambda c, d, n: [])
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
        monkeypatch.setattr(mon, "_nav_pre_entry", lambda c, d, n: [])
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
        monkeypatch.setattr(mon, "_nav_pre_entry", lambda c, d, n: [])
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
        monkeypatch.setattr(mon, "_nav_pre_entry", lambda c, d, n: [])
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


# ───────────────────────────────────────────
# _nav_for_trend — 入场前历史预热补齐（消除新仓位趋势防线空窗）
# ───────────────────────────────────────────

class TestNavForTrend:
    def test_sufficient_post_rows_no_prefetch(self, monkeypatch):
        """入场后净值已 ≥ 预热条数 → 不查入场前历史。"""
        called = []
        def fake_series(code, since=None, **kw):
            return [(f"2026-08-0{i + 1}", 1.0) for i in range(mon.EMA_WARMUP_NAVS)]
        monkeypatch.setattr(mon.nav, "series", fake_series)
        monkeypatch.setattr(mon, "_nav_pre_entry",
                            lambda c, d, n: called.append((c, d, n)) or [])
        nav_dates, navs_trend, navs_post = mon._nav_for_trend("A", "2026-08-01")
        assert len(navs_trend) == mon.EMA_WARMUP_NAVS
        assert navs_trend == navs_post  # 入场后已足：趋势序列与入场后序列一致
        assert len(nav_dates) == len(navs_trend)
        assert called == []

    def test_prepends_pre_entry_history(self, monkeypatch):
        """入场后不足 → 用入场前历史补齐，且排除与入场日重叠的边界行。"""
        post = [2.0, 2.1]
        monkeypatch.setattr(mon.nav, "series",
                            lambda c, since=None, **kw:
                            [("2026-07-30", 2.0), ("2026-07-31", 2.1)])

        from datetime import date, timedelta
        start = date(2026, 5, 1)
        pre_rows = [( (start + timedelta(days=i)).isoformat(), 1.0 + i * 0.001)
                    for i in range(70)]
        # 最后一条是入场日当天（until 含当日）→ 必须被去重排除
        pre_rows.append(("2026-07-31", 1.99))
        fetched = []
        def fake_pre(code, until, limit):
            fetched.append(limit)
            return pre_rows[-(limit):]
        monkeypatch.setattr(mon, "_nav_pre_entry", fake_pre)

        nav_dates, navs_trend, navs_post = mon._nav_for_trend("A", "2026-07-31")
        assert fetched == [mon.EMA_WARMUP_NAVS - len(post) + 1]  # 多取一条用于去重
        assert len(navs_trend) == mon.EMA_WARMUP_NAVS
        assert navs_trend[-len(post):] == post  # 入场后序列完整保留在尾部
        assert navs_post == post  # 硬止损专用序列 = 纯入场后，无预热污染
        assert nav_dates[-len(post):] == ["2026-07-30", "2026-07-31"]

    def test_fund_with_short_total_history_returns_all(self, monkeypatch):
        """总历史都不足预热条数 → 返回全部可得净值（R1 保持不判定，不报错）。"""
        monkeypatch.setattr(mon.nav, "series",
                            lambda c, since=None, **kw: [("2026-07-31", 2.0)])
        monkeypatch.setattr(mon, "_nav_pre_entry",
                            lambda c, d, n: [("2026-07-30", 1.9)])
        nav_dates, navs_trend, navs_post = mon._nav_for_trend("A", "2026-07-31")
        assert navs_trend == [1.9, 2.0]
        assert navs_post == [2.0]  # 入场后仅 1 条（<2 条防御由 hard stop 处理）
        assert nav_dates == ["2026-07-30", "2026-07-31"]

    def test_r1_armed_from_entry_day(self):
        """端到端：买入即处于 EMA60 下方 → R1 首日触发（原逻辑需等 62 个交易日）。"""
        from app.engine.monitor import DefenseContext, EmaTrendRule
        # 62 条：前 60 条缓慢上行至 1.06（EMA ≈ 其下），末 2 条跌破至 0.95/0.94
        warm = [1.0 + i * 0.001 for i in range(60)]
        navs = warm + [0.95, 0.94]
        ctx = DefenseContext(code="A", navs=navs)
        result = EmaTrendRule().check(ctx)
        assert result is not None and result.signal == "EXIT"

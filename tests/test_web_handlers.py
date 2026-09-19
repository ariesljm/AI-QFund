"""Web 面板 handler 与展示 helper 测试——架构深化（候选6）的回归网。

覆盖：fund-detail 成功/失败路径（含监控信号纯文本原因）、_index_context 全链路
组合器、宏观摘要解析、追踪列表统计、alpha 曲线纯函数、display_score 共享换算。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

from app import domain
from app.web import app as webapp
from app.web import charts, dashboard, quotes

# 直接实例化不触发 lifespan（不起调度器线程）
client = TestClient(webapp.app)


def _fake_fund_detail(code):
    return {
        "code": "000001", "name": "测试基金", "type": "混合型",
        "first_date": "2026-07-01", "entry_nav": 1.0,
        "buy_reason": "动量强", "score": 0.3, "combo": 2.0,
        "regime": "BULL", "status": "HOLD",
    }


class TestFundDetail:
    def test_success_with_signal_text_reason(self, monkeypatch):
        """成功路径：监控信号 reason 直接取纯文本（monitor 写的是 \"; \" 拼接原因）。"""
        monkeypatch.setattr(webapp.repo, "get_fund_detail", _fake_fund_detail)
        monkeypatch.setattr(webapp.repo, "get_holdings", lambda code, n: [
            {"stock_code": "600000", "stock_name": "浦发银行", "weight": 5.2, "industry": "银行"},
        ])
        monkeypatch.setattr(webapp.repo.nav, "series", lambda code, limit=None, **kw: [
            ("2026-07-01", 1.0), ("2026-07-02", 1.1),
        ])
        monkeypatch.setattr(webapp.repo, "get_tracked_state", lambda ot, oid: (
            {"state": "EXIT", "date": "2026-07-03",
             "signals_json": '{"drawdown_stop": true, "fatal_news": false, "below_ema20": false, "relative_weak": false, "valuation_pctile": 88.5, "momentum_pos": false, "alpha_neg_days": 0}'}
        ))

        resp = client.get("/api/fund-detail/000001")
        assert resp.status_code == 200
        data = resp.json()
        assert data["fund"]["name"] == "测试基金"
        assert data["fund"]["display_score"] == domain.display_score(2.0, 0.3) == 70
        assert data["top_holdings"][0]["stock_code"] == "600000"
        assert len(data["nav_data"]) == 2
        # 2.0 状态机：signal 取 tracked_states.state，reason 由 signals_json 转中文
        assert data["current_signal"]["signal"] == "EXIT"
        assert "止损8%" in data["current_signal"]["reason"]

    def test_not_found(self, monkeypatch):
        """无推荐记录 → error 提示。"""
        monkeypatch.setattr(webapp.repo, "get_fund_detail", lambda code: None)
        resp = client.get("/api/fund-detail/999999")
        assert resp.status_code == 200
        assert "error" in resp.json()


class TestIndexContext:
    def test_full_context_shape(self, monkeypatch):
        """_index_context 组合器返回全部模板 key（空数据下也安全）。"""
        ctx = dashboard.index_context()
        for key in ("latest", "latest_list", "candidates", "fund_pool",
                    "sector_list", "fund_svg", "alpha_svg",
                    "portfolio_svg", "sharpe_ratio", "max_drawdown",
                    "quality_curve_svg", "now", "today", "reco_status"):
            assert key in ctx


class TestIndexContextBlocks:
    """_index_context 拆分出的窄函数（I3：模板上下文按领域块拆，可独立单测）。"""

    def test_quality_block_empty(self, monkeypatch):
        monkeypatch.setattr(webapp.repo, "get_quality_metrics", lambda n: [])
        (metrics, svg, baseline, ic, xwr, pr) = dashboard.quality_block()
        assert metrics == [] and svg == "" and baseline == 50
        assert ic is None and xwr is None and pr is None

    def test_sector_heatmap_block_projection(self, monkeypatch):
        monkeypatch.setattr(webapp.repo, "get_sector_heatmap", lambda: [
            {"name": "半导体", "weight": 12.345, "momentum": 3.456}])
        rows = dashboard.sector_heatmap_block()
        assert rows == [{"name": "半导体", "weight": 12.3, "momentum": 3.5}]

    def test_portfolio_block_empty(self, monkeypatch):
        monkeypatch.setattr(dashboard, "portfolio_series", lambda: ([], [], []))
        svg, hs_svg, baseline, sharpe, mdd = dashboard.portfolio_block()
        assert svg == "" and hs_svg == "" and baseline == 50
        assert sharpe is None and mdd is None


class TestBuildLatestRecos:
    def test_latest_first_and_fallback_date(self):
        """最新记录为首条；date 缺失时回退 today。"""
        recs = [
            {"id": 2, "code": "BB", "name": "乙", "score": 0.5, "regime": "BEAR",
             "reason": "r2", "status": "HOLD", "date": None, "return": 1.0, "type": "混合"},
            {"id": 1, "code": "AA", "name": "甲", "score": -0.2, "regime": None,
             "reason": "r1", "status": "EXIT", "date": "2026-07-01", "return": None, "type": None},
        ]
        latest, latest_list, latest_rec_id = dashboard.build_latest_recos(recs, "2026-08-05")
        assert latest_rec_id == 2
        assert latest["code"] == "BB"
        assert latest["date"] == "2026-08-05"  # date 缺失回退 today
        assert latest_list[1]["regime"] == "NEUTRAL"  # regime 缺失回退
        assert latest_list[1]["type"] == ""
        assert latest_list[1]["date"] == "2026-07-01"

    def test_empty_returns_zero_id(self):
        """无推荐时 latest=None、id=0。"""
        latest, latest_list, latest_rec_id = dashboard.build_latest_recos([], "2026-08-05")
        assert latest is None and latest_list == [] and latest_rec_id == 0


class TestCandidateSummary:
    def test_return_stats(self, monkeypatch):
        """累计收益/命中率统计。"""
        def fake_summaries(items):
            return {c: {"entry_nav": 1.0, "nav_at_first": 1.0,
                        "latest_nav": 1.2, "signal": None} for c, _ in items}
        monkeypatch.setattr(webapp.repo, "get_candidate_nav_summaries", fake_summaries)
        candidates = [
            {"code": "AAA", "name": "甲", "first_date": "2026-07-01", "rec_count": 1,
             "status": "HOLD", "exit_date": ""},
            {"code": "BBB", "name": "乙", "first_date": "2026-07-02", "rec_count": 2,
             "status": "HOLD", "exit_date": ""},
        ]
        lst, total, n, hit = dashboard.candidate_summary(candidates)
        assert len(lst) == 2
        assert total == pytest.approx(40.0)  # 两只各 +20%
        assert n == 2 and hit == 100.0
        assert lst[0]["status"] == "HOLD"

    def test_status_fallback_to_signal(self, monkeypatch):
        """监控信号优先于推荐状态。"""
        def fake_summaries(items):
            return {c: {"entry_nav": None, "nav_at_first": None,
                        "latest_nav": None, "signal": "EXIT"} for c, _ in items}
        monkeypatch.setattr(webapp.repo, "get_candidate_nav_summaries", fake_summaries)
        lst, _, _, _ = dashboard.candidate_summary([
            {"code": "AAA", "name": "甲", "first_date": "2026-07-01", "rec_count": 1,
             "status": "HOLD", "exit_date": ""},
        ])
        assert lst[0]["status"] == "EXIT"
        assert lst[0]["return"] is None

    def test_first_nav_no_fallback_to_entry_nav(self, monkeypatch):
        """推荐当日净值未出时显示 --（不回退 entry_nav 标记的前一日净值）。"""
        def fake_summaries(items):
            return {c: {"entry_nav": 0.9, "nav_at_first": None,
                        "latest_nav": 1.2, "signal": None} for c, _ in items}
        monkeypatch.setattr(webapp.repo, "get_candidate_nav_summaries", fake_summaries)
        lst, total, n, hit = dashboard.candidate_summary([
            {"code": "AAA", "name": "甲", "first_date": "2026-07-01", "rec_count": 1,
             "status": "HOLD", "exit_date": ""},
        ])
        assert lst[0]["first_nav"] is None  # 当日净值未出 → 不显示
        assert lst[0]["return"] is None


class TestAlphaCurve:
    def test_quality_curve_uses_cum_abs_ret(self):
        """质量曲线消费 quality.py 生成的 points 字段（cum_abs_ret），非旧名 cum_alpha。"""
        pts = [
            {"date": "2026-07-01", "abs_ret": 0.01, "cum_abs_ret": 0.01},
            {"date": "2026-07-02", "abs_ret": -0.005, "cum_abs_ret": 0.005},
            {"date": "2026-07-03", "abs_ret": 0.02, "cum_abs_ret": 0.025},
        ]
        svg, baseline = charts.quality_curve_svg(pts)
        assert svg.startswith("M 0") and " C " in svg
        assert baseline > 90  # 样本全为正收益时 0 线在曲线下方（pad 后仍在视图外）

    def test_quality_curve_empty(self):
        assert charts.quality_curve_svg([]) == ("", 50)

    def test_single_point_flat_line(self):
        """单基金 alpha 曲线为水平线（0% 基线在数据范围外，不做范围断言）。"""
        svg, baseline = charts.smooth_svg_path([5.0])
        assert svg.startswith("M 0,") and "L 200," in svg
        assert baseline > 100  # 单点 +5% 时 0 线远在下方（原行为）

    def test_empty(self):
        assert charts.smooth_svg_path([]) == ("", 50)

    def test_alpha_block_uses_hs300(self, monkeypatch):
        """超额 alpha = 组合累计收益 - 同期沪深300涨幅。"""
        monkeypatch.setattr(webapp.repo, "get_first_reco_date", lambda: "2026-07-01")
        monkeypatch.setattr(webapp.repo, "get_index_close",
                            lambda code, date=None: 3000.0 if date else 3100.0)
        monkeypatch.setattr(webapp.repo, "get_entry_nav", lambda code, date: 1.0)
        monkeypatch.setattr(webapp.repo.nav, "at", lambda code, date: None)
        monkeypatch.setattr(webapp.repo.nav, "latest", lambda code: 1.1)
        candidates = [{"code": "AAA", "name": "甲", "first_date": "2026-07-01",
                       "rec_count": 1, "status": "HOLD", "exit_date": "",
                       "return": 10.0, "first_nav": 1.0}]
        # 组合 +10%，沪深300 +3.33% → alpha ≈ +6.67
        alpha, svg, baseline = dashboard.alpha_block(candidates, 10.0)
        assert alpha == pytest.approx(6.67, abs=0.01)
        assert svg.startswith("M ")


class TestDisplayScore:
    def test_combo_priority(self):
        """有 combo 用 10 倍偏移；无 combo 用 500 倍偏移。"""
        assert domain.display_score(2.0, 0.3) == 70
        assert domain.display_score(None, 0.1) == 100
        assert domain.display_score(None, None) == 0
        assert domain.display_score(None, 0.0) == 0
        assert 0 <= domain.display_score(99.0, 0.0) <= 100


class TestBuildRecoStatus:
    """今日推荐状态：硬闸门拒绝产出必须能被看出来（ticket 25 的 UI 侧收口）。"""

    def test_fresh_when_latest_is_today(self):
        assert dashboard.build_reco_status("2026-09-15", "2026-09-15")["state"] == "fresh"

    def test_pending_when_no_reco(self):
        s = dashboard.build_reco_status(None, "2026-09-15")
        assert s["state"] == "pending" and s["reason_type"] == ""

    def test_stale_latest_is_never_reported_fresh(self):
        """旧推荐（昨天或更早）绝不能被当作今日——否则就是“静默陈旧”的 UI 版。"""
        assert dashboard.build_reco_status("2026-09-04", "2026-09-15")["state"] != "fresh"


class TestRecoStatusReachesPage:
    """模板与上下文的接线回归：状态没渲染出来就等于没有防护。"""

    def test_pending_day_shows_no_reco_notice(self, monkeypatch):
        """无今日推荐时显示“尚未产出推荐”提示（2.0 空态不落库，只有 pending）。"""
        monkeypatch.setattr(webapp.repo, "get_latest_recommendations", lambda _n: [])
        resp = client.get("/")
        assert resp.status_code == 200
        assert "今日尚未产出推荐" in resp.text

    def test_fresh_day_shows_no_notice(self, monkeypatch):
        """今日已有推荐时不得多一条提示（避免永远挂个“无推荐”）。"""
        today = __import__("datetime").datetime.now().strftime("%Y-%m-%d")
        monkeypatch.setattr(webapp.repo, "get_latest_recommendations", lambda _n: [{
            "id": 1, "code": "000001", "name": "测试基金", "score": 0.1,
            "reason": "", "status": "HOLD", "date": today, "return": None, "type": "混合型",
            "regime": "NEUTRAL"}])
        resp = client.get("/")
        assert resp.status_code == 200
        assert "今日尚未产出推荐" not in resp.text


class TestBasicHandlers:
    def test_api_settings_redacts_password(self, monkeypatch):
        """设置接口不泄露 settings_password。"""
        monkeypatch.setattr(webapp, "_load_settings", lambda: {"web": {"settings_password": "secret"}})
        resp = client.get("/api/settings")
        assert resp.status_code == 200
        assert "settings_password" not in resp.json()["web"]


class TestSettingsAuth:
    """方案 A：写操作接口校验 X-Settings-Password 头；密码为空时放行（保持"留空不设密码"语义）。"""

    def test_settings_post_requires_password(self, monkeypatch):
        """设置密码后，无头/错头保存设置返回 403。"""
        monkeypatch.setattr(webapp, "_load_settings", lambda: {"web": {"settings_password": "secret"}})
        resp = client.post("/api/settings", json={"llm": {"model": "x"}})
        assert resp.status_code == 403
        resp = client.post("/api/settings",
                           headers={"X-Settings-Password": "wrong"},
                           json={"llm": {"model": "x"}})
        assert resp.status_code == 403

    def test_settings_post_ok_with_password(self, monkeypatch):
        """携带正确密码头保存设置成功。"""
        monkeypatch.setattr(webapp, "_load_settings", lambda: {"web": {"settings_password": "secret"}})
        monkeypatch.setattr(webapp, "_save_settings", lambda body: True)
        resp = client.post("/api/settings",
                           headers={"X-Settings-Password": "secret"},
                           json={"llm": {"model": "x"}})
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_settings_post_ok_without_password_when_unset(self, monkeypatch):
        """密码未设置时写操作放行。"""
        monkeypatch.setattr(webapp, "_load_settings", lambda: {"web": {"settings_password": ""}})
        monkeypatch.setattr(webapp, "_save_settings", lambda body: True)
        resp = client.post("/api/settings", json={"llm": {"model": "x"}})
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_clear_recommendations_requires_password(self, monkeypatch):
        """设置密码后，清除推荐数据必须带正确密码头。"""
        monkeypatch.setattr(webapp, "_load_settings", lambda: {"web": {"settings_password": "secret"}})
        resp = client.post("/api/clear-recommendations", json={"dry_run": True})
        assert resp.status_code == 403
        resp = client.post("/api/clear-recommendations",
                           headers={"X-Settings-Password": "secret"},
                           json={"dry_run": True})
        assert resp.status_code == 200

    def test_run_pipeline_requires_password(self, monkeypatch):
        """设置密码后，触发管线必须带正确密码头（403 时不启动线程）。"""
        monkeypatch.setattr(webapp, "_load_settings", lambda: {"web": {"settings_password": "secret"}})
        resp = client.post("/api/run-pipeline")
        assert resp.status_code == 403
        # 正确密码路径会真启动管线线程，测试不覆盖，避免污染

    def test_recommendation_status(self):
        resp = client.get("/api/recommendation-status")
        assert resp.status_code == 200
        assert "id" in resp.json() and "updated_at" in resp.json()

    def test_logs_endpoint(self):
        resp = client.get("/api/logs?lines=5")
        assert resp.status_code == 200
        data = resp.json()
        assert "lines" in data and "last_id" in data


class TestIndices:
    """/api/indices：实时抓取（source=live）与降级收盘（source=closed）两路径。"""

    def _clear_cache(self):
        quotes.index_quote.data = None
        quotes.index_quote.expires = 0.0

    def test_live_path(self, monkeypatch):
        """实时源可用：返回两个指数，source=live。"""
        self._clear_cache()
        # 固定为交易时段，避免测试依赖真实时间（非交易时段会短路返回 closed）
        monkeypatch.setattr(quotes, "is_trading_time", lambda: True)
        monkeypatch.setattr(
            quotes.index_quote, "_fetch_live",
            lambda: [
                {"code": "sh000001", "name": "上证指数", "price": 3800.0,
                 "change_percent": -0.4, "source": "live"},
                {"code": "sh000300", "name": "沪深300", "price": 4560.0,
                 "change_percent": 0.6, "source": "live"},
            ],
        )
        resp = client.get("/api/indices")
        assert resp.status_code == 200
        d = resp.json()
        assert d["source"] == "live"
        assert len(d["items"]) == 2
        assert d["items"][0]["code"] == "sh000001"

    def test_fallback_closed(self, monkeypatch):
        """实时源不可用：沪深300/上证指数降级为数据库收盘。"""
        self._clear_cache()
        # 固定为交易时段：真正走 fetch_live 抛异常 → except 降级路径
        # （否则非交易时段会被时间判断短路，测不到异常降级）
        monkeypatch.setattr(quotes, "is_trading_time", lambda: True)
        monkeypatch.setattr(quotes.index_quote, "_fetch_live", lambda: (_ for _ in ()).throw(ConnectionError("断网")))
        monkeypatch.setattr(webapp.repo, "get_index_series", lambda code, cols: [
            ("2026-07-30", 4580.0), ("2026-07-31", 4588.197),
        ])
        resp = client.get("/api/indices")
        assert resp.status_code == 200
        d = resp.json()
        assert d["source"] == "closed"
        items = {it["code"]: it for it in d["items"]}
        assert items["sh000300"]["price"] == 4588.197
        assert items["sh000300"]["change_percent"] == pytest.approx(0.179, abs=0.01)
        assert items["sh000001"]["price"] == 4588.197
        assert items["sh000001"]["change_percent"] == pytest.approx(0.179, abs=0.01)
        assert items["sh000001"]["source"] == "closed"

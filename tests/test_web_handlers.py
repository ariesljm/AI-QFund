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
        monkeypatch.setattr(webapp.repo, "get_nav_history", lambda code, n: [
            ("2026-07-01", 1.0), ("2026-07-02", 1.1),
        ])
        # 即便 detail 形似 JSON 也不解析——回归死代码：monitor 从未写过 JSON
        monkeypatch.setattr(webapp.repo, "get_latest_monitor_event", lambda code: (
            "EXIT", "逻辑判负", True, False,
            '{"reason": "不该被解析"}', "2026-07-03",
        ))

        resp = client.get("/api/fund-detail/000001")
        assert resp.status_code == 200
        data = resp.json()
        assert data["fund"]["name"] == "测试基金"
        assert data["fund"]["display_score"] == domain.display_score(2.0, 0.3) == 70
        assert data["top_holdings"][0]["stock_code"] == "600000"
        assert len(data["nav_data"]) == 2
        # 修复前会 try json.loads 失败回退成原 detail；修复后恒为纯文本原样
        assert data["current_signal"]["reason"] == '{"reason": "不该被解析"}'
        assert data["current_signal"]["signal"] == "EXIT"

    def test_not_found(self, monkeypatch):
        """无推荐记录 → error 提示。"""
        monkeypatch.setattr(webapp.repo, "get_fund_detail", lambda code: None)
        resp = client.get("/api/fund-detail/999999")
        assert resp.status_code == 200
        assert "error" in resp.json()


class TestIndexContext:
    def test_full_context_shape(self, monkeypatch):
        """_index_context 组合器返回全部模板 key（空数据下也安全）。"""
        ctx = webapp._index_context()
        for key in ("latest", "latest_list", "macro", "candidates", "fund_pool",
                    "sector_list", "regime_label", "fund_svg", "alpha_svg",
                    "portfolio_svg", "sharpe_ratio", "max_drawdown",
                    "quality_curve_svg", "empty_today", "now", "today"):
            assert key in ctx


class TestMacroSummary:
    def test_parse_gainers_losers_and_regime(self):
        """领涨/领跌行业解析 + LLM regime 变体归一。"""
        mn = {
            "news_summary": "半导体板块大涨：政策利好\nAI板块走弱：估值回调",
            "top_gainers": "半导体(+3.2%)、白酒(+1.1%)",
            "top_losers": "煤炭(-2.5%)、房地产(-1.2%)",
            "regime_label": "bullish",
        }
        m = webapp._macro_summary(mn)
        assert m["regime_label"] == "BULL"
        assert m["sector_gainers"][0]["name"] == "半导体"
        assert m["sector_gainers"][0]["pct"] == "+3.20%"
        # 领跌按跌幅从小到大排列后反转 → 最深跌幅排最后
        assert [s["name"] for s in m["sector_losers"]] == ["房地产", "煤炭"]
        # 快讯拆分为「标题 + 摘要」对象，冒号前为标题、冒号后为摘要
        assert len(m["macro"]["news_items"]) == 2
        assert m["macro"]["news_items"][0] == {"title": "半导体板块大涨", "summary": "政策利好"}
        assert m["macro"]["news_items"][1] == {"title": "AI板块走弱", "summary": "估值回调"}

    def test_news_without_colon_uses_full_line(self):
        """快讯行无冒号时整行作标题与摘要（弹出窗仍可展示完整内容）。"""
        m = webapp._macro_summary({"news_summary": "央行开展公开市场操作"})
        assert m["macro"]["news_items"][0] == {"title": "央行开展公开市场操作", "summary": "央行开展公开市场操作"}
        assert m["macro"]["news"] == "央行开展公开市场操作"

    def test_empty_macro(self):
        """无宏观数据 → 默认值。"""
        m = webapp._macro_summary(None)
        assert m["regime_label"] == "NEUTRAL"
        assert m["macro"]["news"] == "暂无快讯"
        assert m["flow_inflows"] == []
        assert m["max_inflow"] == 0


class TestCandidateSummary:
    def test_return_stats(self, monkeypatch):
        """累计收益/命中率统计。"""
        monkeypatch.setattr(webapp.repo, "get_latest_signal", lambda code: None)
        monkeypatch.setattr(webapp.repo, "get_entry_nav", lambda code, date: 1.0)
        monkeypatch.setattr(webapp.repo, "get_nav_at_date", lambda code, date: None)
        monkeypatch.setattr(webapp.repo, "get_latest_nav", lambda code: 1.2)
        candidates = [
            {"code": "AAA", "name": "甲", "first_date": "2026-07-01", "rec_count": 1,
             "status": "HOLD", "exit_date": ""},
            {"code": "BBB", "name": "乙", "first_date": "2026-07-02", "rec_count": 2,
             "status": "HOLD", "exit_date": ""},
        ]
        lst, total, n, hit = webapp._candidate_summary(candidates)
        assert len(lst) == 2
        assert total == pytest.approx(40.0)  # 两只各 +20%
        assert n == 2 and hit == 100.0
        assert lst[0]["status"] == "HOLD"

    def test_status_fallback_to_signal(self, monkeypatch):
        """监控信号优先于推荐状态。"""
        monkeypatch.setattr(webapp.repo, "get_latest_signal", lambda code: "EXIT")
        monkeypatch.setattr(webapp.repo, "get_entry_nav", lambda code, date: None)
        monkeypatch.setattr(webapp.repo, "get_nav_at_date", lambda code, date: None)
        monkeypatch.setattr(webapp.repo, "get_latest_nav", lambda code: None)
        lst, _, _, _ = webapp._candidate_summary([
            {"code": "AAA", "name": "甲", "first_date": "2026-07-01", "rec_count": 1,
             "status": "HOLD", "exit_date": ""},
        ])
        assert lst[0]["status"] == "EXIT"
        assert lst[0]["return"] is None


class TestAlphaCurve:
    def test_single_point_flat_line(self):
        """单基金 alpha 曲线为水平线（0% 基线在数据范围外，不做范围断言）。"""
        svg, baseline = webapp._alpha_curve_svg([5.0])
        assert svg.startswith("M 0,") and "L 200," in svg
        assert baseline > 100  # 单点 +5% 时 0 线远在下方（原行为）

    def test_empty(self):
        assert webapp._alpha_curve_svg([]) == ("", 50)

    def test_alpha_block_uses_hs300(self, monkeypatch):
        """超额 alpha = 组合累计收益 - 同期沪深300涨幅。"""
        monkeypatch.setattr(webapp.repo, "get_first_reco_date", lambda: "2026-07-01")
        monkeypatch.setattr(webapp.repo, "get_index_close",
                            lambda code, date=None: 3000.0 if date else 3100.0)
        monkeypatch.setattr(webapp.repo, "get_entry_nav", lambda code, date: 1.0)
        monkeypatch.setattr(webapp.repo, "get_nav_at_date", lambda code, date: None)
        monkeypatch.setattr(webapp.repo, "get_latest_nav", lambda code: 1.1)
        monkeypatch.setattr(webapp.repo, "get_latest_signal", lambda code: None)
        candidates = [{"code": "AAA", "name": "甲", "first_date": "2026-07-01",
                       "rec_count": 1, "status": "HOLD", "exit_date": "",
                       "return": 10.0, "first_nav": 1.0}]
        # 组合 +10%，沪深300 +3.33% → alpha ≈ +6.67
        alpha, svg, baseline = webapp._alpha_block(candidates, 10.0)
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


class TestBasicHandlers:
    def test_api_settings_redacts_password(self, monkeypatch):
        """设置接口不泄露 settings_password。"""
        monkeypatch.setattr(webapp, "_load_settings", lambda: {"web": {"settings_password": "secret"}})
        resp = client.get("/api/settings")
        assert resp.status_code == 200
        assert "settings_password" not in resp.json()["web"]

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
        webapp._index_quote.data = None
        webapp._index_quote.expires = 0.0

    def test_live_path(self, monkeypatch):
        """实时源可用：返回两个指数，source=live。"""
        self._clear_cache()
        monkeypatch.setattr(
            webapp._index_quote, "_fetch_live",
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
        """实时源不可用：沪深300 降级为数据库收盘，上证标记 unavailable。"""
        self._clear_cache()
        monkeypatch.setattr(webapp._index_quote, "_fetch_live", lambda: (_ for _ in ()).throw(ConnectionError("断网")))
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
        assert items["sh000001"]["source"] == "unavailable"
        assert items["sh000001"]["price"] is None

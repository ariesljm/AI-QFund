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
from app.web import app as webapp, dashboard, charts, quotes

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
        # 即便 detail 形似 JSON 也不解析——回归死代码：monitor 从未写过 JSON
        monkeypatch.setattr(webapp.repo, "get_latest_monitor_event", lambda code: (
            {"signal": "EXIT", "logic_verdict": "逻辑判负", "sector_risk": True,
             "holding_risk": False, "detail": '{"reason": "不该被解析"}', "date": "2026-07-03"}
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
        ctx = dashboard.index_context()
        for key in ("latest", "latest_list", "macro", "candidates", "fund_pool",
                    "sector_list", "regime_label", "fund_svg", "alpha_svg",
                    "portfolio_svg", "sharpe_ratio", "max_drawdown",
                    "quality_curve_svg", "empty_today", "now", "today"):
            assert key in ctx


class TestIndexContextBlocks:
    """_index_context 拆分出的窄函数（I3：模板上下文按领域块拆，可独立单测）。"""

    def test_macro_block_empty(self, monkeypatch):
        monkeypatch.setattr(webapp.repo, "get_latest_macro_news", lambda: None)
        monkeypatch.setattr(webapp.repo, "get_empty_recommendation", lambda d: None)
        (macro, gainers, losers, inflow, outflow, max_in,
         max_out, reasoning, regime, empty, flow_net, macro_date) = dashboard.macro_block("2026-08-08")
        assert regime == domain.REGIME_NEUTRAL
        assert empty is None
        assert max_in == 0 and max_out == 0
        assert flow_net is None
        assert macro_date == ""

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


class TestMacroSummary:
    def test_parse_gainers_losers_and_regime(self):
        """领涨/领跌行业解析 + LLM regime 变体归一。"""
        mn = {
            "news_summary": "半导体板块大涨：政策利好\nAI板块走弱：估值回调",
            "top_gainers": "半导体(+3.2%)、白酒(+1.1%)",
            "top_losers": "煤炭(-2.5%)、房地产(-1.2%)",
            "regime_label": "bullish",
        }
        m = domain.parse_macro_summary(mn)
        assert m["regime_label"] == "BULL"
        assert m["sector_gainers"][0]["name"] == "半导体"
        assert m["sector_gainers"][0]["pct"] == "+3.20%"
        # 领跌按跌幅从小到大排列后反转 → 最深跌幅排最后
        assert [s["name"] for s in m["sector_losers"]] == ["房地产", "煤炭"]
        # 快讯拆分为「标题 + 摘要」对象：冒号前为标题，摘要保留完整行
        assert len(m["macro"]["news_items"]) == 2
        assert m["macro"]["news_items"][0] == {"title": "半导体板块大涨", "summary": "半导体板块大涨：政策利好"}
        assert m["macro"]["news_items"][1] == {"title": "AI板块走弱", "summary": "AI板块走弱：估值回调"}

    def test_news_long_line_title_truncation(self):
        """快讯行无冒号且为长句：标题截取到第一句标点/限长，摘要保留完整内容（含时间戳）。"""
        raw = "[05:38] 持有67%仓位押注AI概念，同时也加杠杆，资产管理规模暴跌 这意味着什么？后续还有更多细节内容补充说明。"
        m = domain.parse_macro_summary({"news_summary": raw})
        item = m["macro"]["news_items"][0]
        assert item["title"].startswith("持有67%仓位押注AI概念")
        assert "？" not in item["title"]  # 标题不含第一句之后的标点
        assert item["title"] != item["summary"]  # 摘要必须比标题长
        assert item["summary"] == raw  # 摘要 = 完整行
        assert "后续还有更多细节" in item["summary"]

    def test_news_without_colon_uses_full_line(self):
        """快讯行无冒号时整行作标题与摘要（弹出窗仍可展示完整内容）。"""
        m = domain.parse_macro_summary({"news_summary": "央行开展公开市场操作"})
        assert m["macro"]["news_items"][0] == {"title": "央行开展公开市场操作", "summary": "央行开展公开市场操作"}
        assert m["macro"]["news"] == "央行开展公开市场操作"

    def test_sector_reasoning_regime_chinese(self):
        """AI赛道分析中的英文大盘状态词替换为中文（熊市/牛市/中性）。"""
        m = domain.parse_macro_summary({
            "news_summary": "新闻",
            "sector_reasoning": "半导体领涨，但大盘判定为bearish，消费板块中性观望，注意bull陷阱",
        })
        sr = m["sector_reasoning"]
        assert "熊市" in sr and "bearish" not in sr
        assert "中性" in sr and "neutral" not in sr
        assert "牛市" in sr and "bull" not in sr

    def test_zh_regime_replacements(self):
        """替换函数：大小写不敏感、无匹配原样返回。"""
        assert domain.zh_regime("判定为 Bearish 和 BULL market") == "判定为 熊市 和 牛市 market"
        assert domain.zh_regime("无英文大盘词") == "无英文大盘词"
        assert domain.zh_regime("") == ""
        assert domain.zh_regime(None) is None

    def test_empty_macro(self):
        """无宏观数据 → 默认值。"""
        m = domain.parse_macro_summary(None)
        assert m["regime_label"] == "NEUTRAL"
        assert m["macro"]["news"] == "暂无快讯"
        assert m["flow_inflows"] == []
        assert m["max_inflow"] == 0


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

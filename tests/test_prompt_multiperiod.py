"""Ticket 07：LLM 素材与 prompt 多周期化测试（S4 纯文本 seam + 装配集成）。

契约：
- 候选池素材每赛道携带趋势质量标签与近 5 日资金流趋势（非裸动量）
- 选赛道 prompt 口径改为"未来 1-2 个月上涨潜力"，当日新闻降级为确认/否决
- 提供近 N 日要闻回顾素材（趋势视角，非单日脉冲）
- 候选池普遍高位时允许少选/空推荐
- 终选 prompt：不再以"近 1 月涨幅"为推荐依据，改用相对赛道超额；尊重量化排序前 30%
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.engine import macro_agent
from app.engine.sector_pool import SectorPool, SectorSignal
from app.llm.context import news_theme_summary, pool_text
from app.llm.prompts import final_pick_prompt, sector_selection_prompt

_CTX = SimpleNamespace(sector_reasoning="半导体景气延续", recommended_sectors=["半导体"],
                       risk_sectors=[], regime_label="BEAR")


class TestPoolText:
    def test_label_and_flow_rendered(self):
        """候选池文本含趋势质量标签与近 5 日资金流。"""
        rows = [("半导体", 3.2, 5.0, 8.0, 6, ["追高降权"], "高位降权", 1234.0)]
        text = pool_text(rows)
        assert "高位降权" in text
        assert "近5日资金" in text and "+1234" in text

    def test_legacy_six_tuple_still_works(self):
        """兼容缺省标签/资金流的调用（不破坏既有测试）。"""
        text = pool_text([("食品", 1.5, 0.5, 4.0, 6, [])])
        assert "食品" in text


class TestNewsThemeSummary:
    def test_renders_recent_dates(self, monkeypatch):
        """近 N 日要闻：按日期聚合为可读回顾。"""
        monkeypatch.setattr(
            macro_agent.repo, "get_recent_macro_news",
            lambda days: [("2026-09-03", "政策刺激半导体"), ("2026-09-02", "地产新规")])
        out = news_theme_summary(days=7)
        assert "2026-09-03" in out and "2026-09-02" in out
        assert "政策刺激半导体" in out


class TestSectorSelectionPrompt:
    def test_multiperiod_regime_and_downweight(self):
        """口径：近 20 日多周期数据 + 未来 1-2 个月潜力 + 当日新闻降权 + 可少选。"""
        p = sector_selection_prompt(
            date_str="2026-09-03", pool_text="半导体(5日+3.2%)", pool_reasoning="量化定池: x",
            top_gainers="", top_losers="", etf_net_flow="", news_summary="当日要闻",
            news_history="2026-09-02 政策刺激半导体",
        )
        assert "1-2 个月" in p
        assert "20 个交易日" in p
        assert "确认或否决" in p
        assert "宁可少选" in p
        assert "政策刺激半导体" in p  # 历史素材注入

    def test_news_history_none_ok(self):
        """无历史新闻时 prompt 不崩（无记录场景）。"""
        p = sector_selection_prompt(
            date_str="2026-09-03", pool_text="x", pool_reasoning="r",
            top_gainers="", top_losers="", etf_net_flow="", news_summary="当日要闻")
        assert "1-2 个月" in p


class TestFinalPickPrompt:
    def test_no_recent_1m_as_basis(self):
        """终选不再以近 1 月涨幅为推荐依据（素材数值与旧指令均移除）。"""
        p = final_pick_prompt([{"code": "F1", "name": "n1", "sector": "半导体",
                                "calmar": 1.0, "hurst_60d": 0.6, "combo": 0.7,
                                "momentum_20d": 5.0, "ret_1m": 3.0,
                                "sector_median_mom": 1.0, "mom_gap": 4.0,
                                "holdings": [], "report_date": "2026-06-30",
                                "holdings_months": 2}],
                               _CTX, [])
        assert "近1月涨幅+3.0%" not in p  # 素材数值不再渲染
        assert "reason 文案引用此值" not in p  # 旧指令移除
        assert "排序前 30%" in p

    def test_alpha_material_present(self):
        """终选素材含相对赛道超额（α）口径说明。"""
        p = final_pick_prompt([{"code": "F1", "name": "n1", "sector": "半导体",
                                "calmar": 1.0, "hurst_60d": 0.6, "combo": 0.7,
                                "momentum_20d": 5.0, "ret_1m": 3.0,
                                "sector_median_mom": 1.0, "mom_gap": 4.0,
                                "holdings": [], "report_date": "2026-06-30",
                                "holdings_months": 2}],
                               _CTX, [])
        assert "相对赛道" in p or "赛道中位" in p or "超额" in p


def _ctx():
    return SectorPool(
        date="2026-09-03", regime="BEAR",
        candidates=[SectorSignal(sector="半导体", mom_5d=3.2, mom_20d=5.0,
                                 mom_60d=8.0, n=6, score=3.2,
                                 trend_label="趋势健康", flow_5d=1234.0)],
        excluded=[], reasoning="量化定池: 候选1个, regime=BEAR")


class TestMacroAgentAssetBuilder:
    def test_build_sector_prompt_uses_trend_material(self, monkeypatch):
        """装配：选赛道 prompt 实际携带趋势标签/资金流/历史新闻（素材集成）。"""
        monkeypatch.setattr(macro_agent, "_load_sector_insights", lambda: [])
        monkeypatch.setattr(macro_agent, "_load_available_sectors",
                            lambda: ["半导体"])
        monkeypatch.setattr(macro_agent.repo, "get_market_technical", lambda: None)
        monkeypatch.setattr(macro_agent.repo, "get_recent_macro_news",
                            lambda days=7: [("2026-09-02", "半导体政策支持")])
        pool = _ctx()
        news = {"summary": "当日要闻", "top_gainers": "", "top_losers": "",
                "etf_net_flow": "", "news_date": "2026-09-03"}
        flow = {"summary": "", "top_flows": [], "top_outflows": []}
        prompt, _used = macro_agent._build_sector_prompt(
            "2026-09-03", news, flow, pool)
        assert "趋势健康" in prompt
        assert "近5日资金" in prompt
        assert "半导体政策支持" in prompt

"""T1 空推荐日优雅处理测试。

核心区分：LLM 显式返回空赛道（合法业务决策）≠ LLM 调用/解析失败（技术异常）。
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json

import pytest

from app.llm import macro_agent
from app.engine.sector_pool import SectorPool, SectorSignal
import app.database as db_mod
from app import repo


class TestSuggestQuantEmptyDecision:
    """_suggest_quant：空赛道是合法决策，不抛异常；失败才抛。

    D5 后量化定池先行：测试固定 patch 定池返回非空候选池，
    聚焦 LLM 解析逻辑；池空分支单独用例覆盖。
    """

    @staticmethod
    def _pool():
        return SectorPool(
            date="2026-08-01", regime="BULL",
            candidates=[
                SectorSignal(sector="半导体", mom_5d=3.2, mom_20d=1.0, mom_60d=8.0, n=5, score=3.2),
                SectorSignal(sector="食品", mom_5d=1.5, mom_20d=0.5, mom_60d=4.0, n=6, score=1.5),
                SectorSignal(sector="饮料", mom_5d=0.8, mom_20d=0.2, mom_60d=3.0, n=4, score=0.8),
            ],
            excluded=[], reasoning="量化定池: 候选3个, regime=BULL")

    def _suggest(self, monkeypatch, llm_content):
        monkeypatch.setattr(macro_agent, "call_llm", lambda *a, **k: llm_content)
        monkeypatch.setattr(macro_agent, "build_sector_pool", lambda date_str: self._pool())
        monkeypatch.setattr(macro_agent, "_load_sector_insights", lambda: [])
        monkeypatch.setattr(macro_agent, "_load_available_sectors", lambda: ["食品", "饮料", "半导体"])
        news = {"summary": "今日新闻", "top_gainers": "", "top_losers": "", "etf_net_flow": ""}
        flow = {"summary": "", "top_flows": [], "top_outflows": []}
        return macro_agent._suggest_quant("2026-08-01", news, flow)

    def test_explicit_empty_returns_empty_ctx(self, monkeypatch):
        """LLM 显式返回空赛道 + 原因 → 返回空 ctx，不抛异常。"""
        content = json.dumps({
            "recommended_sectors": [], "risk_sectors": [],
            "regime_label": "neutral", "reasoning": "今日无合适机会",
        })
        ctx = self._suggest(monkeypatch, content)
        assert ctx.recommended_sectors == []
        assert ctx.sector_reasoning == "今日无合适机会"

    def test_empty_pool_skips_llm(self, monkeypatch):
        """D5：量化定池无候选 → 主路径空推荐日，不因 LLM 失败而崩溃。"""
        calls = {"n": 0}
        def fake_llm(*a, **k):
            calls["n"] += 1
            return None
        monkeypatch.setattr(macro_agent, "call_llm", fake_llm)
        monkeypatch.setattr(macro_agent, "build_sector_pool", lambda date_str: SectorPool(
            date="2026-08-01", regime="BEAR", candidates=[],
            excluded=[{"sector": "食品", "reason": "5日动量不足"}],
            reasoning="量化定池: 无满足门槛的赛道"))
        monkeypatch.setattr(macro_agent, "_load_sector_insights", lambda: [])
        news = {"summary": "x", "top_gainers": "", "top_losers": "", "etf_net_flow": ""}
        flow = {"summary": "", "top_flows": [], "top_outflows": []}
        ctx = macro_agent._suggest_quant("2026-08-01", news, flow)
        assert ctx.recommended_sectors == []
        assert "量化定池" in ctx.sector_reasoning
        assert calls["n"] == 0   # 池空纯量化判定，不调 LLM

    def test_call_failure_raises(self, monkeypatch):
        """call_llm 返回 None（调用失败）→ 抛异常（不降级、不兜底）。"""
        with pytest.raises(RuntimeError):
            self._suggest(monkeypatch, None)

    def test_parse_failure_raises(self, monkeypatch):
        """非法 JSON → 抛异常（不降级、不兜底）。"""
        with pytest.raises(RuntimeError):
            self._suggest(monkeypatch, "not json{{{")

    def test_unavailable_sectors_fall_back_to_empty(self, monkeypatch):
        """LLM 推荐了池外赛道 → 作为空推荐日处理（不再抛异常崩溃管线）。"""
        content = json.dumps({
            "recommended_sectors": ["不存在赛道"], "risk_sectors": [],
            "regime_label": "neutral", "reasoning": "x",
        })
        ctx = self._suggest(monkeypatch, content)
        assert ctx.recommended_sectors == []
        # 空推荐日可回溯：reasoning 保留 LLM 原文或注明原因
        assert ctx.sector_reasoning

    def test_alias_sector_resolved(self, monkeypatch):
        """C3：LLM 自由名经别名映射解析到 RBSA 行业（芯片→半导体），且必须在池内。"""
        content = json.dumps({
            "recommended_sectors": ["芯片"], "risk_sectors": [],
            "regime_label": "neutral", "reasoning": "x",
        })
        ctx = self._suggest(monkeypatch, content)
        assert ctx.recommended_sectors == ["半导体"]

    def test_vetoed_sector_excluded(self, monkeypatch):
        """D5：LLM 否决的池内赛道不进推荐，vetoed 记录保留（否决质量可度量）。"""
        content = json.dumps({
            "recommended_sectors": ["半导体", "食品"], "risk_sectors": [],
            "vetoed_sectors": [{"sector": "食品", "reason": "新闻明确利空"}],
            "regime_label": "neutral", "reasoning": "x",
        })
        ctx = self._suggest(monkeypatch, content)
        assert ctx.recommended_sectors == ["半导体"]
        assert ctx.vetoed_sectors == [{"sector": "食品", "reason": "新闻明确利空"}]
        assert len(ctx.candidate_sectors) == 3


class TestEmptyRecommendationRecord:
    """repo 记录/读取空推荐日（历史可回溯）。"""

    def test_round_trip(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "test.db")
        repo.record_empty_recommendation("2026-08-01", "今日无合适机会")
        assert repo.get_empty_recommendation("2026-08-01") == {
            "date": "2026-08-01", "reasoning": "今日无合适机会",
        }

    def test_history_retained(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "test.db")
        repo.record_empty_recommendation("2026-07-01", "七月无机会")
        repo.record_empty_recommendation("2026-08-01", "八月无机会")
        assert repo.get_empty_recommendation("2026-07-01")["reasoning"] == "七月无机会"
        assert repo.get_empty_recommendation()["date"] == "2026-08-01"
        assert repo.get_empty_recommendation("2026-08-02") is None

    def test_upsert_same_day(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "test.db")
        repo.record_empty_recommendation("2026-08-01", "原因A")
        repo.record_empty_recommendation("2026-08-01", "原因B")
        rows = repo.get_empty_recommendation("2026-08-01")
        assert rows["reasoning"] == "原因B"

    def test_get_when_absent(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "test.db")
        assert repo.get_empty_recommendation() is None


class TestSectorPoolFallback:
    """量化定池跨日回退：决策日无特征 → 回退最近特征日，不误判空池。"""

    def test_falls_back_when_no_features(self, monkeypatch):
        from app.engine import sector_pool as sp
        called_with = {}

        monkeypatch.setattr(sp.repo, "get_available_sectors", lambda: ["食品", "半导体"])
        monkeypatch.setattr(sp.repo, "get_market_regime", lambda: "BEAR")
        monkeypatch.setattr(sp.repo, "get_latest_feature_date_before", lambda d: "2026-08-11")

        def fake_medians(sector, date):
            called_with["date"] = date
            return {"mom_5d": 2.0, "mom_20d": -1.0, "mom_60d": -3.0, "n": 10}
        monkeypatch.setattr(sp.repo, "get_sector_momentum_medians", fake_medians)

        pool = sp.build_sector_pool("2026-08-12")
        assert pool.date == "2026-08-11"      # 回退到最近特征日
        assert called_with["date"] == "2026-08-11"  # 查询用回退日
        assert len(pool.candidates) == 2
        assert pool.regime == "BEAR"

    def test_keeps_date_when_features_exist(self, monkeypatch):
        from app.engine import sector_pool as sp
        monkeypatch.setattr(sp.repo, "get_available_sectors", lambda: ["食品"])
        monkeypatch.setattr(sp.repo, "get_market_regime", lambda: "BULL")
        monkeypatch.setattr(sp.repo, "get_latest_feature_date_before", lambda d: d)

        def fake_medians(sector, date):
            return {"mom_5d": 1.5, "mom_20d": 0.5, "mom_60d": 2.0, "n": 5}
        monkeypatch.setattr(sp.repo, "get_sector_momentum_medians", fake_medians)

        pool = sp.build_sector_pool("2026-08-11")
        assert pool.date == "2026-08-11"      # 有特征时用决策日本身
        assert len(pool.candidates) == 1

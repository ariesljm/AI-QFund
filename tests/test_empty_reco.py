"""T1 空推荐日优雅处理测试。

核心区分：LLM 显式返回空赛道（合法业务决策）≠ LLM 调用/解析失败（技术异常）。
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json

import pytest

from app.llm import macro_agent
import app.database as db_mod
from app import repo


class TestSuggestSectorsEmptyDecision:
    """_suggest_sectors：空赛道是合法决策，不抛异常；失败才抛。"""

    def _suggest(self, monkeypatch, llm_content):
        monkeypatch.setattr(macro_agent, "call_llm", lambda *a, **k: llm_content)
        monkeypatch.setattr(macro_agent, "_load_sector_insights", lambda: "")
        monkeypatch.setattr(macro_agent, "_load_available_sectors", lambda: ["食品", "饮料", "半导体"])
        news = {"summary": "今日新闻", "top_gainers": "", "top_losers": "", "etf_net_flow": ""}
        flow = {"summary": "", "top_flows": [], "top_outflows": []}
        return macro_agent._suggest_sectors("2026-08-01", news, flow)

    def test_explicit_empty_returns_empty_ctx(self, monkeypatch):
        """LLM 显式返回空赛道 + 原因 → 返回空 ctx，不抛异常。"""
        content = json.dumps({
            "recommended_sectors": [], "risk_sectors": [],
            "regime_label": "neutral", "reasoning": "今日无合适机会",
        })
        ctx = self._suggest(monkeypatch, content)
        assert ctx.recommended_sectors == []
        assert ctx.sector_reasoning == "今日无合适机会"

    def test_call_failure_raises(self, monkeypatch):
        """call_llm 返回 None（调用失败）→ 抛异常。"""
        with pytest.raises(RuntimeError):
            self._suggest(monkeypatch, None)

    def test_parse_failure_raises(self, monkeypatch):
        """非法 JSON → 抛异常。"""
        with pytest.raises(RuntimeError):
            self._suggest(monkeypatch, "not json{{{")

    def test_unavailable_sectors_raise(self, monkeypatch):
        """LLM 推荐了不可投赛道 → 抛异常（技术异常，非空推荐）。"""
        content = json.dumps({
            "recommended_sectors": ["不存在赛道"], "risk_sectors": [],
            "regime_label": "neutral", "reasoning": "x",
        })
        with pytest.raises(RuntimeError):
            self._suggest(monkeypatch, content)


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

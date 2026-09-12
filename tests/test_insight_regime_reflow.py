"""ticket 13 教训 prompt 回流测试：按当前市场状态优选注入。

覆盖：高置信度筛选、regime 匹配优先、condition 字段匹配、ranking 类型排除、
无匹配优雅降级、空表安全，以及 engine 层传递 regime。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import app.repo as repo
from app.database import db_conn


@pytest.fixture(autouse=True)
def _clean_insights():
    with db_conn() as conn:
        conn.execute("DELETE FROM evolution_insights")
    yield


def _insert(insight: str, insight_type: str = "sector", confidence: float = 1.0,
            condition: str | None = None, created: str = "2026-06-01") -> None:
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO evolution_insights "
            "(insight, insight_type, confidence, created_date, active, condition) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (insight, insight_type, confidence, created, condition),
        )


class TestRegimeInsightSelection:
    def test_regime_matching_prioritized_over_recency(self):
        """提及当前市场状态的教训优先注入（即使更旧）。"""
        _insert("熊市教训：降低仓位", created="2026-06-10")
        _insert("牛市教训：持有成长赛道", created="2026-06-01")
        _insert("通用教训：控制换手率", created="2026-06-05")

        texts = [t for _, t in repo.get_active_insights(limit=3, regime_label="牛市")]

        assert texts[0] == "牛市教训：持有成长赛道"
        assert len(texts) == 3

    def test_condition_field_counts_as_match(self):
        """condition JSON 文本提及 regime 也算匹配。"""
        _insert("模糊教训", condition='{"condition": "当前为熊市", "action": "空仓"}',
                created="2026-06-01")
        _insert("普通教训", created="2026-06-10")

        rows = repo.get_active_insights(limit=2, regime_label="熊市")

        assert rows[0][1] == "模糊教训"

    def test_low_confidence_filtered(self):
        _insert("低置信教训", confidence=0.2)
        _insert("高置信教训", confidence=0.9)
        texts = [t for _, t in repo.get_active_insights(limit=8)]
        assert "低置信教训" not in texts
        assert "高置信教训" in texts

    def test_ranking_type_excluded(self):
        _insert("排分诊断文本", insight_type="ranking")
        _insert("投资教训", insight_type="sector")
        texts = [t for _, t in repo.get_active_insights(limit=8)]
        assert "排分诊断文本" not in texts
        assert "投资教训" in texts

    def test_no_regime_falls_back_to_recency(self):
        _insert("旧教训", created="2026-06-01")
        _insert("新教训", created="2026-06-20")
        texts = [t for _, t in repo.get_active_insights(limit=8, regime_label=None)]
        assert texts[0] == "新教训"

    def test_regime_with_no_match_returns_others(self):
        """regime 无匹配时不报错，返回其余教训补足（优雅降级）。"""
        _insert("普通教训A", created="2026-06-01")
        _insert("普通教训B", created="2026-06-02")
        rows = repo.get_active_insights(limit=8, regime_label="牛市")
        assert len(rows) == 2

    def test_empty_table_returns_empty(self):
        assert repo.get_active_insights(limit=8, regime_label="牛市") == []


class TestLoadInsightsPassesRegime:
    def test_load_insights_forwards_regime_label(self, monkeypatch):
        seen: dict = {}

        def fake_get(limit=8, exclude_ranking=True, regime_label=None):
            seen["regime"] = regime_label
            return [(1, "教训A")]

        monkeypatch.setattr(repo, "get_active_insights", fake_get)
        monkeypatch.setattr(repo, "mark_insights_applied", lambda ids, d: None)

        from app.engine import recommend
        out = recommend._load_insights("牛市")

        assert seen["regime"] == "牛市"
        assert out == ["教训A"]

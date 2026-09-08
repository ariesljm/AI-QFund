"""#3 新闻主题聚合（方向 A：政策持续性 vs 媒体情绪过热）测试。

覆盖：词典命中、政策持续性识别、媒体过热识别、持续性过滤（单日闪现不入选）、
政策词优先于情绪词、原 news_theme_summary 降级路径不变。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.repo as repo
from app import domain
from app.llm.context import (
    extract_sector_mentions,
    news_theme_summary,
    sector_theme_summary,
)


def test_extract_sector_mentions_alias():
    """别名命中：'光伏' → '电源设备'。"""
    vocab = dict(domain.SECTOR_ALIASES)
    hits = extract_sector_mentions("光伏板块获政策扶持", vocab)
    assert hits == {"电源设备"}


def test_extract_sector_mentions_multiple():
    """多赛道命中。"""
    vocab = dict(domain.SECTOR_ALIASES)
    vocab["半导体"] = "半导体"
    hits = extract_sector_mentions("光伏与半导体领涨", vocab)
    assert hits == {"电源设备", "半导体"}


def test_sector_theme_policy_persistence(monkeypatch):
    """政策词持续 2 天 → 政策持续主题（正面，潜在启动线索）。"""
    monkeypatch.setattr(repo, "get_available_sectors", lambda: ["半导体", "电源设备"])
    monkeypatch.setattr(repo, "get_recent_macro_news", lambda days: [
        ("2026-06-01", "[09:00] 半导体产业规划出台"),
        ("2026-06-02", "[09:00] 半导体补贴加码"),
    ])
    out = sector_theme_summary(days=7, min_days=2)
    assert "政策持续主题" in out
    assert "半导体" in out
    assert "媒体过热" not in out


def test_sector_theme_sentiment_overheat(monkeypatch):
    """情绪词持续 2 天 → 媒体过热提示（风险，仅作否决参考）。"""
    monkeypatch.setattr(repo, "get_available_sectors", lambda: ["半导体"])
    monkeypatch.setattr(repo, "get_recent_macro_news", lambda days: [
        ("2026-06-01", "[09:00] 半导体板块大涨"),
        ("2026-06-02", "[09:00] 半导体多股涨停"),
    ])
    out = sector_theme_summary(days=7, min_days=2)
    assert "媒体过热提示" in out
    assert "半导体" in out
    assert "政策持续主题" not in out


def test_sector_theme_single_day_filtered(monkeypatch):
    """单日闪现（< min_days）不入选，返回空串（调用方降级原要闻回顾）。"""
    monkeypatch.setattr(repo, "get_available_sectors", lambda: ["半导体"])
    monkeypatch.setattr(repo, "get_recent_macro_news", lambda days: [
        ("2026-06-01", "[09:00] 半导体补贴出台"),
    ])
    out = sector_theme_summary(days=7, min_days=2)
    assert out == ""


def test_sector_theme_policy_beats_sentiment(monkeypatch):
    """政策词与情绪词同现时政策优先（政策是根因、涨是结果）。"""
    monkeypatch.setattr(repo, "get_available_sectors", lambda: ["半导体"])
    monkeypatch.setattr(repo, "get_recent_macro_news", lambda days: [
        ("2026-06-01", "[09:00] 半导体补贴政策出台后板块大涨"),
        ("2026-06-02", "[09:00] 半导体规划落地"),
    ])
    out = sector_theme_summary(days=7, min_days=2)
    assert "政策持续主题" in out
    assert "媒体过热" not in out


def test_news_theme_summary_fallback_unchanged(monkeypatch):
    """原 news_theme_summary 保留（无持续主题时的降级路径）。"""
    monkeypatch.setattr(repo, "get_recent_macro_news", lambda days: [
        ("2026-06-01", "[09:00] 第一条标题：摘要\n[10:00] 第二条标题：摘要"),
    ])
    out = news_theme_summary(days=7)
    assert "第一条标题" in out

"""#4 洞察闭环因果验证测试。

覆盖：生效后同类案例胜率提升 → 有效；反降 → 疑似无效；样本不足 → 不出结论；
洞察文本不含赛道 → 跳过。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.repo as repo
from app.engine.insights import causal_check


def _case(d, sectors, outcome):
    return {"recommended_sectors": json.dumps(sectors), "outcome": outcome, "date": d}


def test_causal_check_effective(monkeypatch):
    """生效后胜率显著提升（33%→100%）→ 有效。"""
    monkeypatch.setattr(repo, "get_available_sectors", lambda: ["半导体"])
    monkeypatch.setattr(repo, "get_sector_insights_dated",
                        lambda limit: [(1, "半导体赛道追高后常回落", "2026-06-15")])
    cases = [
        _case("2026-06-01", ["半导体"], "负"),
        _case("2026-06-02", ["半导体"], "负"),
        _case("2026-06-03", ["半导体"], "胜"),
        _case("2026-06-16", ["半导体"], "胜"),
        _case("2026-06-17", ["半导体"], "胜"),
        _case("2026-06-18", ["半导体"], "胜"),
    ]
    monkeypatch.setattr(repo, "get_settled_cases_after", lambda ss: cases)
    fixes = causal_check()
    assert any("有效" in f for f in fixes)


def test_causal_check_ineffective(monkeypatch):
    """生效后胜率反降（100%→33%）→ 疑似无效。"""
    monkeypatch.setattr(repo, "get_available_sectors", lambda: ["半导体"])
    monkeypatch.setattr(repo, "get_sector_insights_dated",
                        lambda limit: [(1, "半导体赛道追高后常回落", "2026-06-15")])
    cases = [
        _case("2026-06-01", ["半导体"], "胜"),
        _case("2026-06-02", ["半导体"], "胜"),
        _case("2026-06-03", ["半导体"], "胜"),
        _case("2026-06-16", ["半导体"], "负"),
        _case("2026-06-17", ["半导体"], "负"),
        _case("2026-06-18", ["半导体"], "胜"),
    ]
    monkeypatch.setattr(repo, "get_settled_cases_after", lambda ss: cases)
    fixes = causal_check()
    assert any("疑似无效" in f for f in fixes)


def test_causal_check_insufficient_samples(monkeypatch):
    """任一组样本 <3 → 不出结论。"""
    monkeypatch.setattr(repo, "get_available_sectors", lambda: ["半导体"])
    monkeypatch.setattr(repo, "get_sector_insights_dated",
                        lambda limit: [(1, "半导体赛道追高后常回落", "2026-06-15")])
    cases = [
        _case("2026-06-01", ["半导体"], "胜"),
        _case("2026-06-16", ["半导体"], "负"),
    ]
    monkeypatch.setattr(repo, "get_settled_cases_after", lambda ss: cases)
    assert causal_check() == []


def test_causal_check_no_sector_match(monkeypatch):
    """洞察文本不含任何赛道 → 跳过。"""
    monkeypatch.setattr(repo, "get_available_sectors", lambda: ["半导体"])
    monkeypatch.setattr(repo, "get_sector_insights_dated",
                        lambda limit: [(1, "大盘高位时谨慎追涨", "2026-06-15")])
    cases = [_case("2026-06-01", ["半导体"], "胜")] * 6
    monkeypatch.setattr(repo, "get_settled_cases_after", lambda ss: cases)
    assert causal_check() == []

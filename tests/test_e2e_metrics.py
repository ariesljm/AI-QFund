"""#2 端到端 P&L 度量测试。

覆盖：按实际退出日期算净收益（扣赎回费）、对比 40 日理论收益的
timing_contribution（监控择时真实贡献）、异常样本跳过。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.repo as repo
from app.engine import quality


def test_e2e_exit_uses_return_rate(monkeypatch):
    """EXIT 案例：gross = return_rate，扣赎回费后为净收益。"""
    rows = [("F1", "2026-06-01", 1.0, "EXIT", "2026-06-20", 0.03)]
    monkeypatch.setattr(repo, "get_e2e_sample_rows", lambda a, b: rows)
    monkeypatch.setattr(repo.nav, "forward_return", lambda code, d: 0.05)

    m = quality.compute_e2e_metrics("2026-06-01", "2026-06-30")

    assert m["e2e_sample_count"] == 1
    # 持有 19 自然日 → 7-29 档 0.75%；e2e = 0.03 - 0.0075 = 0.0225
    assert abs(m["e2e_mean_ret"] - 0.0225) < 1e-6
    # timing = e2e - theo = 0.0225 - 0.05 = -0.0275（提前退出牺牲了理论收益）
    assert abs(m["timing_contribution"] - (-0.0275)) < 1e-6


def test_e2e_holding_uses_latest_nav(monkeypatch):
    """持有中：gross = latest/entry - 1，持有天数按今天算（不依赖具体费率档）。"""
    rows = [("F1", "2026-06-01", 1.0, "HOLD", None, None)]
    monkeypatch.setattr(repo, "get_e2e_sample_rows", lambda a, b: rows)
    monkeypatch.setattr(repo.nav, "forward_return", lambda code, d: 0.05)
    monkeypatch.setattr(repo.nav, "latest", lambda code: 1.06)

    m = quality.compute_e2e_metrics("2026-06-01", "2026-06-30")

    assert m["e2e_sample_count"] == 1
    assert m["e2e_mean_ret"] is not None


def test_e2e_timing_positive_when_stop_saves(monkeypatch):
    """止损保命：理论大亏、实际提前退出亏损更小 → timing 为正。"""
    rows = [("F1", "2026-06-01", 1.0, "EXIT", "2026-06-10", -0.02)]
    monkeypatch.setattr(repo, "get_e2e_sample_rows", lambda a, b: rows)
    monkeypatch.setattr(repo.nav, "forward_return", lambda code, d: -0.08)

    m = quality.compute_e2e_metrics("2026-06-01", "2026-06-30")

    # e2e = -0.02 - 0.75%(9天) = -0.0275；theo = -0.08；timing = +0.0525
    assert m["timing_contribution"] > 0


def test_e2e_skips_missing_entry_nav(monkeypatch):
    """无入场净值 → 跳过该样本，timing 为 None。"""
    rows = [("F1", "2026-06-01", None, "HOLD", None, None)]
    monkeypatch.setattr(repo, "get_e2e_sample_rows", lambda a, b: rows)

    m = quality.compute_e2e_metrics("2026-06-01", "2026-06-30")

    assert m["e2e_sample_count"] == 0
    assert m["timing_contribution"] is None


def test_e2e_empty_sample(monkeypatch):
    """空样本：profit_rate/mean/timing 全为 None。"""
    monkeypatch.setattr(repo, "get_e2e_sample_rows", lambda a, b: [])

    m = quality.compute_e2e_metrics("2026-06-01", "2026-06-30")

    assert m["e2e_sample_count"] == 0
    assert m["e2e_profit_rate"] is None
    assert m["timing_contribution"] is None

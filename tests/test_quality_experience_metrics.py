"""ticket 12 质量体验指标测试：推荐至退出最大回撤 + 平均持仓周期。

覆盖 `_max_drawdown` 纯函数（回撤相对峰值、单调上升零回撤、路径不足）
与 `compute_e2e_metrics` 的两个新聚合指标。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.repo as repo
from app import domain
from app.engine import quality


class TestMaxDrawdown:
    def test_drawdown_relative_to_peak(self, monkeypatch):
        monkeypatch.setattr(
            repo.nav, "series",
            lambda code, since=None, until=None, limit=None:
            [("2026-06-01", 1.0), ("2026-06-02", 1.2),
             ("2026-06-03", 0.9), ("2026-06-04", 1.05)],
        )
        # 峰值 1.2 → 谷 0.9 → dd = 0.9/1.2 - 1 = -0.25
        mdd = quality._max_drawdown("F1", "2026-06-01", "2026-06-04")
        assert abs(mdd - (-0.25)) < 1e-9

    def test_monotonic_rise_zero_drawdown(self, monkeypatch):
        monkeypatch.setattr(
            repo.nav, "series",
            lambda code, since=None, until=None, limit=None:
            [("2026-06-01", 1.0), ("2026-06-02", 1.1)],
        )
        assert quality._max_drawdown("F1", "2026-06-01", "2026-06-02") == 0.0

    def test_insufficient_path_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            repo.nav, "series",
            lambda code, since=None, until=None, limit=None: [("2026-06-01", 1.0)],
        )
        assert quality._max_drawdown("F1", "2026-06-01", "2026-06-02") is None


class TestComputeE2eExperienceMetrics:
    def _patch(self, monkeypatch, rows, path):
        monkeypatch.setattr(repo, "get_e2e_sample_rows", lambda a, b: rows)
        monkeypatch.setattr(repo.nav, "forward_return", lambda code, d: 0.03)
        monkeypatch.setattr(
            repo.nav, "series",
            lambda code, since=None, until=None, limit=None: path,
        )

    def test_returns_mean_hold_days_and_drawdown(self, monkeypatch):
        rows = [
            # (code, reco_date, entry_nav, status, exit_date, return_rate)
            ("F1", "2026-06-01", 1.0, domain.SIGNAL_EXIT, "2026-07-01", 0.05),
            ("F2", "2026-06-01", 1.0, domain.SIGNAL_EXIT, "2026-06-15", 0.02),
        ]
        # 路径含 5% 回撤（峰值 1.0 → 谷 0.95）
        self._patch(monkeypatch, rows,
                    [("2026-06-01", 1.0), ("2026-06-05", 0.95), ("2026-06-10", 1.02)])

        m = quality.compute_e2e_metrics("2026-06-01", "2026-06-30")

        assert m["e2e_mean_hold_days"] is not None
        assert m["e2e_mean_hold_days"] > 0
        assert m["e2e_mean_max_drawdown"] is not None
        assert m["e2e_mean_max_drawdown"] < 0  # 路径出现回撤
        # points 每条带 hold_days + max_drawdown（可回溯单条体验）
        assert m["e2e_points"][0]["hold_days"] > 0
        assert m["e2e_points"][0]["max_drawdown"] is not None

    def test_no_path_yields_none_drawdown_but_keeps_hold_days(self, monkeypatch):
        rows = [("F1", "2026-06-01", 1.0, domain.SIGNAL_EXIT, "2026-07-01", 0.05)]
        # 路径仅 1 点 → 回撤 None，但持仓周期仍统计
        self._patch(monkeypatch, rows, [("2026-06-01", 1.0)])

        m = quality.compute_e2e_metrics("2026-06-01", "2026-06-30")

        assert m["e2e_mean_max_drawdown"] is None
        assert m["e2e_mean_hold_days"] == 30  # 2026-06-01 → 2026-07-01 共 30 自然日

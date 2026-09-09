"""Ticket 04：量化定池信号重排测试（直接测 build_sector_pool）。

新信号（40 日窗口证据）：
- 近 5 日资金流累计为负 → 降权（不再与资金流入赛道同权）
- 20/60 日动量高位 → 温和降权而非整池剔除（60 日视野反转显著减弱）
- 极端高波动（vol 截面 P90+）→ 剔除
- 存量门槛保留：5 日动量 ≤0 仍剔除
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.engine import sector_pool as sp


def _mom(sector, m5, m20, m60):
    return {"mom_5d": m5, "mom_20d": m20, "mom_60d": m60, "n": 5}


def _base(monkeypatch, sectors, medians, trend, regime="BEAR"):
    monkeypatch.setattr(sp.repo, "get_available_sectors", lambda: list(sectors))
    monkeypatch.setattr(sp.repo, "get_latest_feature_date_before", lambda d: "2026-09-02")
    monkeypatch.setattr(sp.repo, "get_market_regime", lambda: regime)
    monkeypatch.setattr(sp.repo, "get_sector_momentum_medians",
                        lambda s, d: medians.get(s))
    monkeypatch.setattr(sp.repo, "get_sector_trend_features", lambda d: trend)


class TestFlowTrendSignal:
    def test_flow_negative_downweighted(self, monkeypatch):
        """同动量下，资金流出赛道 score 低于资金流入赛道，且带降权标记。"""
        sectors = ["A", "B"]
        medians = {s: _mom(s, 3.0, 5.0, 8.0) for s in sectors}
        trend = {"A": {"mom_5d": 3.0, "mom_20d": 5.0, "vol_20d": 0.2,
                       "flow_5d": -500.0, "flow_days": 5, "label": "资金流出"},
                 "B": {"mom_5d": 3.0, "mom_20d": 5.0, "vol_20d": 0.2,
                       "flow_5d": 800.0, "flow_days": 5, "label": "趋势健康"}}
        _base(monkeypatch, sectors, medians, trend)

        pool = sp.build_sector_pool("2026-09-03")
        by_name = {c.sector: c for c in pool.candidates}
        assert "资金流出" in by_name["A"].flags
        assert by_name["A"].score < by_name["B"].score

    def test_high_momentum_downweight_not_removed(self, monkeypatch):
        """60 日动量截面高位赛道保留（不再剔除），但带高位降权标记。"""
        sectors = ["A", "B", "C", "D"]
        medians = {s: _mom(s, 2.0, 3.0, m60) for s, m60 in
                   zip(sectors, [30.0, 20.0, 15.0, 10.0], strict=True)}
        trend = {s: {"mom_5d": 2.0, "mom_20d": 3.0, "vol_20d": 0.2,
                     "flow_5d": 100.0, "flow_days": 5, "label": "趋势健康"}
                 for s in sectors}
        _base(monkeypatch, sectors, medians, trend)

        pool = sp.build_sector_pool("2026-09-03")
        names = {c.sector for c in pool.candidates}
        assert "A" in names  # 高位最强赛道保留（原实现会剔除 → 回归点）
        a = next(c for c in pool.candidates if c.sector == "A")
        assert "中长高位降权" in a.flags

    def test_extreme_vol_removed(self, monkeypatch):
        """极端高波动（vol 截面 P90+）赛道剔除。"""
        sectors = ["A", "B", "C", "D"]
        medians = {s: _mom(s, 2.0, 3.0, 10.0) for s in sectors}
        trend = {
            "A": {"mom_5d": 2.0, "mom_20d": 3.0, "vol_20d": 0.9, "flow_5d": 100.0,
                  "flow_days": 5, "label": "趋势健康"},
            "B": {"mom_5d": 2.0, "mom_20d": 3.0, "vol_20d": 0.1, "flow_5d": 100.0,
                  "flow_days": 5, "label": "趋势健康"},
            "C": {"mom_5d": 2.0, "mom_20d": 3.0, "vol_20d": 0.2, "flow_5d": 100.0,
                  "flow_days": 5, "label": "趋势健康"},
            "D": {"mom_5d": 2.0, "mom_20d": 3.0, "vol_20d": 0.3, "flow_5d": 100.0,
                  "flow_days": 5, "label": "趋势健康"},
        }
        _base(monkeypatch, sectors, medians, trend)

        pool = sp.build_sector_pool("2026-09-03")
        assert "A" not in {c.sector for c in pool.candidates}
        assert any(e["sector"] == "A" for e in pool.excluded)


class TestLegacyGatesPreserved:
    def test_momentum_lte_zero_still_removed(self, monkeypatch):
        """存量门槛：5 日动量 ≤0 仍剔除。"""
        sectors = ["A", "B"]
        medians = {"A": _mom("A", -1.0, 3.0, 8.0),
                   "B": _mom("B", 3.0, 5.0, 8.0)}
        trend = {s: {"mom_5d": m, "mom_20d": 3.0, "vol_20d": 0.2,
                     "flow_5d": 100.0, "flow_days": 5, "label": "趋势健康"}
                 for s, m in zip(sectors, [-1.0, 3.0], strict=True)}
        _base(monkeypatch, sectors, medians, trend)

        pool = sp.build_sector_pool("2026-09-03")
        assert all(c.sector != "A" for c in pool.candidates)

    def test_trend_label_attached(self, monkeypatch):
        """候选池每赛道携带趋势质量标签（供 LLM 素材消费）。"""
        sectors = ["A"]
        medians = {"A": _mom("A", 3.0, 5.0, 8.0)}
        trend = {"A": {"mom_5d": 3.0, "mom_20d": 5.0, "vol_20d": 0.2,
                       "flow_5d": 200.0, "flow_days": 5, "label": "趋势健康"}}
        _base(monkeypatch, sectors, medians, trend)

        pool = sp.build_sector_pool("2026-09-03")
        assert pool.candidates[0].trend_label == "趋势健康"

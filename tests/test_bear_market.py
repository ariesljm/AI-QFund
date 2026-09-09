"""T06（reco-hardening）：熊市盈利机制验收测试（用户决策 D1：熊市也要找到赚钱的基金）。

研究证据（data/bear_market_research.json，24089 熊市样本，20 日口径）：
- 5 日动量高分位胜率 50.2% vs 低分位 18.0%（证实现有门槛方向正确）
- 低波动高分位 29.8% vs 低分位 60.1%（熊市防守有效）→ 熊市高波动降权落地
本测试固化：熊市 regime 分支的降权行为（BULL 不受影响）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.engine.sector_pool import BEAR_HIGH_VOL_DOWNWEIGHT, BEAR_HIGH_VOL_PCT, build_sector_pool


class TestBearHighVolDownweight:
    def _ctx(self, monkeypatch, regime, vol_by_sector, mom_5d_by_sector=None):
        """构造定池环境：regime + 每赛道 vol（含高波动赛道）。"""
        import app.engine.sector_pool as sp
        from app.repo import base as repo

        sectors = list(vol_by_sector)
        mom5 = mom_5d_by_sector or {s: 3.0 for s in sectors}

        def fake_available():
            return sectors

        def fake_medians(sector, date):
            return {"mom_5d": mom5[sector], "mom_20d": 2.0, "mom_60d": 5.0, "n": 5}

        def fake_trend(date):
            out = {}
            for s in sectors:
                out[s] = {"mom_5d": mom5[s], "mom_20d": 2.0, "mom_60d": 5.0,
                          "vol_20d": vol_by_sector[s], "flow_5d": 100.0, "label": ""}
            return out

        def fake_regime():
            return regime

        def fake_feature_date(date):
            return date

        monkeypatch.setattr(repo, "get_available_sectors", fake_available)
        monkeypatch.setattr(repo, "get_sector_momentum_medians", fake_medians)
        monkeypatch.setattr(repo, "get_sector_trend_features", fake_trend)
        monkeypatch.setattr(repo, "get_market_regime", fake_regime)
        monkeypatch.setattr(repo, "get_latest_feature_date_before", fake_feature_date)
        return sp

    def test_bear_high_vol_downweighted(self, monkeypatch):
        """熊市：vol 截面 P70+（未到 P90 剔除线）的赛道被降权（低波动防守）。"""
        self._ctx(monkeypatch, "BEAR",
                  {"低波赛道": 0.5, "中波赛道": 1.0, "较高波赛道": 2.0, "高波赛道": 2.5, "很高波赛道": 2.9, "超高波赛道": 3.0})
        pool = build_sector_pool("2026-09-03")
        flags = {c.sector: c.flags for c in pool.candidates}
        # 中高波（P70~P90 之间）应带熊市降权标签
        assert any("熊市高波动降权" in f for f in flags.values())

    def test_bull_ignores_bear_downweight(self, monkeypatch):
        """牛市：不触发熊市降权分支。"""
        self._ctx(monkeypatch, "BULL",
                  {"低波赛道": 0.5, "中波赛道": 1.0, "较高波赛道": 2.0, "高波赛道": 2.5, "很高波赛道": 2.9, "超高波赛道": 3.0})
        pool = build_sector_pool("2026-09-03")
        for c in pool.candidates:
            assert "熊市高波动降权" not in c.flags

    def test_low_vol_keeps_score(self, monkeypatch):
        """熊市：低波动赛道不受降权影响（分数保留）。"""
        self._ctx(monkeypatch, "BEAR",
                  {"低波赛道": 0.5, "中波赛道": 1.0, "较高波赛道": 2.0, "高波赛道": 2.5, "很高波赛道": 2.9, "超高波赛道": 3.0})
        pool = build_sector_pool("2026-09-03")
        low = next(c for c in pool.candidates if c.sector == "低波赛道")
        mid_high = next(c for c in pool.candidates if c.sector == "很高波赛道")
        # 中高波被降权后分数应低于低波动（同基准分）
        assert low.score > mid_high.score


_ = BEAR_HIGH_VOL_DOWNWEIGHT  # noqa: F401
_ = BEAR_HIGH_VOL_PCT  # noqa: F401

"""票 07：个股日线复权收益纯函数 + 存取 roundtrip。

复权口径单一来源在 domain.adjusted_returns：前复权收盘价 → cur/prev−1；
停牌日（缺数据）跳过，复牌日收益含停牌期跳空（不把停牌当 0）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import app.database as db_mod
from app.data.store import save_stock_daily
from app.domain import adjusted_returns
from app.repo.base import get_stock_daily


class TestAdjustedReturns:
    def test_normal(self):
        closes = {"2024-01-01": 100.0, "2024-01-02": 110.0, "2024-01-03": 99.0}
        got = adjusted_returns(closes)
        assert set(got.keys()) == {"2024-01-02", "2024-01-03"}
        assert got["2024-01-02"] == pytest.approx(0.1)
        assert got["2024-01-03"] == pytest.approx(-0.1)

    def test_suspension_gap_included(self):
        """停牌日缺失：复牌日收益 = 复牌价/停牌前价 − 1（含跳空，非 0）。"""
        closes = {"2024-01-01": 100.0, "2024-01-05": 121.0}   # 中间停牌 3 天
        got = adjusted_returns(closes)
        assert abs(got["2024-01-05"] - 0.21) < 1e-12

    def test_nonpositive_prev_skipped(self):
        """prev <= 0（脏数据）跳过，不产出 inf/异常收益。"""
        assert adjusted_returns({"2024-01-01": 0.0, "2024-01-02": 5.0}) == {}

    def test_single_point_empty(self):
        assert adjusted_returns({"2024-01-01": 100.0}) == {}


class TestStockDailyRepo:
    def _seed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "sd.db")
        save_stock_daily("600519", {f"2024-{i:03d}": 100.0 + i for i in range(10)})

    def test_roundtrip(self, monkeypatch, tmp_path):
        self._seed(monkeypatch, tmp_path)
        got = get_stock_daily("600519")
        assert len(got) == 10
        assert got["2024-000"] == 100.0 and got["2024-009"] == 109.0

    def test_days_limit(self, monkeypatch, tmp_path):
        self._seed(monkeypatch, tmp_path)
        got = get_stock_daily("600519", days=3)
        assert list(got.keys()) == ["2024-007", "2024-008", "2024-009"]

    def test_missing(self, monkeypatch, tmp_path):
        self._seed(monkeypatch, tmp_path)
        assert get_stock_daily("NOPE") == {}

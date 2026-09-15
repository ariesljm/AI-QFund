"""票 16：虚拟组合脱轨检测纯函数。

归一化 / 组合收益 / 相关性与 R² / 脱轨判定——全部纯函数直测，含零方差退化、
停牌日对齐、窗口不足的处置口径（对照 np.corrcoef 的静默 NaN 污染）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.engine.drift import (
    DRIFT_CORR_THRESHOLD,
    DRIFT_R2_THRESHOLD,
    ROLLING_WINDOW,
    corr_r2,
    drift_check,
    normalize_weights,
    proxy_returns,
)


class TestNormalizeWeights:
    def test_normalize(self):
        h = [{"stock_code": "A", "weight": 3}, {"stock_code": "B", "weight": 1}]
        got = normalize_weights(h)
        assert got == {"A": 0.75, "B": 0.25}

    def test_empty(self):
        assert normalize_weights([]) == {}
        assert normalize_weights([{"stock_code": "A", "weight": 0}]) == {}


class TestProxyReturns:
    def test_two_stocks(self):
        # A 权重 0.75 收益 +10%，B 权重 0.25 收益 0 → 组合 7.5%
        w = {"A": 0.75, "B": 0.25}
        dailies = {"A": {"d1": 100, "d2": 110}, "B": {"d1": 100, "d2": 100}}
        got = proxy_returns(w, dailies)
        assert got["d2"] == pytest.approx(0.075)

    def test_suspension_reweights_within_available(self):
        """B 停牌日（d2 无数据）→ 权重在 A 上重新归一化为 1，组合收益 = A 的收益。"""
        w = {"A": 0.75, "B": 0.25}
        dailies = {"A": {"d1": 100, "d2": 110}, "B": {"d1": 100}}
        got = proxy_returns(w, dailies)
        assert got["d2"] == pytest.approx(0.10)

    def test_no_data_day_skipped(self):
        w = {"A": 1.0}
        dailies = {"A": {"d1": 100, "d3": 110}}   # d2 全市场无数据
        got = proxy_returns(w, dailies)
        assert "d2" not in got
        assert got["d3"] == pytest.approx(0.10)


class TestCorrR2:
    def test_perfect_positive(self):
        c, r2 = corr_r2([1, 2, 3], [2, 4, 6])
        assert c == pytest.approx(1.0) and r2 == pytest.approx(1.0)

    def test_perfect_negative(self):
        c, r2 = corr_r2([1, 2, 3], [3, 2, 1])
        assert c == pytest.approx(-1.0) and r2 == pytest.approx(1.0)

    def test_zero_variance_is_drifted_not_error(self):
        """零方差 → (0.0, 0.0)：不抛异常、无 NaN（对照 np.corrcoef）。"""
        assert corr_r2([1.0, 1.0, 1.0], [1, 2, 3]) == (0.0, 0.0)
        assert corr_r2([1, 2, 3], [5.0, 5.0]) == (0.0, 0.0)   # 长度不等也退化

    def test_nan_explicitly_degrades(self):
        assert corr_r2([1.0, float("nan"), 3.0], [1, 2, 3]) == (0.0, 0.0)

    def test_too_short(self):
        assert corr_r2([1.0], [2.0]) == (0.0, 0.0)


class TestDriftCheck:
    def _series(self, n=ROLLING_WINDOW + 5, spread=0.1, noise=0.0):
        """构造 fund 与带噪声的 proxy 序列（重叠日期）。"""
        days = [f"d{i}" for i in range(n)]
        fund = {d: spread * i for i, d in enumerate(days)}
        proxy = {d: spread * i + noise * i for i, d in enumerate(days)}
        return fund, proxy

    def test_aligned_not_drifted(self):
        fund, proxy = self._series(noise=0.0)   # 完全对齐
        got = drift_check(fund, proxy)
        assert got is not None and got["is_drifted"] is False

    def test_uncorrelated_is_drifted(self):
        fund, proxy = self._series(noise=0.0)
        # 打乱 proxy 相关性接近 0（纯函数，构造反序即可得到负/弱相关）
        proxy = {d: -v for d, v in proxy.items()}   # 完全负相关 → r2=1 不算脱轨
        got = drift_check(fund, proxy)
        assert got is not None and got["is_drifted"] is False      # 负相关 r2=1
        # 常数序列（零方差）→ 脱轨标记（r2=0 < 0.25）
        proxy = {d: 5.0 for d in fund}
        got = drift_check(fund, proxy)
        assert got is not None and got["is_drifted"] is True

    def test_window_insufficient_returns_none(self):
        fund = {f"d{i}": float(i) for i in range(5)}    # 5 天 < 15
        proxy = {d: v for d, v in fund.items()}
        assert drift_check(fund, proxy) is None

    def test_uses_latest_window(self):
        fund, proxy = self._series()
        got = drift_check(fund, proxy, window=10)
        assert got is not None and got["samples"] == 10

    def test_threshold_constants_sane(self):
        assert DRIFT_CORR_THRESHOLD == 0.40 and DRIFT_R2_THRESHOLD == 0.25

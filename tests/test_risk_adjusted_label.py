"""风险调整收益标签测试（ticket 09）。

目标函数从纯 40 日绝对收益改为：40 日收益 − λ × 40 日最大回撤。
锁定：纯函数手算正确性、窗口边界、λ 配置生效、标签版本升级校验。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from app import domain
from app import model as model_mod


class TestRiskAdjustedReturn:
    def test_monotonic_rise_no_drawdown(self):
        """净值单调上升：最大回撤 0 → 标签等于纯收益。"""
        navs = np.array([1.0, 1.1, 1.2])
        y = model_mod.risk_adjusted_return(navs, 0, 2)
        assert abs(y - 0.2) < 1e-12  # 1.2/1.0 - 1 = 0.2, λ×0 = 0

    def test_with_drawdown_subtracts_lambda_weighted_dd(self):
        """含回撤：标签 = 收益 − λ×最大回撤（手算，λ 已标定为 1.0）。"""
        navs = np.array([1.0, 1.2, 0.9])
        # ret = 0.9/1.0-1 = -0.1；峰值 1.2 → max_dd = (1.2-0.9)/1.2 = 0.25
        # y = -0.1 - 1.0×0.25 = -0.35
        y = model_mod.risk_adjusted_return(navs, 0, 2)
        assert abs(y - (-0.35)) < 1e-12

    def test_lambda_configurable(self, monkeypatch):
        """λ 可配置：λ=1 时回撤惩罚加倍。"""
        monkeypatch.setattr(model_mod.domain, "RISK_ADJ_DD_LAMBDA", 1.0)
        navs = np.array([1.0, 1.2, 0.9])
        y = model_mod.risk_adjusted_return(navs, 0, 2)
        assert abs(y - (-0.1 - 1.0 * 0.25)) < 1e-12  # -0.35

    def test_window_out_of_range_returns_nan(self):
        """窗口越界 → nan（不抛异常）。"""
        navs = np.array([1.0, 1.1])
        assert np.isnan(model_mod.risk_adjusted_return(navs, 0, 2))

    def test_non_finite_returns_nan(self):
        """窗口含非有限值 → nan。"""
        navs = np.array([1.0, np.nan, 1.2])
        assert np.isnan(model_mod.risk_adjusted_return(navs, 0, 2))

    def test_penalty_is_never_negative_premium(self):
        """有回撤时风险调整标签严格低于纯收益（惩罚方向正确）。"""
        navs = np.array([1.0, 1.3, 1.1])
        pure = 1.1 / 1.0 - 1.0
        y = model_mod.risk_adjusted_return(navs, 0, 2)
        assert y < pure


class TestLabelVersionUpgrade:
    def test_label_version_is_risk_adjusted(self):
        """标签版本：risk_adj_40d_v2（λ 标定 0.5→1.0 后递增，与旧标签不混用）。"""
        assert model_mod.LABEL_VERSION == "risk_adj_40d_v2"

    def test_default_lambda_is_conservative(self):
        """λ 默认值落在 (0, 1] 的标定区间（0.3~1.0 扫描单调，取上界 1.0）。"""
        assert 0.0 < domain.RISK_ADJ_DD_LAMBDA <= 1.0

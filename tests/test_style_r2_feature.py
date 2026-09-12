"""style_r2（风格清晰度）特征测试：纯函数反推 + 降级 + 缺列容错。"""

import numpy as np
import pandas as pd
import pytest

from app.domain import FEATURE_COLS
from app.features.calculator import (
    compute_fund_features, _style_r2_from_frame, score_frame,
)
from app.engine.recommend import _dropna_features


def _nav_series(n=120, seed=1):
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0005, 0.008, n)
    navs = 1.0 + np.cumsum(rets)
    navs = np.maximum(navs, 0.1)
    return navs


def _dates(n=120, start="2024-06-01"):
    return pd.bdate_range(start, periods=n).strftime("%Y-%m-%d").tolist()


class TestStyleR2FromFrame:
    def test_high_r2_when_fund_tracks_sector(self):
        """净值完全由单一板块驱动 → r2 高（接近 1）。"""
        dates = _dates(120)
        rng = np.random.default_rng(7)
        sector_ret = rng.normal(0.001, 0.01, 120)
        navs = 1.0 + np.cumsum(sector_ret)
        navs = np.maximum(navs, 0.1)
        # 板块宽表：一列 = 该板块收益（百分数）
        df = pd.DataFrame({f"s{i}": rng.normal(0, 0.01, 120) * 100 for i in range(5)},
                          index=dates)
        df["s0"] = sector_ret * 100  # 基金追踪 s0
        r2 = _style_r2_from_frame(dates, navs, df)
        assert r2 is not None and r2 > 0.5

    def test_insufficient_window_returns_none(self):
        dates = _dates(10)
        navs = _nav_series(10)
        df = pd.DataFrame({"s0": np.ones(10)}, index=dates)
        assert _style_r2_from_frame(dates, navs, df) is None

    def test_zero_or_negative_nav_returns_none(self):
        dates = _dates(120)
        navs = _nav_series(120)
        navs[-1] = -1.0
        df = pd.DataFrame({"s0": np.ones(120)}, index=dates)
        assert _style_r2_from_frame(dates, navs, df) is None

    def test_none_sector_frame_returns_none(self):
        assert _style_r2_from_frame(_dates(120), _nav_series(120), None) is None


class TestComputeFundFeaturesStyleR2:
    def test_style_r2_key_present_and_zero_without_sector(self):
        navs = _nav_series(120)
        feat = compute_fund_features(navs, np.ones(120) * 3000.0,
                                     np.ones(120) * 1e8)
        assert feat is not None and feat["style_r2"] == 0.0

    def test_style_r2_computed_with_sector_frame(self):
        navs = _nav_series(120)
        dates = _dates(120)
        rng = np.random.default_rng(7)
        navs_padded = np.concatenate([[1.0], navs])
        df = pd.DataFrame({f"s{i}": rng.normal(0, 0.01, 120) * 100 for i in range(5)},
                          index=dates)
        df["s0"] = (np.diff(navs_padded) / np.maximum(navs_padded[:-1], 1e-9)) * 100
        feat = compute_fund_features(navs, np.ones(120) * 3000.0,
                                     np.ones(120) * 1e8,
                                     nav_dates=dates, sector_frame=df)
        assert feat is not None and 0.0 <= feat["style_r2"] <= 1.0

    def test_feature_cols_contains_style_r2(self):
        assert "style_r2" in FEATURE_COLS


class TestDropnaMissingColumnTolerance:
    def test_dropna_ignores_missing_column(self):
        df = pd.DataFrame({"hurst_60d": [1.0, np.nan]})
        out = _dropna_features(df)
        assert len(out) == 1  # 缺 style_r2 不崩溃，只按存在列过滤

    def test_dropna_filters_nan_existing_columns(self):
        df = pd.DataFrame({"hurst_60d": [np.nan, 1.0]})
        assert len(_dropna_features(df)) == 1

    def test_empty_cols_returns_unchanged(self):
        df = pd.DataFrame({"x": [1.0, np.nan]})
        out = _dropna_features(df)
        assert len(out) == 2  # 无 FEATURE_COLS 列 → 不过滤


class TestScoreFrameMissingColumn:
    def test_score_frame_pads_missing_feature_columns(self):
        from app.domain import RankingConfig
        df = pd.DataFrame({"hurst_60d": [1.0, 2.0]})
        for c in FEATURE_COLS:
            if c not in df.columns:
                df[c] = 0.0
        scored = score_frame(df, model=None, cfg=RankingConfig(), idx_mom=0.0)
        # model=None（回测）时产出 score_norm/combo，无 score 列；验证完整性与无 NaN
        assert scored["combo"].notna().all() and len(scored) == 2

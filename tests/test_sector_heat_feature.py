"""ticket 08 第 3 项测试：赛道热度特征真进 LightGBM 输入口径。

原 08 设计用 net_flow 算拥挤度，但东财资金流历史接口不可达（仅 ~17 日实时快照），
无法回溯到训练决策日。改用**板块日涨幅**（sector_daily_snapshot，2024-03 起 600+ 日）
构造可历史化的市场级赛道热度，与 MARKET_COLS 同口径注入 LightGBM。
"""

import numpy as np
import pandas as pd

from app.domain import MARKET_COLS
from app.features.calculator import (
    calc_sector_heat,
    market_state_features,
    sector_heat_from_frame,
)


class TestCalcSectorHeat:
    def test_insufficient_samples_degrades_to_zero(self):
        """有效样本 < 5 个赛道 → 0.0（不窃动模型）。"""
        assert calc_sector_heat([1.0, 2.0]) == 0.0
        assert calc_sector_heat([]) == 0.0

    def test_extreme_outlier_gives_positive_heat(self):
        """单赛道极端上涨（资金集中）→ 分化度为正。"""
        assert calc_sector_heat([0.0, 0.5, 1.0, 1.5, 20.0]) > 0

    def test_uniform_returns_gives_zero_heat(self):
        """所有赛道涨幅一致（无分化）→ P90 − 中位数 = 0。"""
        assert calc_sector_heat([1.0] * 8) == 0.0

    def test_none_and_nan_filtered(self):
        """None/NaN 先过滤，不足 5 个有效值则降级。"""
        assert calc_sector_heat([None, 1.0, 2.0, float("nan")]) == 0.0
        # 补足 5 个有效值后正常计算
        assert calc_sector_heat([0.0, 0.5, 1.0, 1.5, 20.0, None]) > 0


class TestSectorHeatFromFrame:
    def _frame(self):
        return pd.DataFrame(
            {"半导体": [1.0, 2.0, 1.0, 3.0, 1.0],
             "白酒": [0.5, 0.5, 0.5, 0.5, 0.5],
             "煤炭": [0.2, 0.2, 0.2, 0.2, 0.2],
             "医药": [0.1, 0.1, 0.1, 0.1, 0.1],
             "军工": [0.3, 0.3, 0.3, 0.3, 0.3]},
            index=["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05"],
        )

    def test_no_lookahead_before_window(self):
        """as_of 早于窗口长度 → 降级 0（不能取到未来 5 日）。"""
        assert sector_heat_from_frame(self._frame(), as_of="2026-01-02") == 0.0

    def test_slices_strictly_at_as_of(self):
        """切片严格不含 as_of 之后的数据（无前视）。"""
        f = self._frame()
        full = sector_heat_from_frame(f, as_of="2026-01-05")
        partial = sector_heat_from_frame(f, as_of="2026-01-03")
        assert full != partial          # 2026-01-04/05 的半导体暴涨改变了热度

    def test_empty_frame_degrades(self):
        assert sector_heat_from_frame(None) == 0.0
        assert sector_heat_from_frame(pd.DataFrame()) == 0.0


class TestMarketStateIncludesHeat:
    def test_sector_heat_is_in_market_cols(self):
        """赛道热度已进 MARKET_COLS → 自动进 LightGBM 输入（同口径注入）。"""
        assert "sector_heat_5d" in MARKET_COLS

    def test_market_state_features_carries_heat(self):
        closes = np.arange(1.0, 62.0)
        vols = np.ones(61)
        mkt = market_state_features(closes, vols, sector_heat=3.5)
        assert mkt["sector_heat_5d"] == 3.5
        assert set(MARKET_COLS) <= set(mkt.keys())

    def test_default_heat_is_zero(self):
        """缺板块数据时不传 → 0.0（优雅降级，不窃动）。"""
        mkt = market_state_features(np.arange(1.0, 62.0), np.ones(61))
        assert mkt["sector_heat_5d"] == 0.0

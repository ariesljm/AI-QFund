"""ticket 08 残留测试：市场风险偏好特征口径。

市场风险偏好：MARKET_COLS 已含指数动量/波动率/偏离（ticket 08 第 1 项口径锁定）。
拥挤度惩罚与 combo 打分已在 2.0 多因子排序改造中下线。
"""

from app.domain import MARKET_COLS


class TestMarketRiskPreference:
    def test_market_cols_cover_momentum_and_volatility(self):
        """市场风险偏好（动量/波动率/偏离）已作为全局特征进模型输入口径。"""
        assert "idx_mom_20d" in MARKET_COLS
        assert "idx_vol_20d" in MARKET_COLS
        assert "bias_60d" in MARKET_COLS

"""ticket 08 测试：赛道拥挤度惩罚 + 市场风险偏好特征口径。

拥挤度：短期净流入极端且斜率过陡 → 负惩罚；样本不足/零方差/流出 → 不惩罚。
市场风险偏好：MARKET_COLS 已含指数动量/波动率/偏离（ticket 08 第 1 项口径锁定）。
"""

from app.domain import MARKET_COLS
from app.features.calculator import combo_score, sector_crowding_penalty

_W = {"model": 0.5, "rs": 0.2, "cal": 0.2, "hurst": 0.1}


class TestSectorCrowdingPenalty:
    def test_extreme_and_steep_gives_negative_penalty(self):
        """历史平稳 → 末段急升，最新值极端（z>2）且斜率陡 → 负惩罚。"""
        flows = [100.0, 105.0, 98.0, 102.0, 400.0, 900.0]
        assert sector_crowding_penalty(flows) < 0

    def test_penalty_bounded_within_unit(self):
        """惩罚归一到 [-1, 0]。"""
        flows = [100.0, 102.0, 98.0, 101.0, 5000.0, 100000.0]
        p = sector_crowding_penalty(flows)
        assert -1.0 <= p <= 0.0

    def test_zero_variance_no_penalty(self):
        assert sector_crowding_penalty([100.0] * 6) == 0.0

    def test_not_extreme_no_penalty(self):
        """温和流入（无极端值）→ 不惩罚。"""
        flows = [100.0, 110.0, 105.0, 108.0, 112.0, 115.0]
        assert sector_crowding_penalty(flows) == 0.0

    def test_insufficient_samples_degrades(self):
        assert sector_crowding_penalty([100.0, 200.0]) == 0.0
        assert sector_crowding_penalty([]) == 0.0

    def test_outflow_no_penalty(self):
        """净流出加剧不是拥挤（负值段均 → 不惩罚）。"""
        assert sector_crowding_penalty(
            [-100.0, -200.0, -300.0, -900.0, -1500.0, -2000.0]) == 0.0

    def test_none_values_filtered(self):
        assert sector_crowding_penalty([None, 100.0, None]) == 0.0


class TestComboCrowdingWired:
    def test_crowding_lowers_combo(self):
        """拥挤惩罚项接入 combo：同分下拥挤标的 combo 更低。"""
        base = combo_score(0.5, 0.0, 1.0, 0.5, _W)
        penalized = combo_score(0.5, 0.0, 1.0, 0.5, _W, sector_crowding=-1.0)
        assert penalized < base

    def test_default_no_crowding_is_neutral(self):
        assert combo_score(0.5, 0.0, 1.0, 0.5, _W) == combo_score(
            0.5, 0.0, 1.0, 0.5, _W, sector_crowding=0.0)


class TestMarketRiskPreference:
    def test_market_cols_cover_momentum_and_volatility(self):
        """市场风险偏好（动量/波动率/偏离）已作为全局特征进模型输入口径。"""
        assert "idx_mom_20d" in MARKET_COLS
        assert "idx_vol_20d" in MARKET_COLS
        assert "bias_60d" in MARKET_COLS

"""Ticket 06：组合分（combo）相对强弱基准调整测试。

多窗口验证（2026-09）：40 日持有视野下 5 日动量延续性最强（hot +3.08%/胜率 63%），
20 日动量在该窗口已无区分度（spread +0.18）。因此组合分中 rel_strength 基准
由 20 日动量改为 5 日动量。
契约：
- 其他相同、mom_5d 高 → combo 高（5 日动量正贡献）
- 其他相同、momentum_20d 差异 → combo 不再随之变化（不再作为相对强弱基准）
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from app import domain
from app.features import calculator


def _row(**overrides):
    row = {c: 1.0 for c in domain.FEATURE_COLS + domain.MARKET_COLS}
    row["regime"] = "NEUTRAL"
    row["rbsa_weight_1"] = 0.0
    row.update(overrides)
    return row


def _frame(*rows):
    return pd.DataFrame(list(rows))


class ConstantModel:
    """恒定预测分：只测排序配方对特征列的响应，排除模型分干扰。"""

    def predict(self, X):
        return np.full(len(X), 0.5)


def _score(df, idx_mom=0.0):
    cfg = domain.RankingConfig()
    return calculator.score_frame(df.copy(), ConstantModel(), cfg, idx_mom,
                                  default_regime="NEUTRAL",
                                  rbsa_weight_col="rbsa_weight_1")


class TestRelStrengthUsesMom5d:
    def test_higher_mom5d_makes_higher_combo(self):
        """其他相同、5 日动量高 → combo 高（rel_strength 正贡献）。"""
        a = _row(mom_5d=5.0, momentum_20d=1.0)
        b = _row(mom_5d=1.0, momentum_20d=5.0)
        df = _score(_frame(a, b), idx_mom=0.0)
        ca = df.loc[df["mom_5d"] == 5.0, "combo"].iloc[0]
        cb = df.loc[df["mom_5d"] == 1.0, "combo"].iloc[0]
        assert ca > cb

    def test_momentum_20d_no_longer_drives_combo(self):
        """其他相同、20 日动量差异 → combo 不变（不再是相对强弱基准）。"""
        a = _row(mom_5d=3.0, momentum_20d=10.0)
        b = _row(mom_5d=3.0, momentum_20d=1.0)
        df = _score(_frame(a, b), idx_mom=0.0)
        ca = df.loc[df["momentum_20d"] == 10.0, "combo"].iloc[0]
        cb = df.loc[df["momentum_20d"] == 1.0, "combo"].iloc[0]
        assert ca == cb

    def test_rel_strength_value_equals_mom5d_minus_idx(self):
        """rel_strength 列 = mom_5d − 指数动量（配方明确断言）。"""
        a = _row(mom_5d=4.0, momentum_20d=9.0)
        df = _score(_frame(a), idx_mom=1.0)
        assert df.iloc[0]["rel_strength"] == 3.0


class TestModelStillDominant:
    def test_model_weight_unchanged(self):
        """模型分仍为主导权重（0.7，Q7 共识不回归）。"""
        assert domain.RankingConfig().model_weight == 0.7

    def test_no_volatility_main_factor(self):
        """低波动不再作为组合分主因子（多窗口验证无区分度）。"""
        a = _row(vol_20d=0.05)
        b = _row(vol_20d=0.9)
        df = _score(_frame(a, b), idx_mom=0.0)
        ca = df.loc[df["vol_20d"] == 0.05, "combo"].iloc[0]
        cb = df.loc[df["vol_20d"] == 0.9, "combo"].iloc[0]
        assert ca == cb

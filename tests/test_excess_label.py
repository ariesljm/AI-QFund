"""票 09：2.0 主标签（同类中性化超额收益 − λ×最大回撤）纯函数。

excess_adjusted_return 是标签构造核心：λ=0 退化为纯超额、负超额/大回撤
方向性正确；λ 来自配置（settings.toml [label].lambda，初值 1.0）。
样本装配的超额接线（同类净值回填后）与 LABEL_VERSION v3 升版不在此文件。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.config import get_label_lambda
from app.model import excess_adjusted_return


class TestExcessAdjustedReturn:
    def test_normal(self):
        # 超额 +5%，回撤 10%，λ=1 → 5% − 10% = −5%
        assert excess_adjusted_return(0.05, 0.10, 1.0) == pytest.approx(-0.05)

    def test_lambda_zero_degrades_to_pure_excess(self):
        """λ=0 → 纯超额收益（票 09 退化路径）。"""
        assert excess_adjusted_return(0.05, 0.10, 0.0) == pytest.approx(0.05)

    def test_negative_excess_penalized(self):
        assert excess_adjusted_return(-0.03, 0.05, 1.0) == pytest.approx(-0.08)

    def test_no_drawdown_is_pure_excess(self):
        assert excess_adjusted_return(0.04, 0.0, 2.0) == pytest.approx(0.04)

    def test_higher_lambda_penalizes_more(self):
        """λ 越大惩罚越重：同一 (excess, dd) 下 λ=2 严格低于 λ=1。"""
        assert excess_adjusted_return(0.05, 0.10, 2.0) < excess_adjusted_return(0.05, 0.10, 1.0)


class TestLabelLambdaConfig:
    def test_lambda_comes_from_settings(self):
        """λ 初值 1.0（生产标定值，非文档的 1.5~2.0）。"""
        assert get_label_lambda() == 1.0

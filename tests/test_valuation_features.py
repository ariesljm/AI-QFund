"""票 05：个股估值派生特征（重仓股加权 PE 分位）+ 分位逆函数。

接缝：分位与加权都是纯函数。这里测 domain.percentile_of 的逆一致性/边界，
以及 weighted_valuation_percentile 的归一化与缺失处置口径。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.domain import percentile, percentile_of
from app.features.valuation import weighted_valuation_percentile


class TestPercentileOf:
    def test_inverse_consistency(self):
        """percentile_of 是 percentile 的逆：roundtrip 在插值精度内闭合。"""
        values = [3.1, 1.7, 9.9, 4.2, 6.6, 2.0, 8.8]
        for p in [0, 10, 25, 50, 75, 90, 100]:
            assert abs(percentile_of(values, percentile(values, p)) - p) < 1e-9

    def test_midpoint(self):
        assert percentile_of([1, 2, 3, 4], 2.5) == 50.0
        assert percentile_of([1, 2, 3, 4], 1.0) == 0.0
        assert percentile_of([1, 2, 3, 4], 4.0) == 100.0

    def test_duplicates_take_block_midpoint(self):
        """等值块取中点：与 numpy 线性插值口径一致。"""
        assert percentile_of([1, 2, 2, 2, 3], 2) == 50.0

    def test_out_of_bounds_clamped(self):
        assert percentile_of([1, 2, 3], 0) == 0.0
        assert percentile_of([1, 2, 3], 99) == 100.0

    def test_degenerate(self):
        assert percentile_of([], 1.0) == 0.0          # 空序列
        assert percentile_of([5.0], 5.0) == 0.0       # 单点：无秩信息
        assert percentile_of([7, 7, 7, 7], 7) == 50.0  # 全等值：中性
        assert percentile_of([1, 2, 3], 99) == 100.0   # 越界截断


class TestWeightedValuationPercentile:
    def test_equal_weights_both_top(self):
        h = [{"stock_code": "A", "weight": 5}, {"stock_code": "B", "weight": 5}]
        hist = {"A": [10, 20, 30], "B": [10, 20, 30]}   # 末位 30 = 历史最高 → 100 分位
        assert weighted_valuation_percentile(h, hist) == 100.0

    def test_weighted_average_exact(self):
        # A=100 分位（末位历史最高），B=0 分位（末位历史最低），权重 1:3 → 25
        h = [{"stock_code": "A", "weight": 1}, {"stock_code": "B", "weight": 3}]
        hist = {"A": [1, 2, 3], "B": [3, 2, 1]}
        assert abs(weighted_valuation_percentile(h, hist) - 25.0) < 1e-9

    def test_weights_shift_result(self):
        # 同一对分位(100/0)，权重 9:1 → 90（偏向高位股）
        h = [{"stock_code": "A", "weight": 9}, {"stock_code": "B", "weight": 1}]
        hist = {"A": [1, 2, 3], "B": [3, 2, 1]}
        assert abs(weighted_valuation_percentile(h, hist) - 90.0) < 1e-9

    def test_missing_valuation_excluded_from_denominator(self):
        """缺失估值的股票从分子分母一并剔除，不把缺失当 0。"""
        h = [{"stock_code": "A", "weight": 5},
             {"stock_code": "MISSING", "weight": 5},
             {"stock_code": "B", "weight": 5}]
        hist = {"A": [1, 2, 3, 4, 5], "B": [1, 2, 3, 4, 5]}   # 均为 100
        assert weighted_valuation_percentile(h, hist) == 100.0

    def test_all_missing_returns_none(self):
        h = [{"stock_code": "X", "weight": 5}]
        assert weighted_valuation_percentile(h, {}) is None
        assert weighted_valuation_percentile([], {"X": [1, 2, 3]}) is None

    def test_nonpositive_weights_ignored(self):
        h = [{"stock_code": "A", "weight": 0}, {"stock_code": "B", "weight": -1},
             {"stock_code": "C", "weight": 2}]
        hist = {"C": [1, 2, 3]}
        assert weighted_valuation_percentile(h, hist) == 100.0

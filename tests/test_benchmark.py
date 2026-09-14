"""主标尺（超额收益）纯函数测试。

接缝：无——超额收益与同类分组是纯计算，直接构造输入断言输出，不经过决策周期入口。
期望值全部来自手算样例或口径定义（Q3/Q4/Q11），不由被测实现反推。
"""

import pytest

from app import benchmark, domain


class TestPeerGroup:
    def test_uses_rbsa_first_industry(self):
        """同类 = RBSA 第一行业（持仓聚合口径，Q19）。"""
        assert benchmark.peer_group({"rbsa_industry_1": "半导体"}) == "半导体"

    def test_missing_or_blank_is_out_not_fallback(self):
        """RBSA 为空直接出局——不回退到基金类型（Q4 否决了小样本回退）。"""
        assert benchmark.peer_group({"rbsa_industry_1": ""}) is None
        assert benchmark.peer_group({"rbsa_industry_1": None}) is None
        assert benchmark.peer_group({"rbsa_industry_1": "   "}) is None
        assert benchmark.peer_group({}) is None

    def test_no_features_row_is_out(self):
        assert benchmark.peer_group(None) is None


class TestPeerMean:
    def test_hand_computed_mean(self):
        # 0.01 + 0.03 + 0.05 + 7×0.0 = 0.09 → /10 = 0.009
        assert benchmark.peer_mean([0.01, 0.03, 0.05] + [0.0] * 7) == pytest.approx(0.009)

    def test_below_min_samples_is_dropped(self):
        # MIN_PEER_SAMPLES = 10：9 个同类样本不足以构成基准，剔除该样本
        assert benchmark.peer_mean([0.01] * 9) is None

    def test_exactly_min_samples_passes(self):
        assert benchmark.peer_mean([0.02] * 10) == pytest.approx(0.02)

    def test_empty_returns_none(self):
        assert benchmark.peer_mean([]) is None

    def test_all_invalid_returns_none(self):
        assert benchmark.peer_mean([None] * 12) is None

    def test_invalid_peers_excluded_from_sample_count(self):
        # 12 个里只有 9 个有净值 → 有效样本 9 < 10 → 剔除（不是 12 ≥ 10 就放行）
        peers = [0.02] * 9 + [None, float("nan"), None]
        assert benchmark.peer_mean(peers) is None

    def test_nan_does_not_pollute_mean(self):
        # 有效 10 个（0.10 + 9×0.0）→ 均值 0.01；NaN 不得把结果变成 NaN
        peers = [0.10] + [0.0] * 9 + [float("nan")]
        assert benchmark.peer_mean(peers) == pytest.approx(0.01)


class TestExcessReturn:
    def test_hand_computed_difference(self):
        assert benchmark.excess_return(0.10, [0.04] * 10) == pytest.approx(0.06)

    def test_negative_excess(self):
        assert benchmark.excess_return(-0.05, [0.02] * 10) == pytest.approx(-0.07)

    def test_below_min_peer_samples_is_dropped_not_fallback(self):
        assert benchmark.excess_return(0.10, [0.04] * 9) is None

    def test_missing_own_return_is_none(self):
        assert benchmark.excess_return(None, [0.04] * 10) is None

    def test_no_peers_does_not_raise(self):
        assert benchmark.excess_return(0.10, []) is None


class TestBenchmarkDeclaration:
    def test_version_is_declared(self):
        assert isinstance(benchmark.BENCHMARK_VERSION, str)
        assert benchmark.BENCHMARK_VERSION

    def test_min_peer_samples_is_ten(self):
        assert benchmark.MIN_PEER_SAMPLES == 10

    def test_forward_window_is_single_source(self):
        """前向窗口与领域常量同源，标尺不得另立一份 40。"""
        assert benchmark.FORWARD_DAYS == domain.FORWARD_DAYS

    def test_profit_caliber_is_single_source(self):
        """赚钱口径复用 domain.is_profit（>1%），标尺不得自写 >0 名义口径。"""
        assert benchmark.PROFIT_THRESHOLD == domain.PROFIT_THRESHOLD
        assert benchmark.is_profit(0.02) is True
        assert benchmark.is_profit(0.005) is False


class TestVersionGuard:
    def test_matching_version_accepted(self):
        assert benchmark.is_current_benchmark(benchmark.BENCHMARK_VERSION) is True

    def test_stale_or_missing_version_rejected(self):
        """版本不一致的记录拒绝与当前标尺聚合（这才是版本号的用途）。"""
        assert benchmark.is_current_benchmark("excess_rbsa40d_v0") is False
        assert benchmark.is_current_benchmark(None) is False
        assert benchmark.is_current_benchmark("") is False

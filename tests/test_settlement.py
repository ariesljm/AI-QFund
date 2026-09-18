"""结算账本测试：三个统计量分职、标尺版本守卫、结算入口装配。

接缝：决策周期入口（结算阶段是外部行为）+ 纯函数（三统计量各自计算）。
期望值来自手算样例或口径定义，不由被测实现反推。
"""

import pytest

from app import benchmark, domain, settlement

CURRENT = benchmark.BENCHMARK_VERSION


def _s(abs_return=0.05, excess=0.01, confidence=0.6, version=None,
       peer_n=100, max_drawdown=0.08, code="000001", reco_date="2026-01-05"):
    return settlement.Settlement(
        reco_date=reco_date,
        code=code,
        benchmark_version=version if version is not None else CURRENT,
        settle_date="2026-03-05",
        abs_return=abs_return,
        excess_return=excess,
        peer_n=peer_n,
        max_drawdown=max_drawdown,
        confidence=confidence,
    )


class TestThreeStatistics:
    """三统计量分职：主标尺看超额、用户口径看绝对、过程标尺看校准。"""

    def test_benchmark_expectation_is_mean_excess(self):
        # (0.02 − 0.01 + 0.05) / 3 = 0.02
        rows = [_s(excess=0.02), _s(excess=-0.01), _s(excess=0.05)]
        assert settlement.benchmark_expectation(rows) == pytest.approx(0.02)

    def test_absolute_win_rate_uses_profit_threshold_single_source(self):
        """>1% 口径来自 domain.is_profit：0.005 不算赚钱，0.011 算。"""
        rows = [_s(abs_return=0.02), _s(abs_return=0.005),
                _s(abs_return=-0.03), _s(abs_return=0.011)]
        assert settlement.absolute_win_rate(rows) == pytest.approx(0.5)

    def test_calibration_gap_is_confidence_vs_hit_rate(self):
        # 自陈 80%，实际 2/4 = 50% → 偏差 30pp
        rows = [_s(confidence=0.8, abs_return=0.02), _s(confidence=0.8, abs_return=0.02),
                _s(confidence=0.8, abs_return=-0.02), _s(confidence=0.8, abs_return=-0.02)]
        assert settlement.calibration_gap(rows) == pytest.approx(0.3)

    def test_statistics_are_not_interchangeable(self):
        """同一批数据必须给出各自的答案——这是切断「裁判即运动员」的判据。"""
        rows = [_s(abs_return=0.10, excess=-0.02), _s(abs_return=0.10, excess=-0.02)]
        assert settlement.benchmark_expectation(rows) == pytest.approx(-0.02)
        assert settlement.absolute_win_rate(rows) == pytest.approx(1.0)
        # 用户口径赚钱、主标尺却是负超额——两者绝不可互相替代
        assert settlement.benchmark_expectation(rows) < 0 < settlement.absolute_win_rate(rows)


class TestVersionGuard:
    """标尺版本守卫：旧版本记录不得与当前标尺聚合。"""

    def test_stale_rows_excluded_from_every_statistic(self):
        fresh = [_s(excess=0.04, abs_return=0.05)]
        stale = [_s(excess=-0.50, abs_return=-0.50, version="excess_rbsa40d_v0")] * 3
        rows = fresh + stale
        assert settlement.benchmark_expectation(rows) == pytest.approx(0.04)
        assert settlement.absolute_win_rate(rows) == pytest.approx(1.0)

    def test_all_stale_yields_none_not_zero(self):
        """全被守卫拦下时返回 None（无可用样本），而不是 0（会被读成"中性表现"）。"""
        rows = [_s(version="excess_rbsa40d_v0")]
        assert settlement.benchmark_expectation(rows) is None
        assert settlement.absolute_win_rate(rows) is None
        assert settlement.calibration_gap(rows) is None


class TestMissingInputs:
    def test_empty_rows_yield_none(self):
        assert settlement.benchmark_expectation([]) is None
        assert settlement.absolute_win_rate([]) is None
        assert settlement.calibration_gap([]) is None

    def test_rows_without_excess_are_skipped_not_crashing(self):
        rows = [_s(excess=0.02), _s(excess=None), _s(excess=0.06)]
        assert settlement.benchmark_expectation(rows) == pytest.approx(0.04)

    def test_no_confidence_yields_none_gap(self):
        assert settlement.calibration_gap([_s(confidence=None)]) is None

    def test_thin_peer_rows_do_not_enter_excess_aggregate(self):
        """同类样本不足的观测不进超额聚合（n<10 剔除），但绝对收益仍保留。"""
        rows = [_s(excess=None, peer_n=9, abs_return=0.05),
                _s(excess=None, peer_n=9, abs_return=-0.05)]
        assert settlement.benchmark_expectation(rows) is None
        assert settlement.absolute_win_rate(rows) == pytest.approx(0.5)


class TestBuildSettlement:
    """结算入口的装配部分（纯函数）：只接受已算好的收益，不读库。"""

    def test_assembles_row_with_excess(self):
        row = settlement.build(
            reco_date="2026-01-05", code="000001", settle_date="2026-03-05",
            own_return=0.10, max_drawdown=0.06,
            peer_returns=[0.04] * 10, confidence=0.7,
        )
        assert row is not None
        assert row.excess_return == pytest.approx(0.06)
        assert row.peer_n == 10
        assert row.abs_return == pytest.approx(0.10)
        assert row.max_drawdown == pytest.approx(0.06)
        assert row.benchmark_version == CURRENT

    def test_thin_peers_keep_absolute_but_no_excess(self):
        row = settlement.build(
            reco_date="2026-01-05", code="000001", settle_date="2026-03-05",
            own_return=0.10, max_drawdown=0.06, peer_returns=[0.04] * 9,
        )
        assert row is not None
        assert row.excess_return is None
        assert row.peer_n == 9
        assert row.abs_return == pytest.approx(0.10)

    def test_peer_n_counts_valid_peers_only(self):
        row = settlement.build(
            reco_date="2026-01-05", code="000001", settle_date="2026-03-05",
            own_return=0.10, max_drawdown=0.06,
            peer_returns=[0.04] * 12 + [None, float("nan")],
        )
        assert row is not None and row.peer_n == 12

    def test_missing_own_return_yields_no_row(self):
        """自身窗口未满 → 不写账（宁可不记，不可记错）。"""
        assert settlement.build(
            reco_date="2026-01-05", code="000001", settle_date="2026-03-05",
            own_return=None, max_drawdown=None, peer_returns=[0.04] * 10,
        ) is None


class TestWindowMaxDrawdown:
    """窗口最大回撤：口径单一来源（结算列 + 训练标签共用）。"""

    def test_hand_computed(self):
        # 1.00 → 1.20 → 0.90：峰 1.20 谷 0.90 → 25%
        assert domain.window_max_drawdown([1.0, 1.2, 0.9]) == pytest.approx(0.25)

    def test_monotonic_rise_is_zero(self):
        assert domain.window_max_drawdown([1.0, 1.1, 1.2]) == pytest.approx(0.0)

    def test_positive_magnitude_not_negative(self):
        """符号契约：返回正幅值，故标签是 ret − λ·dd（不是 +）。"""
        assert domain.window_max_drawdown([1.0, 0.8]) == pytest.approx(0.2)

    def test_insufficient_or_invalid_points_return_none(self):
        assert domain.window_max_drawdown([]) is None
        assert domain.window_max_drawdown([1.0]) is None
        assert domain.window_max_drawdown([1.0, 0.0]) is None
        assert domain.window_max_drawdown([1.0, None]) is None

    def test_agrees_with_training_label_caliber(self):
        """与标签同口径：label = ret − λ·dd，λ 越大惩罚越重。"""
        navs = [1.0, 1.2, 0.9, 1.0]
        lam = 2.0
        ret = navs[-1] / navs[0] - 1.0
        dd = domain.window_max_drawdown(navs)
        assert dd is not None
        label = domain.excess_adjusted_return(ret, dd, lam)
        assert label == pytest.approx(ret - lam * dd)


class TestLedgerRepo:
    """账本落库与读回（真实表结构，非打桩）。"""

    def test_insert_and_read_roundtrip(self):
        from app import repo

        row = _s(reco_date="2026-05-06", code="123456")
        assert repo.ledger.insert([row]) == 1
        rows = repo.ledger.list_rows(reco_date="2026-05-06")
        assert len(rows) == 1
        assert rows[0]["code"] == "123456"
        assert settlement.load(rows)[0] == row

    def test_resettling_same_window_updates_instead_of_duplicating(self):
        from app import repo

        repo.ledger.insert([_s(reco_date="2026-05-07", code="654321", abs_return=0.01)])
        repo.ledger.insert([_s(reco_date="2026-05-07", code="654321", abs_return=0.09)])
        rows = repo.ledger.list_rows(reco_date="2026-05-07")
        assert len(rows) == 1
        assert rows[0]["abs_return"] == pytest.approx(0.09)

    def test_aggregates_over_rows_read_back_from_db(self):
        from app import repo

        repo.ledger.insert([
            _s(reco_date="2026-05-08", code="A1", excess=0.03, abs_return=0.05),
            _s(reco_date="2026-05-08", code="A2", excess=-0.01, abs_return=-0.05),
        ])
        rows = settlement.load(repo.ledger.list_rows(reco_date="2026-05-08"))
        assert settlement.benchmark_expectation(rows) == pytest.approx(0.01)
        assert settlement.absolute_win_rate(rows) == pytest.approx(0.5)

    def test_empty_insert_is_noop(self):
        from app import repo

        assert repo.ledger.insert([]) == 0

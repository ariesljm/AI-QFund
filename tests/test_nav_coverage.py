"""净值覆盖度缺口测试（ticket 25）。

接缝：纯函数（缺口计算）+ 数据基座步骤入口（覆盖率超阈值必须让流程失败）。

**背景（2026-09-14 实测，不是构造场景）**：全市场 6,161 只基金的净值停更在
2026-09-03，而全局最新为 2026-09-11；这 6 个交易日里每日仍写入约 6,400 行。
既有护栏是 `total_new == 0`——只要还有一只基金在更新，它就永不触发，于是
"少了半个市场"与"全部成功"在日志里长得一模一样。

这组测试钉住：**半数缺失必须被判定为失败**，而不是"有写入就算成功"。
"""

import pytest

from app.data.nav import (
    MAX_NAV_STALE_RATIO,
    NavCoverageError,
    assert_nav_coverage,
    nav_coverage_gap,
)

# 交易日集（与 2026-08-31 ~ 2026-09-11 的真实交易日一致）
DAYS = ["2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04",
        "2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11"]
TARGET = "2026-09-11"


def _ranges(latest_by_code: dict[str, str | None]) -> dict[str, tuple[str, str]]:
    return {code: ("2024-01-02", latest or "") for code, latest in latest_by_code.items()}


class TestNavCoverageGap:
    def test_half_market_stale_is_counted(self):
        """复刻真实故障：一半基金停在 09-03，另一半到 09-11。"""
        local = {f"fresh{i}": TARGET for i in range(100)}
        local.update({f"stale{i}": "2026-09-03" for i in range(100)})
        gap = nav_coverage_gap(_ranges(local), DAYS)

        assert gap["total"] == 200
        assert gap["stale"] == 100
        assert gap["ratio"] == pytest.approx(0.5)
        assert gap["worst_lag"] == 6  # 09-03 → 09-11 隔 6 个交易日
        assert gap["target"] == TARGET
        assert len(gap["samples"]) <= 10

    def test_one_day_lag_is_tolerated(self):
        """QDII/晚一天发布是正常节奏，不该告警（否则阈值会被噪声淹没）。"""
        local = {f"f{i}": TARGET for i in range(99)}
        local["q"] = "2026-09-10"
        assert nav_coverage_gap(_ranges(local), DAYS)["stale"] == 0

    def test_five_day_lag_is_tolerated_but_six_is_not(self):
        """卡在 5~10 之间：5 个交易日是正常发布延迟上限，6 个已经算故障。

        真实故障恰好是 6 个交易日（09-03 → 09-11），而逐只打标阈值是 >10——
        本闸门必须比它更早发现，否则那次故障会继续藏在缝里。
        """
        assert nav_coverage_gap(_ranges({"a": TARGET, "b": "2026-09-04"}), DAYS)["stale"] == 0
        gap = nav_coverage_gap(_ranges({"a": TARGET, "b": "2026-09-03"}), DAYS)
        assert gap["stale"] == 1
        assert gap["worst_lag"] == 6

    def test_ten_day_threshold_would_have_missed_the_real_outage(self):
        """回归锁定：容忍 10 天时故障态与健康态无法区分（都约 0.56%）。"""
        local = {f"f{i}": TARGET for i in range(94)}
        local.update({f"s{i}": "2026-09-03" for i in range(6)})  # 6/100 = 6%
        assert nav_coverage_gap(_ranges(local), DAYS)["ratio"] == pytest.approx(0.06)
        assert nav_coverage_gap(_ranges(local), DAYS, tolerance=10)["stale"] == 0

    def test_fund_without_nav_counts_as_stale(self):
        """无净值的基金（新基金/拉取从未成功）也是缺口，不能当作"无滞后"。"""
        gap = nav_coverage_gap(_ranges({"a": TARGET, "b": None}), DAYS)
        assert gap["stale"] == 1

    def test_healthy_market_is_zero(self):
        gap = nav_coverage_gap(_ranges({f"f{i}": TARGET for i in range(50)}), DAYS)
        assert gap["stale"] == 0 and gap["ratio"] == 0.0

    def test_empty_input_is_not_a_failure(self):
        """空库/无目标日不得误报故障（否则空库自举会被自己拦住）。"""
        assert nav_coverage_gap({}, DAYS)["ratio"] == 0.0
        assert nav_coverage_gap(_ranges({"a": TARGET}), [])["ratio"] == 0.0
        assert nav_coverage_gap(_ranges({"a": None}), DAYS, target=None)["ratio"] == 0.0


class TestCoverageAssertion:
    """超阈值必须**抛错**：流程失败 → 特征步骤不跑 → 下游拿不到陈旧特征。"""

    def test_raises_when_half_market_is_stale(self):
        local = {f"fresh{i}": TARGET for i in range(100)}
        local.update({f"stale{i}": "2026-09-03" for i in range(100)})
        with pytest.raises(NavCoverageError) as ei:
            assert_nav_coverage(_ranges(local), DAYS)
        assert "100/200" in str(ei.value)

    def test_does_not_raise_within_threshold(self):
        # 10/1001 ≈ 1.0% < 5% 阈值：容忍尾部零星停更（健康态实测仅 0.6%）
        local = {f"f{i}": TARGET for i in range(1000)}
        local["x"] = "2026-09-03"
        assert_nav_coverage(_ranges(local), DAYS)

    def test_returns_gap_for_logging(self):
        gap = assert_nav_coverage(_ranges({f"f{i}": TARGET for i in range(50)}), DAYS)
        assert gap["stale"] == 0 and gap["total"] == 50

    def test_threshold_is_calibrated_to_measurements(self):
        """阈值由实测标定（健康 0.6% / 故障 49.2%），改动它必须是有意的。"""
        assert MAX_NAV_STALE_RATIO == pytest.approx(0.05)

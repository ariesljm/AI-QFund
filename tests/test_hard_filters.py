"""票 06：模块一四条硬过滤谓词（纯函数，离网直测）。

阈值单一来源在 domain.hard_filter_violations；数据（AUM/申赎状态/单日上限）
由票 06 数据源供给。这里覆盖四条过滤各自的正反用例 + 边界值 + 缺数据口径。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.domain import (
    AUM_MAX,
    AUM_MIN,
    DAILY_PURCHASE_MIN,
    SHORT_HISTORY_NAVS,
    hard_filter_violations,
)


def _pass_kwargs(**overrides):
    base = {"aum": 500_000_000,           # 5 亿，区间内
            "purchase_status": "normal",
            "daily_limit": None,          # 无限购
            "nav_count": SHORT_HISTORY_NAVS}
    base.update(overrides)
    return base


class TestHardFilters:
    def test_all_pass(self):
        assert hard_filter_violations(**_pass_kwargs()) == []

    # ── 合并规模 ──
    def test_aum_below_min(self):
        assert "aum" in hard_filter_violations(**_pass_kwargs(aum=AUM_MIN - 1))

    def test_aum_above_max(self):
        assert "aum" in hard_filter_violations(**_pass_kwargs(aum=AUM_MAX + 1))

    def test_aum_boundaries_ok(self):
        assert "aum" not in hard_filter_violations(**_pass_kwargs(aum=AUM_MIN))
        assert "aum" not in hard_filter_violations(**_pass_kwargs(aum=AUM_MAX))

    def test_aum_missing_not_violation(self):
        """缺数据不因缺而误杀（由别的机制兜底，不在这里判违规）。"""
        assert "aum" not in hard_filter_violations(**_pass_kwargs(aum=None))

    # ── 申赎状态 ──
    def test_suspended(self):
        assert "suspended" in hard_filter_violations(
            **_pass_kwargs(purchase_status="suspended"))

    def test_limited_not_suspended(self):
        assert "suspended" not in hard_filter_violations(
            **_pass_kwargs(purchase_status="limited"))

    # ── 单日上限 ──
    def test_daily_limit_below_min(self):
        assert "daily_limit" in hard_filter_violations(
            **_pass_kwargs(daily_limit=DAILY_PURCHASE_MIN - 1))

    def test_daily_limit_boundary_ok(self):
        assert "daily_limit" not in hard_filter_violations(
            **_pass_kwargs(daily_limit=DAILY_PURCHASE_MIN))

    def test_daily_limit_none_ok(self):
        assert "daily_limit" not in hard_filter_violations(
            **_pass_kwargs(daily_limit=None))

    # ── 净值历史 ──
    def test_short_history(self):
        assert "short_history" in hard_filter_violations(
            **_pass_kwargs(nav_count=SHORT_HISTORY_NAVS - 1))

    def test_nav_history_boundary_ok(self):
        assert "short_history" not in hard_filter_violations(
            **_pass_kwargs(nav_count=SHORT_HISTORY_NAVS))

    # ── 组合 ──
    def test_multiple_violations(self):
        got = hard_filter_violations(
            **_pass_kwargs(aum=10_000, purchase_status="suspended",
                           daily_limit=500, nav_count=1))
        assert got == ["aum", "suspended", "daily_limit", "short_history"]

    def test_limited_with_low_limit(self):
        """限大额 + 单日上限 < 1000 → 只报 daily_limit，不报 suspended。"""
        got = hard_filter_violations(
            **_pass_kwargs(purchase_status="limited", daily_limit=500))
        assert "daily_limit" in got and "suspended" not in got

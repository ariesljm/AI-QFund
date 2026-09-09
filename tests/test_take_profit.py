"""T08（reco-hardening）：止盈机制回测研究（sim 层验收）。

审计问题：系统只有止损没有止盈，"少数大赢家"利润跑不出来。
回测结论（标定报告第八节）：盈利保护止盈无增益；且监控定位是发
持有/警惕/加仓/离场信号而非盈亏计算——本模块保留为回测研究工具，
不进生产信号链。本测试固化 sim_take_profit 的行为契约。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.features.calculator import sim_take_profit


class TestSimTakeProfit:
    def test_no_profit_holds_to_maturity(self):
        """未达盈利阈值 → 按持有到期结算（不提前卖出）。"""
        navs = [1.0 + 0.003 * i for i in range(41)]  # 40 日后 +12%，< 15%
        ret = sim_take_profit(navs, profit_threshold=0.15, max_days=40)
        assert ret == pytest.approx(navs[40] / navs[0] - 1.0, abs=1e-9)

    def test_profit_then_pullback_triggers(self):
        """盈利 15% 后回撤 12% → 提前结算（锁住利润）。"""
        navs = [1.0]
        for _ in range(20):
            navs.append(navs[-1] * 1.012)   # 涨到 +27%
        peak = navs[-1]
        for _ in range(4):
            navs.append(navs[-1] * 0.96)    # 连续回撤 → 距峰值 -14%
        ret = sim_take_profit(navs, profit_threshold=0.15, pullback_pct=0.12, max_days=40)
        assert ret is not None
        assert ret < peak / 1.0 - 1.0       # 已结算（不是持有到期），且仍 > 0
        assert ret > 0                       # 锁住的是利润

    def test_pullback_below_threshold_holds(self):
        """盈利后小幅回撤（<12%）→ 不触发，持有到期。"""
        navs = [1.0]
        for _ in range(20):
            navs.append(navs[-1] * 1.012)   # +27%
        for _ in range(3):
            navs.append(navs[-1] * 0.99)    # 回撤 ~3%
        while len(navs) < 41:
            navs.append(navs[-1] * 1.005)
        ret = sim_take_profit(navs, profit_threshold=0.15, pullback_pct=0.12, max_days=40)
        assert ret is not None
        assert ret > 0.20                    # 持到期，吃满涨幅

    def test_short_data_returns_none(self):
        assert sim_take_profit([1.0]) is None

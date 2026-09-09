"""T03（reco-hardening）：费用与执行摩擦进回测验收测试。

审计问题：回测只算净值收益，申购费/赎回费/T+1 滑点合计每次交易 1-2%，
可能吃掉动量因子的利润空间。本测试固化：费率分段、到手收益换算、
回测 net 列（默认关闭费用向后兼容、开启时费后口径）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import pytest

from backtest import fees
from backtest.backtest import _attach_forward_returns
from tests.test_backtest_pure_funcs import _make_idx_df, _make_nav_df


class TestRedemptionFee:
    """赎回费率分段表（场外常见档位）。"""

    def test_punitive_under_7_days(self):
        assert fees.redemption_fee_pct(1) == 1.5
        assert fees.redemption_fee_pct(6) == 1.5

    def test_7_to_29_days(self):
        assert fees.redemption_fee_pct(7) == 0.75
        assert fees.redemption_fee_pct(14) == 0.75
        assert fees.redemption_fee_pct(29) == 0.75

    def test_30_to_364_days(self):
        assert fees.redemption_fee_pct(30) == 0.5
        assert fees.redemption_fee_pct(40) == 0.5
        assert fees.redemption_fee_pct(364) == 0.5

    def test_over_one_year_free(self):
        assert fees.redemption_fee_pct(365) == 0.0
        assert fees.redemption_fee_pct(730) == 0.0


class TestNetReturn:
    """毛收益 → 到手收益（申购费 + 赎回费 + 滑点扣减）。"""

    def test_positive_return_reduced(self):
        # +10% 毛收益、持有 40 日（赎回费 0.5%）、申购 0.15%、滑点 0.3%
        net = fees.net_return(0.10, hold_days=40)
        expect = 1.10 * (1 - 0.0045) * (1 - 0.005) - 1
        assert net == pytest.approx(expect, abs=1e-9)
        assert net < 0.10

    def test_negative_return_magnified(self):
        # -5% 毛收益同样扣费 → 到手亏更多
        net = fees.net_return(-0.05, hold_days=40)
        expect = 0.95 * (1 - 0.0045) * (1 - 0.005) - 1
        assert net == pytest.approx(expect, abs=1e-9)
        assert net < -0.05

    def test_hold_days_matters(self):
        # 同收益：7 日内惩罚费 > 30 日后
        short = fees.net_return(0.10, hold_days=5)
        long = fees.net_return(0.10, hold_days=40)
        assert short < long

    def test_zero_fee_returns_gross(self):
        # 申购/滑点/赎回费全关闭 → 纯毛收益
        assert fees.net_return(0.10, hold_days=40, fee_buy_pct=0.0, slippage_pct=0.0,
                               fee_sell_pct=0.0) == pytest.approx(0.10)


class TestAttachNetColumns:
    """回测 net 列：默认关闭（向后兼容）、开启时费后口径。"""

    def _run(self, fee_buy=0.0, slippage=0.0, stop_mode="none"):
        idx_df = _make_idx_df(days=140)
        bt_date = idx_df.index[70]
        dates = [d.strftime("%Y-%m-%d") for d in idx_df.index[70:112]]
        fund = [1.0 + 0.005 * i for i in range(42)]  # 平缓上行 → 40 日后 +20%
        nav_df = _make_nav_df("A", dates, fund)
        df = pd.DataFrame({"code": ["A"]})
        return _attach_forward_returns(df, nav_df, idx_df, bt_date,
                                       stop_mode=stop_mode,
                                       fee_buy_pct=fee_buy, slippage_pct=slippage)

    def test_fee_disabled_net_equals_gross(self):
        """默认（费用关闭）：net 列 == 毛收益列（向后兼容）。"""
        out = self._run()
        row = out.iloc[0]
        assert np.isclose(row["forward_abs_net"], row["forward_abs"])
        assert np.isclose(row["forward_stop_net"], row["forward_stop"])

    def test_fee_enabled_net_below_gross(self):
        """费用开启：到手收益 < 毛收益（持有到期 40 日档赎回费）。"""
        out = self._run(fee_buy=0.15, slippage=0.3)
        row = out.iloc[0]
        assert row["forward_abs_net"] < row["forward_abs"]
        expect = fees.net_return(row["forward_abs"], hold_days=40)
        assert np.isclose(row["forward_abs_net"], expect)

    def test_stop_path_uses_short_hold_fee(self):
        """止损路径按短持有档（STOP_HOLD_DAYS）计费，费率 ≥ 持有到期档。"""
        out = self._run(fee_buy=0.15, slippage=0.3, stop_mode="hard", )
        row = out.iloc[0]
        # hard 止损在平缓上行行情不触发 → stop == abs；net 用短持有档费率
        expect = fees.net_return(row["forward_stop"], hold_days=fees.STOP_HOLD_DAYS)
        assert np.isclose(row["forward_stop_net"], expect)
        # 短持有档赎回费 ≥ 40 日档 → 费后收益不高于毛收益
        assert row["forward_stop_net"] <= row["forward_stop"]


_ = _make_idx_df  # noqa: F401

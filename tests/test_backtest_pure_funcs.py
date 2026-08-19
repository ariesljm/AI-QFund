"""回测纯函数测试：补全 backtest.py / backtest_walkforward.py / sector_signals.py
未覆盖的纯函数（不依赖 DB/model，只接受 DataFrame 参数）。

与 test_backtest_walkforward.py 互补：后者已覆盖 gate_verdict/_pctile_ret/_summarize。
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import pytest

from app import domain
from backtest.backtest import _regime_at_date, _attach_forward_returns
from backtest.backtest_walkforward import _sector_point
from backtest.sector_signals import _cross_sectional_ic


# ── 辅助：构造测试用指数/净值数据 ──────────────────────────

def _make_idx_df(days: int = 100, start: str = "2024-01-01") -> pd.DataFrame:
    """构造指数 DataFrame：DatetimeIndex，close 线性递增，volume 恒定。"""
    idx = pd.date_range(start, periods=days, freq="D")
    return pd.DataFrame({"close": np.arange(100, 100 + days, dtype=float),
                         "volume": np.full(days, 1000.0)}, index=idx)


def _make_nav_df(code: str, dates: list[str], navs: list[float]) -> pd.DataFrame:
    """构造单基金净值 DataFrame（code/date/cum_nav）。"""
    return pd.DataFrame({"code": [code] * len(dates),
                         "date": pd.to_datetime(dates),
                         "cum_nav": navs})


# ── _regime_at_date ─────────────────────────────────────────

class TestRegimeAtDate:
    """回测日 regime：收盘 vs EMA60（查表，纯函数）。"""

    def test_bull_when_close_above_ema60(self):
        idx_df = pd.DataFrame({"close": [100.0], "ema60": [95.0]},
                              index=pd.to_datetime(["2024-03-01"]))
        assert _regime_at_date(idx_df, pd.Timestamp("2024-03-01")) == domain.REGIME_BULL

    def test_bear_when_close_below_ema60(self):
        idx_df = pd.DataFrame({"close": [90.0], "ema60": [95.0]},
                              index=pd.to_datetime(["2024-03-01"]))
        assert _regime_at_date(idx_df, pd.Timestamp("2024-03-01")) == domain.REGIME_BEAR

    def test_neutral_when_date_before_all_rows(self):
        idx_df = pd.DataFrame({"close": [100.0], "ema60": [95.0]},
                              index=pd.to_datetime(["2024-03-01"]))
        # 查询日早于所有指数行 → 无数据 → NEUTRAL
        assert _regime_at_date(idx_df, pd.Timestamp("2024-02-01")) == domain.REGIME_NEUTRAL

    def test_uses_last_row_on_or_before_date(self):
        # 多行时取 <= 查询日的最后一行
        idx_df = pd.DataFrame({"close": [100.0, 80.0], "ema60": [95.0, 95.0]},
                              index=pd.to_datetime(["2024-03-01", "2024-03-02"]))
        assert _regime_at_date(idx_df, pd.Timestamp("2024-03-05")) == domain.REGIME_BEAR


# ── _attach_forward_returns ─────────────────────────────────

class TestAttachForwardReturns:
    """20 日前向收益 + 可选止损模拟（none/atr/hard 三模式）。"""

    def _setup(self, fund_navs: list[float], days: int = 100, bt_offset: int = 70):
        """构造 idx_df + nav_df + df + bt_date；fund_navs 从入场日起含 21 日净值。"""
        idx_df = _make_idx_df(days)
        bt_date = idx_df.index[bt_offset]
        dates = [d.strftime("%Y-%m-%d") for d in idx_df.index[bt_offset:bt_offset + len(fund_navs)]]
        nav_df = _make_nav_df("A", dates, fund_navs)
        df = pd.DataFrame({"code": ["A"]})
        return df, nav_df, idx_df, bt_date

    def test_none_mode_forward_abs_equals_hold_return(self):
        # 净值 1.0 → 1.1（20 日后），无止损 → forward_stop == forward_abs
        fund = [1.0 + 0.005 * i for i in range(22)]  # 平缓上行
        df, nav_df, idx_df, bt_date = self._setup(fund)
        out = _attach_forward_returns(df, nav_df, idx_df, bt_date, stop_mode="none")
        row = out.iloc[0]
        assert np.isclose(row["forward_abs"], fund[20] / fund[0] - 1.0)
        assert np.isclose(row["forward_stop"], row["forward_abs"])
        # alpha = 绝对收益 - 指数收益
        idx_pos = idx_df.index.get_indexer([bt_date])[0]
        idx_fwd_ret = idx_df["close"].iloc[idx_pos + 20] / idx_df["close"].iloc[idx_pos] - 1.0
        assert np.isclose(row["forward_alpha"], row["forward_abs"] - idx_fwd_ret)

    def test_hard_stop_triggers_on_drawdown(self):
        # 净值冲高后回撤 > 10% → 硬止损触发，提前结算
        fund = [1.0, 1.2] + [1.0] * 19  # 冲高到 1.2 后回落到 1.0（回撤 16.7% > 10%）
        df, nav_df, idx_df, bt_date = self._setup(fund)
        out = _attach_forward_returns(df, nav_df, idx_df, bt_date,
                                      stop_mode="hard", stop_param=0.10)
        # 触发日结算收益 = 1.0/1.0 - 1 = 0.0（从入场 1.0 到触发日 1.0）
        assert np.isclose(out.iloc[0]["forward_stop"], 0.0)

    def test_atr_stop_triggers_on_volatility(self):
        # 构造剧烈回撤使 ATR 追踪止损触发：入场后连续下跌
        fund = [1.0, 0.95, 0.85, 0.70, 0.70] + [0.70] * 17
        df, nav_df, idx_df, bt_date = self._setup(fund)
        out = _attach_forward_returns(df, nav_df, idx_df, bt_date,
                                      stop_mode="atr", stop_param=2.0)
        # 触发后止损收益 < 0（净值已跌）
        assert out.iloc[0]["forward_stop"] < 0

    def test_fwd_out_of_range_returns_df_unchanged(self):
        # idx 长度不足以容纳 fwd_pos → 提前 return df（不附加 forward_* 列）
        idx_df = _make_idx_df(days=80)
        bt_date = idx_df.index[70]  # fwd_pos=90 >= 80
        nav_df = _make_nav_df("A", [bt_date.strftime("%Y-%m-%d")], [1.0])
        df = pd.DataFrame({"code": ["A"]})
        out = _attach_forward_returns(df, nav_df, idx_df, bt_date, stop_mode="none")
        assert "forward_abs" not in out.columns

    def test_missing_fund_yields_nan(self):
        # df 中基金不在 nav_df → alpha/abs 为 nan
        idx_df = _make_idx_df(days=100)
        bt_date = idx_df.index[70]
        nav_df = _make_nav_df("OTHER", [bt_date.strftime("%Y-%m-%d")], [1.0])
        df = pd.DataFrame({"code": ["A"]})
        out = _attach_forward_returns(df, nav_df, idx_df, bt_date, stop_mode="none")
        assert pd.isna(out.iloc[0]["forward_abs"])
        assert pd.isna(out.iloc[0]["forward_alpha"])


# ── _sector_point ───────────────────────────────────────────

class TestSectorPoint:
    """赛道内模式单点汇总：随机选 2 赛道，Top1 vs 随机对比。"""

    @staticmethod
    def _make_df(n_per_sector: int = 12, sectors: int = 2) -> pd.DataFrame:
        """构造含 sector/alpha/abs_ret/combo 列的面板。"""
        rows = []
        for s in range(sectors):
            for i in range(n_per_sector):
                rows.append({
                    "sector": f"S{s}", "alpha": float(i),
                    "abs_ret": float(i) * 0.01, "combo": float(i),
                })
        return pd.DataFrame(rows)

    def test_normal_returns_aggregated_dict(self):
        df = self._make_df()
        out = _sector_point(df, pd.Timestamp("2024-03-01"), rng_seed=42, regime="BULL")
        assert out is not None
        assert out["date"] == "2024-03-01"
        assert out["regime"] == "BULL"
        assert out["n_sectors"] == 2
        # Top1 combo 最高 → abs_ret 最大（i=11 → 0.11）
        assert out["top1_abs_pct"] == pytest.approx(0.11 * 100)
        assert out["n_funds"] == 24
        assert -1.0 <= out["ic_sector"] <= 1.0

    def test_insufficient_sectors_returns_none(self):
        # 赛道成员 < 10 → 无 valid 赛道 → None
        df = self._make_df(n_per_sector=5, sectors=2)
        assert _sector_point(df, pd.Timestamp("2024-03-01"), rng_seed=1) is None

    def test_no_sector_column_returns_none(self):
        df = pd.DataFrame({"alpha": [1.0], "abs_ret": [0.01], "combo": [1.0]})
        assert _sector_point(df, pd.Timestamp("2024-03-01"), rng_seed=1) is None

    def test_single_valid_sector_only_uses_one(self):
        # 一赛道 12 只、另一赛道 5 只 → 只 1 个 valid
        df = self._make_df(n_per_sector=12, sectors=1)
        out = _sector_point(df, pd.Timestamp("2024-03-01"), rng_seed=42)
        assert out is not None
        assert out["n_sectors"] == 1


# ── _cross_sectional_ic ─────────────────────────────────────

class TestCrossSectionalIC:
    """截面 IC：每决策日 feat 与目标收益的 Spearman 秩相关均值。"""

    @staticmethod
    def _make_panel(feat_vals: list, target_vals: list, dates: list[str]) -> pd.DataFrame:
        """构造 panel：date/feat/fwd_20d。"""
        rows = []
        for d, f, t in zip(dates, feat_vals, target_vals):
            rows.append({"date": d, "feat": f, "fwd_20d": t})
        return pd.DataFrame(rows)

    def test_perfect_positive_correlation(self):
        # feat 与目标完全正相关 → ic_mean ≈ 1.0
        panel = self._make_panel(
            feat_vals=[1, 2, 3, 4, 5], target_vals=[10, 20, 30, 40, 50],
            dates=["2024-01-01"] * 5)
        out = _cross_sectional_ic(panel, "feat")
        assert out["ic_mean"] == pytest.approx(1.0)
        assert out["n_dates"] == 1
        assert out["n_rows"] == 5
        assert out["ic_positive_pct"] == 1.0

    def test_perfect_negative_correlation(self):
        panel = self._make_panel(
            feat_vals=[1, 2, 3, 4, 5], target_vals=[50, 40, 30, 20, 10],
            dates=["2024-01-01"] * 5)
        out = _cross_sectional_ic(panel, "feat")
        assert out["ic_mean"] == pytest.approx(-1.0)
        assert out["ic_positive_pct"] == 0.0

    def test_constant_series_yields_no_dates(self):
        # 常数序列无秩差异 → spearman None → 不计入 → n_dates=0
        panel = self._make_panel(
            feat_vals=[5, 5, 5, 5, 5], target_vals=[1, 2, 3, 4, 5],
            dates=["2024-01-01"] * 5)
        out = _cross_sectional_ic(panel, "feat")
        assert out["n_dates"] == 0
        assert out["ic_mean"] is None

    def test_empty_panel_returns_zeros(self):
        out = _cross_sectional_ic(pd.DataFrame(columns=["date", "feat", "fwd_20d"]), "feat")
        assert out["n_dates"] == 0
        assert out["n_rows"] == 0
        assert out["ic_mean"] is None

    def test_multi_date_averages_ics(self):
        # 两日：第一日完全正相关(+1)，第二日完全负相关(-1) → 均值 0
        rows = []
        rows.extend({"date": "2024-01-01", "feat": float(i + 1), "fwd_20d": float(i + 1) * 10}
                     for i in range(5))  # +1
        rows.extend({"date": "2024-01-02", "feat": float(i + 1), "fwd_20d": float(5 - i) * 10}
                     for i in range(5))  # -1
        panel = pd.DataFrame(rows)
        out = _cross_sectional_ic(panel, "feat")
        assert out["n_dates"] == 2
        assert out["ic_mean"] == pytest.approx(0.0, abs=1e-9)
        assert out["ic_positive_pct"] == 0.5

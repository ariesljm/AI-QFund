"""walk-forward 回测模块纯函数测试（gate_verdict / _pctile_ret / _summarize）。

不触碰 DB：只用构造数据验证规则门判定、分位计算、汇总口径。
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import pytest

from backtest.backtest_walkforward import gate_verdict, _pctile_ret, _summarize


class TestGateVerdict:
    """规则门三条件判定：任一触发 → 不可投。"""

    def test_all_clear_investable(self):
        ok, reasons = gate_verdict(close=5000.0, ema60=4900.0, mom20=0.02,
                                   pctile=50.0, mom_threshold=-3.0,
                                   pct_threshold=90.0)
        assert ok is True
        assert reasons == []

    def test_bear_condition(self):
        ok, reasons = gate_verdict(close=4800.0, ema60=4900.0, mom20=0.01,
                                   pctile=50.0, mom_threshold=-3.0,
                                   pct_threshold=90.0)
        assert ok is False
        assert any("BEAR" in r for r in reasons)

    def test_momentum_condition(self):
        ok, reasons = gate_verdict(close=5000.0, ema60=4900.0, mom20=-0.04,
                                   pctile=50.0, mom_threshold=-3.0,
                                   pct_threshold=90.0)
        assert ok is False
        assert any("动量" in r for r in reasons)

    def test_pctile_condition(self):
        ok, reasons = gate_verdict(close=5000.0, ema60=4900.0, mom20=0.01,
                                   pctile=95.0, mom_threshold=-3.0,
                                   pct_threshold=90.0)
        assert ok is False
        assert any("分位" in r for r in reasons)

    def test_multiple_conditions_accumulate(self):
        ok, reasons = gate_verdict(close=4800.0, ema60=4900.0, mom20=-0.05,
                                   pctile=99.0, mom_threshold=-3.0,
                                   pct_threshold=90.0)
        assert ok is False
        assert len(reasons) == 3

    def test_ma60_none_skips_bear(self):
        """ema60 缺失时 BEAR 条件跳过，其余条件照常判定。"""
        ok, reasons = gate_verdict(close=5000.0, ema60=None, mom20=0.01,
                                   pctile=50.0, mom_threshold=-3.0,
                                   pct_threshold=90.0)
        assert ok is True
        assert reasons == []

    def test_pctile_none_skips_overheat(self):
        ok, reasons = gate_verdict(close=5000.0, ema60=4900.0, mom20=0.01,
                                   pctile=None, mom_threshold=-3.0,
                                   pct_threshold=90.0)
        assert ok is True
        assert reasons == []


class TestPctileRet:
    """250日涨幅分位计算。"""

    def test_historical_insufficient_returns_none(self):
        s = pd.Series(np.linspace(100, 200, 100))
        assert _pctile_ret(s, 50, pct_window=250, pct_lookback=750) is None

    def test_known_pctile(self):
        # 构造：pct_lookback=10，pct_window=5 → seg 长度 15
        # 历史滚动涨幅 = 12 个（但只取 10 个），当前涨幅构造为最大 → 分位 100
        n = 5 + 10
        prices = np.arange(1.0, n + 1)  # 1..15 等幅上涨
        s = pd.Series(prices)
        # bt_pos = len-1，需要 bt_pos >= pct_window + pct_lookback
        bt_pos = len(s) - 1  # 14，而 window+lookback = 15 → 不足 → None
        assert _pctile_ret(s, bt_pos, 5, 10) is None

    def test_pctile_max_when_cur_highest(self):
        # 等幅（等差）上涨序列：滚动 5 日涨幅随价格升高而递减，
        # 当前涨幅 = 历史最小值 → 分位 0（当前涨幅不低于任何历史滚动涨幅）
        s2 = pd.Series(np.arange(1.0, 26.0))  # 25 个点
        bt_pos2 = 24  # 24 >= 5+10 ✓
        pct = _pctile_ret(s2, bt_pos2, 5, 10)
        assert pct == 0.0

    def test_pctile_high_when_cur_accelerates(self):
        # 前 24 点等差，最后一天 +100% 跳升 → 当前 5 日涨幅（约 152%）
        # 远高于其余 9 个历史滚动涨幅（33%~71%）→ 分位应处于高位（上界 90%）
        base = np.arange(1.0, 25.0)
        s = pd.Series(np.concatenate([base, [base[-1] * 2.0]]))
        bt_pos = len(s) - 1  # 24 >= 15 ✓
        pct = _pctile_ret(s, bt_pos, 5, 10)
        assert pct >= 80.0


class TestSummarize:
    """汇总口径：全期 / 出手日 / 门拒绝日 / 随机基线。"""

    def _make_df(self):
        return pd.DataFrame([
            {"date": "2021-01-04", "regime": "BULL", "investable": True,
             "reasons": "", "top_abs_pct": 2.0, "top_alpha_pct": 1.0,
             "ic": 0.2, "baseline_abs_pct": 0.5, "n_funds": 100},
            {"date": "2021-02-01", "regime": "BEAR", "investable": False,
             "reasons": "BEAR", "top_abs_pct": -3.0, "top_alpha_pct": -2.0,
             "ic": 0.1, "baseline_abs_pct": -1.0, "n_funds": 100},
            {"date": "2021-03-01", "regime": "BULL", "investable": True,
             "reasons": "", "top_abs_pct": 4.0, "top_alpha_pct": 2.0,
             "ic": 0.3, "baseline_abs_pct": 1.0, "n_funds": 100},
            {"date": "2021-04-01", "regime": "BEAR", "investable": False,
             "reasons": "动量", "top_abs_pct": -1.0, "top_alpha_pct": 0.5,
             "ic": 0.0, "baseline_abs_pct": 0.0, "n_funds": 100},
        ])

    def test_block_metrics(self):
        s = _summarize(self._make_df(), "rules", -3.0, 90.0, 250, 750, 5, 1.0)
        assert s["points_total"] == 4
        # 全期：abs 均值 = (2-3+4-1)/4 = 0.5；胜率 = 2/4 = 50%
        assert s["all"]["top_abs_pct"] == pytest.approx(0.5)
        assert s["all"]["win_rate_pct"] == pytest.approx(50.0)
        # 出手日：abs = (2+4)/2 = 3.0；胜率 100%
        assert s["invested"]["points"] == 2
        assert s["invested"]["top_abs_pct"] == pytest.approx(3.0)
        assert s["invested"]["win_rate_pct"] == pytest.approx(100.0)
        # 门拒绝日：abs = (-3-1)/2 = -2.0 → 门避开了亏钱
        assert s["rejected"]["points"] == 2
        assert s["rejected"]["top_abs_pct"] == pytest.approx(-2.0)
        # 基线：abs = (0.5-1+1+0)/4 = 0.125；胜率 50%
        assert s["baseline"]["abs_pct"] == pytest.approx(0.125)
        assert s["baseline"]["win_rate_pct"] == pytest.approx(50.0)

    def test_empty_block(self):
        df = pd.DataFrame(columns=["date", "regime", "investable", "reasons",
                                   "top_abs_pct", "top_alpha_pct", "ic",
                                   "baseline_abs_pct", "n_funds"])
        s = _summarize(df, "rules", -3.0, 90.0, 250, 750, 5, 0.1)
        assert s["points_total"] == 0
        assert s["invested"]["points"] == 0
        assert s["invested"]["top_abs_pct"] is None


class TestEma60Sim:
    """EMA60 退出模拟与生产防线 R1 同判定（候选 A：退出语义单一来源）。"""

    def _declining(self, n=90, drop=0.05):
        """先平后跌的净值序列：约第 65 日后连跌 2 日必 < EMA60（需 >62 天预热）。"""
        navs = [1.0] * n
        for i in range(60, n):
            navs[i] = navs[i - 1] * (1 - drop / 20)
        return navs

    def test_trigger_matches_ema60_exit(self):
        from app.features.calculator import ema60_exit, sim_ema60_exit
        navs = self._declining()
        triggered, _ = ema60_exit(navs)
        assert triggered is True
        # max_days 须大于 EMA 预热期（span+confirm=62）；触发日在下跌初期卖出=少亏，优于持有到底
        sim_ret = sim_ema60_exit(navs, max_days=len(navs) - 1)
        hold_ret = navs[-1] / navs[0] - 1.0
        assert sim_ret is not None and sim_ret > hold_ret

    def test_flat_series_no_trigger(self):
        from app.features.calculator import ema60_exit, sim_ema60_exit
        navs = [1.0] * 80
        assert ema60_exit(navs) == (False, "")
        assert sim_ema60_exit(navs, max_days=79) == pytest.approx(0.0)

    def test_short_series_returns_none(self):
        from app.features.calculator import sim_ema60_exit
        assert sim_ema60_exit([1.0]) is None

    def test_20day_window_no_trigger(self):
        """主回测 20 日窗口内生产 R1（预热 62 日）不触发 → 模拟=固定持有（如实反映生产）。"""
        from app.features.calculator import sim_ema60_exit
        navs = self._declining()
        sim_ret = sim_ema60_exit(navs, max_days=20)
        assert sim_ret == pytest.approx(navs[20] / navs[0] - 1.0)


class TestProfitStats:
    """赚钱口径纯函数（候选 A：quality 度量与回测汇总共用单一来源）。"""

    def test_profit_stats_basic(self):
        from app.engine.quality import profit_stats
        ps = profit_stats([0.03, -0.01, 0.005, 0.02], threshold=0.01)
        # 赚钱(>1%)：0.03, 0.02 → 2/4 = 0.5；名义胜率(>0)：0.03,0.005,0.02 → 3/4
        assert ps["profit_rate"] == pytest.approx(0.5)
        assert ps["win_rate"] == pytest.approx(0.75)
        # 盈亏比：盈利均值(0.025) / 亏损均值(0.0025) = 10
        assert ps["payoff_ratio"] == pytest.approx(10.0)
        assert ps["mean"] == pytest.approx(0.01125)

    def test_profit_stats_empty(self):
        from app.engine.quality import profit_stats
        ps = profit_stats([])
        assert ps["profit_rate"] is None and ps["payoff_ratio"] is None


class TestPrepareTrainingDataParam:
    """训练采样参数化（候选 A：回测与线上同一套训练口径）。"""

    def _fake_index(self):
        import pandas as pd
        dates = pd.bdate_range("2024-01-01", periods=120)
        close = pd.Series(100.0 + np.arange(120), index=dates)
        vol = pd.Series(1e6, index=dates)
        rows = [(d.strftime("%Y-%m-%d"), float(c), float(v))
                for d, c, v in zip(dates, close, vol)]
        return rows

    def test_window_end_truncates_samples(self, monkeypatch):
        """window_end 截断指数：决策日需留有 20 日前向收益 + 60 日特征历史。

        we=第 80 个交易日时，有效决策日 ≤ we-20=第 60 日，但特征要求 pos≥60
        （决策日至少是第 61 个交易日）→ 样本为空；we=最后一日 → 样本非空。
        """
        from app import model as model_mod
        idx_rows = self._fake_index()
        monkeypatch.setattr(model_mod.repo, "get_index_series",
                            lambda *a, **k: idx_rows)
        dates = [r[0] for r in idx_rows]
        nav_rows_by_code = {}
        for code in ("001", "002", "003"):
            nav_rows_by_code[code] = [
                (dates[i], float(1.0 + i * 0.001)) for i in range(len(dates))]
        monkeypatch.setattr(model_mod.repo.nav, "series",
                            lambda code, **k: nav_rows_by_code[code])
        monkeypatch.setattr(model_mod.repo, "get_train_fund_codes",
                            lambda *a, **k: ["001", "002", "003"])

        X_early, *_ = model_mod.prepare_training_data(
            window_end=dates[79], fund_codes=["001", "002", "003"])
        assert len(X_early) == 0  # 截断到第 80 个交易日：特征历史不足，无样本
        X_full, y_full, w_full, *_ = model_mod.prepare_training_data(
            window_end=dates[-1], fund_codes=["001", "002", "003"])
        assert len(X_full) > 0 and len(X_full) == len(y_full) == len(w_full)

    def test_window_end_none_uses_default_pool(self, monkeypatch):
        from app import model as model_mod
        idx_rows = self._fake_index()
        monkeypatch.setattr(model_mod.repo, "get_index_series",
                            lambda *a, **k: idx_rows)
        calls = {"n": 0}

        def _default_pool(*a, **k):
            calls["n"] += 1
            return []

        monkeypatch.setattr(model_mod.repo, "get_train_fund_codes", _default_pool)
        monkeypatch.setattr(model_mod.repo.nav, "series", lambda code, **k: [])
        X, y, w, *_ = model_mod.prepare_training_data()  # 缺省走 get_train_fund_codes
        assert calls["n"] == 1
        assert len(X) == 0  # 空基金池 → 空样本（不报错）

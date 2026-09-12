"""walk-forward 回测纯函数测试（spec 终极验收的测试接缝）。"""

import numpy as np

from app.engine.walk_forward import (
    _slice_window, _quantile_layers, _layer_winrate, _ols, report,
    WINDOW, FORWARD,
)


class TestSliceWindow:
    """升序净值序列二分切片：as_of 及之前最近 window+1 条。"""

    def test_window_slice(self):
        navs = [("2024-01-01", 1.0), ("2024-01-02", 1.1), ("2024-01-03", 1.2),
                ("2024-01-04", 1.3), ("2024-01-05", 1.4)]
        w = _slice_window(navs, "2024-01-04", 2)
        assert [d for d, _ in w] == ["2024-01-02", "2024-01-03", "2024-01-04"]

    def test_as_of_before_all_returns_empty(self):
        navs = [("2024-01-05", 1.0)]
        assert _slice_window(navs, "2024-01-01", 60) == []

    def test_window_larger_than_history(self):
        navs = [("2024-01-01", 1.0), ("2024-01-02", 1.1)]
        w = _slice_window(navs, "2024-01-02", 60)
        assert len(w) == 2


class TestQuantileLayers:
    def test_four_layers(self):
        recs = [{"weight_1": i} for i in range(100)]
        layers = _quantile_layers(recs, "weight_1")
        assert [l[0] for l in layers] == ["Q1", "Q2", "Q3", "Q4"]

    def test_boundaries_monotonic(self):
        recs = [{"weight_1": i} for i in range(100)]
        layers = _quantile_layers(recs, "weight_1")
        bounds = [l[1] for l in layers][1:] + [float("inf")]
        assert all(a <= b for a, b in zip([l[1] for l in layers], bounds))


class TestLayerWinrate:
    def test_winrate_calculation(self):
        recs = [
            {"weight_1": 1.0, "fwd_ret": 0.02},
            {"weight_1": 2.0, "fwd_ret": -0.01},
            {"weight_1": 3.0, "fwd_ret": 0.03},
            {"weight_1": 50.0, "fwd_ret": 0.01},
        ]
        layers = _layer_winrate(recs, "weight_1")
        # Q1 只含 weight_1=1.0 的一条（fwd_ret=+0.02 赢 → 100%）
        q1 = layers[0]
        assert q1["n"] == 1 and q1["winrate"] == 1.0
        # Q4 只含 weight_1=50.0 的一条（fwd_ret=+0.01 赢 → 100%）
        assert layers[-1]["n"] == 1 and layers[-1]["winrate"] == 1.0


class TestOls:
    def test_positive_coef_for_correlated_factor(self):
        rng = np.random.default_rng(3)
        x = rng.normal(0, 1, 200)
        y = 0.5 * x + rng.normal(0, 0.1, 200)
        # 各因子都取随机值（避免常数列导致 X 奇异）
        recs = [{"weight_1": float(w1), "r2": float(xi),
                 "momentum": float(mo), "fwd_ret": float(yi)}
                for w1, xi, mo, yi in zip(rng.normal(0, 1, 200), x,
                                           rng.normal(0, 1, 200), y)]
        reg = _ols(recs)
        assert reg["r2"][1] > 0 and reg["r2"][2] < 0.05

    def test_missing_returns_nan(self):
        assert _ols([])["_n"][1] == 0.0


class TestReport:
    def test_report_renders(self):
        recs = [{"t": "2024-01-01", "code": "A", "weight_1": 30.0, "r2": 0.5,
                 "fwd_ret": 0.02, "momentum": 0.1}] * 10
        text = report(recs)
        assert "回测报告" in text and "40 日赚钱胜率" in text

    def test_empty_recs(self):
        assert report([]) == "无有效样本（数据窗口不足）"


class TestConstants:
    def test_window_matches_style_track(self):
        from app.engine.style_track import _WINDOW
        assert WINDOW == _WINDOW == 60

    def test_forward_matches_domain(self):
        from app.domain import FORWARD_DAYS
        assert FORWARD == FORWARD_DAYS == 40

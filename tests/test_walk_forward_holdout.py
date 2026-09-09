"""T05（reco-hardening）：样本外切分纪律工具验收测试。

纪律：挖数段定参 → 验证段只读不改；差异超阈值标注疑似过拟合。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.walk_forward_holdout import compare_segments, holdout_split


class TestHoldoutSplit:
    def test_eighty_twenty_split(self):
        dates = [f"2024-{m:02d}-01" for m in range(1, 11)]  # 10 个决策日
        train, valid = holdout_split(dates)
        assert len(train) == 8 and len(valid) == 2
        assert train[-1] < valid[0]  # 时间顺序：验证段严格在后

    def test_empty_input(self):
        assert holdout_split([]) == ([], [])

    def test_single_date_goes_train(self):
        train, valid = holdout_split(["2024-01-01"])
        assert train == ["2024-01-01"] and valid == []

    def test_unsorted_input_sorted(self):
        train, valid = holdout_split(["2024-03-01", "2024-01-01", "2024-02-01"])
        assert train == ["2024-01-01", "2024-02-01"]
        assert valid == ["2024-03-01"]


class TestCompareSegments:
    def test_ok_when_similar(self):
        train = {"periods": 40, "profit_rate_pct": 55.0, "mean_top_abs_pct": 1.2, "mean_ic": 0.05}
        valid = {"periods": 10, "profit_rate_pct": 50.0, "mean_top_abs_pct": 0.8, "mean_ic": 0.02}
        r = compare_segments(train, valid)
        assert r["verdict"] == "OK"
        assert r["diff_pp"]["profit_rate_pct"] == -5.0

    def test_suspect_when_winrate_diff_big(self):
        train = {"periods": 40, "profit_rate_pct": 60.0, "mean_top_abs_pct": 1.5, "mean_ic": 0.05}
        valid = {"periods": 10, "profit_rate_pct": 30.0, "mean_top_abs_pct": -0.5, "mean_ic": -0.1}
        r = compare_segments(train, valid)
        assert r["verdict"] == "SUSPECT_OVERFIT"
        assert any("胜率差" in n for n in r["notes"])

    def test_suspect_when_ret_diff_big(self):
        train = {"periods": 40, "profit_rate_pct": 50.0, "mean_top_abs_pct": 2.0, "mean_ic": 0.05}
        valid = {"periods": 10, "profit_rate_pct": 45.0, "mean_top_abs_pct": -4.0, "mean_ic": -0.05}
        r = compare_segments(train, valid)
        assert r["verdict"] == "SUSPECT_OVERFIT"

    def test_missing_keys_handled(self):
        r = compare_segments({"periods": 1}, {"periods": 1})
        assert r["diff_pp"]["profit_rate_pct"] is None

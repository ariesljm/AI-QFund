"""审计 P1-1：单基金特征新鲜度闸门。

全局最新特征日护栏只拦"数据基座整体失败"；单基金特征可滞后最多 10 交易日
（_STALE_NAV_LAG_DAYS）仍以旧快照入池，与实时市场列/赛道中位数构成混合时点
假相对值。修复：候选行携带 feature_date，滞后决策日期望日超过
MAX_FEATURE_LAG_TRADE_DAYS 即剔除。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import pytest

from app.engine import recommend
from app.repo import meta_keys as META

# 固定交易日历：2026-08-24(一) ~ 2026-09-04(五) 剔除周三节假日，共 10 个交易日
TRADE_DAYS = [
    "2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28",
    "2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04",
]


class TestDropStaleFeatureRows:
    @pytest.fixture(autouse=True)
    def _trade_dates(self, monkeypatch):
        def fake_get_meta(key):
            if key == META.TRADE_DATES_CACHE:
                return json.dumps(TRADE_DAYS)
            return None
        monkeypatch.setattr(recommend.repo, "get_meta", fake_get_meta)

    def _df(self, feature_dates):
        rows = [{"code": f"f{i}", "feature_date": d,
                 "momentum_20d": 1.0, "rbsa_weight_1": 50.0} for i, d in enumerate(feature_dates)]
        return pd.DataFrame(rows)

    def test_fresh_rows_kept(self, monkeypatch):
        """特征日 = 期望日（09-04 前一交易日 09-03）→ 全部保留。"""
        monkeypatch.setattr(recommend, "_expected_feature_date",
                            lambda: "2026-09-03")
        df = self._df(["2026-09-03", "2026-09-02", "2026-09-01"])
        out = recommend._drop_stale_feature_rows(df)
        assert len(out) == 3

    def test_stale_rows_dropped(self, monkeypatch):
        """特征日早于期望日 3 个交易日以上 → 剔除（09-03 期望，08-28 滞后 3 交易日被过滤）。"""
        monkeypatch.setattr(recommend, "_expected_feature_date",
                            lambda: "2026-09-03")
        df = self._df(["2026-09-03", "2026-08-28", "2026-08-10"])
        out = recommend._drop_stale_feature_rows(df)
        assert len(out) == 1
        assert out.iloc[0]["code"] == "f0"

    def test_missing_feature_date_dropped(self, monkeypatch):
        """无特征日期 → 保守剔除（信息缺失即不进推荐）。"""
        monkeypatch.setattr(recommend, "_expected_feature_date",
                            lambda: "2026-09-03")
        df = self._df(["2026-09-03", None])
        out = recommend._drop_stale_feature_rows(df)
        assert len(out) == 1

    def test_no_calendar_cache_no_false_positive(self, monkeypatch):
        """无交易日历缓存 → 不误伤（全局护栏兜底）。"""
        monkeypatch.setattr(recommend, "_expected_feature_date", lambda: None)
        df = self._df(["2026-08-10", None])
        out = recommend._drop_stale_feature_rows(df)
        assert len(out) == 2

    def test_no_feature_date_column_noop(self):
        """上游未带 feature_date 列 → 原样返回（不报错）。"""
        df = pd.DataFrame([{"code": "f0", "momentum_20d": 1.0}])
        assert len(recommend._drop_stale_feature_rows(df)) == 1

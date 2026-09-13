"""A2 回归：_check_index_freshness 指数滞后告警（修复前 days 未定义 → NameError 必炸）。

触发现场 = 指数断档：本地指数最新日 < 期望交易日且滞后超阈值。此前该分支
一旦触发就抛 NameError，从 Step 3 on_success 冒泡出 run_pipeline，当天
Step 4（持仓）与 Step 7（特征）被静默跳过——保护链路反而炸断自愈路径。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import app.data.foundation as fd
import app.repo.meta_keys as META
import app.utils.trading_calendar as tc

# 固定交易日历：2026-08-24(一) ~ 2026-09-04(五) 剔除周三节假日，共 10 个交易日
TRADE_DAYS = [
    "2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28",
    "2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04",
]


class TestCheckIndexFreshness:
    @pytest.fixture(autouse=True)
    def _stub_calendar(self, monkeypatch):
        """注入交易日缓存（trading_day_lag 缺省 days 用交易日历，与生产同源）。"""
        monkeypatch.setattr(
            tc, "get_meta",
            lambda key: (json.dumps(TRADE_DAYS)
                         if key == META.TRADE_DATES_CACHE else None))
        monkeypatch.setattr(tc, "_cache", None)

    def _run(self, monkeypatch, latest: str | None, expected: str):
        """patch 指数序列/期望交易日/save_meta，执行核查并返回 meta 捕获。"""
        saved: dict = {}
        monkeypatch.setattr(
            fd, "get_index_rows",
            lambda symbol: [] if latest is None else [(latest, 1.0)])
        monkeypatch.setattr(fd, "expected_trade_date", lambda: expected)
        monkeypatch.setattr(fd, "save_meta",
                            lambda k, v: saved.update(k=k, v=json.loads(v)))
        fd._check_index_freshness(threshold=2)  # 修复前此分支 NameError 必炸
        return saved

    def test_lag_over_threshold_records_warning(self, monkeypatch):
        """指数最新 08-31、期望 09-04（滞后 3 交易日 > 阈值 2）→ 记录 INDEX_FRESHNESS。"""
        saved = self._run(monkeypatch, "2026-08-31", "2026-09-04")
        assert saved["k"] == META.INDEX_FRESHNESS
        assert saved["v"]["lag"] == 4

    def test_lag_within_threshold_silent(self, monkeypatch):
        """指数最新 09-03、期望 09-04（滞后 1 < 阈值 2）→ 不记录。"""
        assert self._run(monkeypatch, "2026-09-03", "2026-09-04") == {}

    def test_index_missing_no_crash(self, monkeypatch):
        """指数完全缺失 → 走 error 日志分支，不抛异常不记录。"""
        assert self._run(monkeypatch, None, "2026-09-04") == {}

    def test_no_calendar_cache_no_false_positive(self, monkeypatch):
        """无交易日历缓存 → lag=0 不误报（与生产'无缓存不误报'一致）。"""
        monkeypatch.setattr(
            tc, "get_meta", lambda key: None)
        monkeypatch.setattr(tc, "_cache", None)
        assert self._run(monkeypatch, "2026-08-31", "2026-09-04") == {}

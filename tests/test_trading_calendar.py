"""交易日历测试：akshare 全年日历 → 缓存 → 判断 → 失败降级。"""

import sys
from pathlib import Path
from datetime import date

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import app.database as db_mod
from app.utils import trading_calendar


@pytest.fixture
def iso_db(monkeypatch, tmp_path):
    """隔离 DB + 重置交易日历模块级缓存。"""
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(db_mod._migrate, "_done", False, raising=False)
    trading_calendar._cache = None
    trading_calendar._last_refresh_at = 0.0
    yield
    trading_calendar._cache = None
    trading_calendar._last_refresh_at = 0.0


def _fake_days():
    """模拟 akshare 返回：2026-08-03(周一)~08-07(周五) 五个交易日。"""
    return ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"]


class TestIsTradingDay:
    def test_weekday_in_calendar(self, iso_db, monkeypatch):
        monkeypatch.setattr(trading_calendar, "_fetch_trade_dates", lambda: _fake_days())
        assert trading_calendar.is_trading_day(date(2026, 8, 3)) is True  # 周一

    def test_weekend_not_trading(self, iso_db, monkeypatch):
        monkeypatch.setattr(trading_calendar, "_fetch_trade_dates", lambda: _fake_days())
        assert trading_calendar.is_trading_day(date(2026, 8, 8)) is False  # 周六

    def test_holiday_weekday_not_trading(self, iso_db, monkeypatch):
        """工作日但不在日历（国庆休市）→ 非交易日。"""
        monkeypatch.setattr(
            trading_calendar, "_fetch_trade_dates",
            lambda: ["2026-09-29", "2026-09-30", "2026-10-08", "2026-10-09"],
        )
        assert trading_calendar.is_trading_day(date(2026, 9, 30)) is True   # 周三 正常
        assert trading_calendar.is_trading_day(date(2026, 10, 1)) is False  # 国庆（周四）
        assert trading_calendar.is_trading_day(date(2026, 10, 8)) is True   # 节后恢复

    def test_cached_no_refetch(self, iso_db, monkeypatch):
        """缓存覆盖当天后，后续判断不再调用 akshare。"""
        calls = []
        monkeypatch.setattr(
            trading_calendar, "_fetch_trade_dates",
            lambda: calls.append(1) or _fake_days(),
        )
        trading_calendar.is_trading_day(date(2026, 8, 3))
        trading_calendar.is_trading_day(date(2026, 8, 4))
        trading_calendar.is_trading_day(date(2026, 8, 5))
        assert len(calls) == 1

    def test_persisted_in_meta(self, iso_db, monkeypatch):
        """拉取结果落库，重启后仍可复用。"""
        monkeypatch.setattr(trading_calendar, "_fetch_trade_dates", lambda: _fake_days())
        trading_calendar.is_trading_day(date(2026, 8, 3))
        with db_mod.db_conn() as conn:
            raw = db_mod.meta_get(conn, trading_calendar._META_KEY)
        assert raw is not None and "2026-08-03" in raw

    def test_refresh_when_cache_expired(self, iso_db, monkeypatch):
        """缓存只覆盖到旧年底（跨年）→ 自动刷新。"""
        with db_mod.db_conn() as conn:
            db_mod.meta_set(conn, trading_calendar._META_KEY,
                            '["2026-12-30", "2026-12-31"]')
        calls = []
        monkeypatch.setattr(
            trading_calendar, "_fetch_trade_dates",
            lambda: calls.append(1) or ["2027-01-04", "2027-01-05"],
        )
        assert trading_calendar.is_trading_day(date(2027, 1, 4)) is True  # 2027-01-04 周一
        assert calls == [1]

    def test_failure_means_not_trading(self, iso_db, monkeypatch):
        """akshare 拉取失败 → 视为非交易日（不启动），即使周一。"""

        def boom():
            raise RuntimeError("akshare down")

        monkeypatch.setattr(trading_calendar, "_fetch_trade_dates", boom)
        assert trading_calendar.is_trading_day(date(2026, 8, 10)) is False  # 周一

    def test_fetch_trims_to_recent_two_years(self, monkeypatch):
        """akshare 返回 1990 年至今全量 → 裁剪为最近两年。"""
        import akshare as ak
        import pandas as pd

        days = [f"{y}-{m:02d}-01" for y in range(1990, 2027) for m in (1, 6)]
        monkeypatch.setattr(ak, "tool_trade_date_hist_sina",
                            lambda: pd.DataFrame({"trade_date": days}))
        result = trading_calendar._fetch_trade_dates()
        assert result == ["2025-01-01", "2025-06-01", "2026-01-01", "2026-06-01"]

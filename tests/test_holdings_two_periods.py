"""票 12 切片一：持仓异动装配的两期读取（get_holdings_two_periods）。

_slices_for 的 prev 装配 seam：最近两期（报告期倒序）→ (cur, prev)，
holdings_change_snapshot 消费。缺失上期 → prev 为空列表（切片显式"无对比基准"）。
"""

import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.data.store as store
import app.database as db_mod
import app.utils.trading_calendar as tc
from app.repo import meta_keys as META
from app.repo.base import get_holdings_two_periods


def _open_tmp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "two_periods.db")


def _weekdays(year: int) -> list[str]:
    days, d = [], date(year, 1, 1)
    while d.year == year:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += timedelta(days=1)
    return days


def _seed_calendar(monkeypatch, days: list[str] | None):
    monkeypatch.setattr(tc, "_history", None)
    if days is not None:
        from app.repo.base import save_meta
        save_meta(META.TRADE_DATES_HISTORY, json.dumps(days))


def _rows(code: str, report_date: str, prefix: str, n: int = 2) -> list[tuple]:
    return [(code, report_date, f"{prefix}{i}", f"股{i}", 5.0 - i) for i in range(n)]


class TestGetHoldingsTwoPeriods:
    def test_two_periods_returns_cur_and_prev(self, monkeypatch, tmp_path):
        """两期存在 → (最新期, 次新期)，各自按权重降序。"""
        _open_tmp_db(monkeypatch, tmp_path)
        _seed_calendar(monkeypatch, _weekdays(2024))
        with db_mod.db_conn() as conn:
            store.save_holdings_batch(conn, _rows("F001", "2023-12-31", "S0"))
            store.save_holdings_batch(conn, _rows("F001", "2024-03-31", "S1"))
        cur, prev = get_holdings_two_periods("F001")
        assert [h["stock_code"] for h in cur] == ["S10", "S11"]
        assert [h["stock_code"] for h in prev] == ["S00", "S01"]

    def test_single_period_prev_empty(self, monkeypatch, tmp_path):
        """仅一期 → prev 为空（切片口径：缺失上期显式可见，不脑补）。"""
        _open_tmp_db(monkeypatch, tmp_path)
        _seed_calendar(monkeypatch, _weekdays(2024))
        with db_mod.db_conn() as conn:
            store.save_holdings_batch(conn, _rows("F001", "2024-03-31", "S1"))
        cur, prev = get_holdings_two_periods("F001")
        assert cur and prev == []

    def test_no_holdings_both_empty(self, monkeypatch, tmp_path):
        _open_tmp_db(monkeypatch, tmp_path)
        _seed_calendar(monkeypatch, _weekdays(2024))
        assert get_holdings_two_periods("F999") == ([], [])

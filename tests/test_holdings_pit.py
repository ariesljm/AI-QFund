"""票 04：持仓公告日（PIT 口径）。

共识 Q15：`fund_holdings` 加 `disclosure_date`，样本只允许 `disclosure_date <= d`；
拿不到公告日则退化为「报告期 + 15 个工作日」的保守滞后（宁晚不早）。

东财 jjcc 页面实测不含公告日期（只有报告期标签），故一律走该保守值。这里测四件事：
精确推算（注入日历，手算对照）、离线退化上界、写入点自动打标、
以及**按决策日取期次**的可见性——最后一条才是闸门本体。
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
from app.repo.base import get_holdings, get_holdings_summaries, save_meta


def _open_tmp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "pit.db")


def _weekdays(year: int) -> list[str]:
    """该年全部工作日（不含周末）——注入日历用，节假日对断言无影响。"""
    days, d = [], date(year, 1, 1)
    while d.year == year:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += timedelta(days=1)
    return days


def _seed_calendar(monkeypatch, days: list[str] | None):
    """注入全历史日历缓存并清模块缓存；None = 模拟离线无日历。"""
    monkeypatch.setattr(tc, "_history", None)
    if days is not None:
        save_meta(META.TRADE_DATES_HISTORY, json.dumps(days))


def _rows(code: str, report_date: str, n: int = 2) -> list[tuple]:
    return [(code, report_date, f"S{i}", f"股票{i}", 5.0 - i) for i in range(n)]


class TestDisclosureDate:
    def test_15th_trading_day_after_report_date(self, monkeypatch, tmp_path):
        """报告期 + 15 个工作日：2024-03-31 → 2024-04-19（手算对照）。"""
        _open_tmp_db(monkeypatch, tmp_path)
        _seed_calendar(monkeypatch, _weekdays(2024))
        assert tc.disclosure_date("2024-03-31", days=tc.cached_history_days()) == "2024-04-19"
        # 跨年：2023-12-31 的 15 个工作日落在 2024-01-19
        assert tc.disclosure_date("2023-12-31", days=tc.cached_history_days()) == "2024-01-19"

    def test_offline_falls_back_to_calendar_day_upper_bound(self, monkeypatch, tmp_path):
        """无日历且不联网 → +31 自然日；且该上界**不早于**精确值（宁晚不早）。"""
        _open_tmp_db(monkeypatch, tmp_path)
        _seed_calendar(monkeypatch, None)
        got = tc.disclosure_date("2024-03-31", days=tc.cached_history_days())
        assert got == "2024-05-01"
        # 对照精确值：退化必须更晚，否则就是泄漏方向
        _seed_calendar(monkeypatch, _weekdays(2024))
        assert got >= tc.disclosure_date("2024-03-31", days=tc.cached_history_days())

    def test_upper_bound_covers_national_day_span(self, monkeypatch, tmp_path):
        """上界要能覆盖国庆长假：即便扣掉 7 个连续节假日，15 个工作日仍在该范围内。"""
        _open_tmp_db(monkeypatch, tmp_path)
        holiday = {f"2024-10-{d:02d}" for d in range(1, 8)}      # 国庆 7 天
        days = [d for d in _weekdays(2024) if d not in holiday]
        _seed_calendar(monkeypatch, days)
        exact = tc.disclosure_date("2024-09-30", days=tc.cached_history_days())
        _seed_calendar(monkeypatch, None)
        assert exact <= tc.disclosure_date("2024-09-30", days=tc.cached_history_days())


class TestWritePath:
    def test_save_holdings_batch_stamps_disclosure_date(self, monkeypatch, tmp_path):
        """写入点自动补公告日：漏写会让 NULL 在 PIT 过滤下等价于永不可见。"""
        _open_tmp_db(monkeypatch, tmp_path)
        _seed_calendar(monkeypatch, _weekdays(2024))
        with db_mod.db_conn() as conn:
            store.save_holdings_batch(conn, _rows("F001", "2024-03-31"))
            got = conn.execute(
                "SELECT DISTINCT disclosure_date FROM fund_holdings "
                "WHERE code='F001'").fetchall()
        assert [r[0] for r in got] == ["2024-04-19"]

    def test_legacy_rows_backfilled_on_migration(self, monkeypatch, tmp_path):
        """旧库无该列：迁移补列并就地回填。日历缓存已在库中 → 精确值。"""
        db_path = tmp_path / "legacy.db"
        monkeypatch.setattr(db_mod, "DB_PATH", db_path)
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO meta VALUES (?, ?)",
                     (META.TRADE_DATES_HISTORY, json.dumps(_weekdays(2024))))
        conn.execute("CREATE TABLE fund_holdings (code TEXT NOT NULL, report_date TEXT NOT NULL, "
                     "stock_code TEXT NOT NULL, stock_name TEXT, weight REAL, "
                     "PRIMARY KEY (code, report_date, stock_code))")
        conn.execute("INSERT INTO fund_holdings VALUES ('F001','2024-03-31','S0','股票0',5.0)")
        conn.commit()
        conn.close()
        monkeypatch.setattr(db_mod, "_INITIALIZED_PATHS", set())
        with db_mod.db_conn() as conn:
            row = conn.execute("SELECT disclosure_date FROM fund_holdings").fetchone()
        assert row[0] == "2024-04-19"

    def test_legacy_backfill_without_calendar_uses_safe_bound(self, monkeypatch, tmp_path):
        """首次打开库时日历尚未缓存 → 落保守上界（迁移不联网），方向偏晚不偏早。"""
        db_path = tmp_path / "legacy2.db"
        monkeypatch.setattr(db_mod, "DB_PATH", db_path)
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE fund_holdings (code TEXT NOT NULL, report_date TEXT NOT NULL, "
                     "stock_code TEXT NOT NULL, stock_name TEXT, weight REAL, "
                     "PRIMARY KEY (code, report_date, stock_code))")
        conn.execute("INSERT INTO fund_holdings VALUES ('F001','2024-03-31','S0','股票0',5.0)")
        conn.commit()
        conn.close()
        monkeypatch.setattr(db_mod, "_INITIALIZED_PATHS", set())
        with db_mod.db_conn() as conn:
            row = conn.execute("SELECT disclosure_date FROM fund_holdings").fetchone()
        assert row[0] == "2024-05-01"          # 报告期 + 31 自然日
        assert row[0] >= "2024-04-19"          # 不早于精确值（不泄漏）


class TestPitVisibility:
    """闸门本体：∀ 决策日 d，可见持仓必须满足 disclosure_date <= d。"""

    def _seed_two_periods(self, monkeypatch, tmp_path):
        _open_tmp_db(monkeypatch, tmp_path)
        _seed_calendar(monkeypatch, _weekdays(2024))
        with db_mod.db_conn() as conn:
            store.save_holdings_batch(conn, _rows("F001", "2023-12-31"))  # 公告 2024-01-19
            store.save_holdings_batch(conn, _rows("F001", "2024-03-31"))  # 公告 2024-04-19

    def test_undisclosed_newer_period_is_invisible(self, monkeypatch, tmp_path):
        """2024-04-10 决策：一季报（04-19 才公告）不可见 → 只能看到 2023-12-31 那期。"""
        self._seed_two_periods(monkeypatch, tmp_path)
        assert get_holdings_summaries(["F001"], as_of="2024-04-10")["F001"]["report_date"] \
            == "2023-12-31"
        assert [h["stock_code"] for h in get_holdings("F001", as_of="2024-04-10")] \
            == ["S0", "S1"]

    def test_period_becomes_visible_on_disclosure_date(self, monkeypatch, tmp_path):
        """边界含当日：d == disclosure_date 即可见；d 早一天仍不可见。"""
        self._seed_two_periods(monkeypatch, tmp_path)
        assert get_holdings_summaries(["F001"], as_of="2024-04-18")["F001"]["report_date"] \
            == "2023-12-31"
        assert get_holdings_summaries(["F001"], as_of="2024-04-19")["F001"]["report_date"] \
            == "2024-03-31"

    def test_pit_monotonicity_no_leak_across_decision_days(self, monkeypatch, tmp_path):
        """扫一遍决策日：任何一天可见的报告期，其公告日都不得晚于当天。"""
        self._seed_two_periods(monkeypatch, tmp_path)
        with db_mod.db_conn() as conn:
            announced = dict(conn.execute(
                "SELECT report_date, disclosure_date FROM fund_holdings "
                "WHERE code='F001' GROUP BY report_date").fetchall())
        day = date(2024, 1, 1)
        while day <= date(2024, 5, 31):
            d = day.isoformat()
            got = get_holdings_summaries(["F001"], as_of=d)["F001"]["report_date"]
            if got is not None:
                assert announced[got] <= d, f"{d} 泄漏了 {got}（公告于 {announced[got]}）"
            day += timedelta(days=1)

    def test_no_as_of_keeps_latest_period(self, monkeypatch, tmp_path):
        """不传决策日（实时推荐路径）维持原行为：取库内最新一期，不受公告日限制。"""
        self._seed_two_periods(monkeypatch, tmp_path)
        assert get_holdings_summaries(["F001"])["F001"]["report_date"] == "2024-03-31"

"""repo.nav 净值时间序列 module 测试：series/latest/at/at_or_before/latest_dates/batch_latest/all_rows。"""

import sqlite3
import pytest

import app.database as db_mod
from app.repo import nav

_NAV_ROWS = [
    ("A", "2026-07-01", 1.00), ("A", "2026-07-02", 1.02), ("A", "2026-07-03", 1.01),
    ("B", "2026-07-01", 2.00), ("B", "2026-07-02", 2.10),
]


@pytest.fixture
def nav_db(monkeypatch, tmp_path):
    """临时库 + fund_nav 表 + 固定净值行（与测试隔离库会话级重定向叠加，局部覆盖）。"""
    db_path = tmp_path / "nav.db"
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE fund_nav (code TEXT, date TEXT, cum_nav REAL)")
    conn.executemany("INSERT INTO fund_nav VALUES (?,?,?)", _NAV_ROWS)
    conn.commit()
    conn.close()


@pytest.fixture
def forward_db(monkeypatch, tmp_path):
    """满 21 条净值窗口（A）与不足窗口（B）/入场净值为 0（X）的固定库。"""
    db_path = tmp_path / "fwd.db"
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    rows = [("A", f"2026-07-{d:02d}", 1.00 + i * 0.01) for i, d in enumerate(range(1, 22))]
    rows += [("B", f"2026-08-{d:02d}", 1.10 + i * 0.01) for i, d in enumerate(range(1, 6))]
    rows += [("X", "2026-07-01", 0.0)]
    rows += [("X", f"2026-07-{d:02d}", 1.00 + i * 0.01) for i, d in enumerate(range(2, 22))]
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE fund_nav (code TEXT, date TEXT, cum_nav REAL)")
    conn.executemany("INSERT INTO fund_nav VALUES (?,?,?)", rows)
    conn.commit()
    conn.close()


class TestSeries:
    def test_ascending(self, nav_db):
        rows = nav.series("A")
        assert [r[0] for r in rows] == ["2026-07-01", "2026-07-02", "2026-07-03"]
        assert [r[1] for r in rows] == [1.00, 1.02, 1.01]

    def test_since_until(self, nav_db):
        rows = nav.series("A", since="2026-07-02", until="2026-07-02")
        assert rows == [("2026-07-02", 1.02)]

    def test_limit_takes_recent_n(self, nav_db):
        rows = nav.series("A", limit=2)
        assert [r[0] for r in rows] == ["2026-07-02", "2026-07-03"]

    def test_unknown_code_empty(self, nav_db):
        assert nav.series("NOPE") == []


class TestForwardReturn:
    """前向收益判定（架构深化 C 收敛）：结算/反事实/质量度量共享的单一来源。"""

    def test_full_window_returns_abs_ret(self, forward_db):
        ret = nav.forward_return("A", "2026-07-01")
        assert ret is not None
        assert abs(ret - 0.20) < 1e-9  # 第 0 条 1.00 → 第 20 条 1.20

    def test_short_window_none(self, forward_db):
        assert nav.forward_return("B", "2026-08-01") is None

    def test_nonpositive_start_nav_none(self, forward_db):
        """入场净值非正（异常数据）→ None，不产生除零/负收益。"""
        assert nav.forward_return("X", "2026-07-01") is None


class TestLatestAndAt:
    def test_latest(self, nav_db):
        assert nav.latest("A") == 1.01
        assert nav.latest("B") == 2.10

    def test_latest_unknown_none(self, nav_db):
        assert nav.latest("NOPE") is None

    def test_at(self, nav_db):
        assert nav.at("A", "2026-07-02") == 1.02
        assert nav.at("A", "2026-07-09") is None

    def test_at_or_before(self, nav_db):
        assert nav.at_or_before("A", "2026-07-02") == 1.02
        # 早于全部行 → None
        assert nav.at_or_before("A", "2026-06-01") is None


class TestBatch:
    def test_latest_dates(self, nav_db):
        assert nav.latest_dates() == {"A": "2026-07-03", "B": "2026-07-02"}

    def test_batch_latest(self, nav_db):
        rows = nav.batch_latest(["A", "B"])
        assert rows[0] == ("A", "2026-07-01", 1.00)
        # 按日期升序，末行为最新日期行
        assert rows[-1] == ("A", "2026-07-03", 1.01)

    def test_batch_latest_empty(self, nav_db):
        assert nav.batch_latest([]) == []

    def test_all_rows(self, nav_db):
        rows = nav.all_rows()
        assert len(rows) == 5
        assert rows[0] == ("A", "2026-07-01", 1.00)
        assert rows[-1] == ("B", "2026-07-02", 2.10)

"""停更打标（mark_stale_funds）与特征新鲜度护栏（_feature_freshness）测试。"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.database as db_mod
import app.engine.recommend as rec
from app.data.foundation import mark_short_history_funds, mark_stale_funds
from app.database import get_db, meta_set
from app.engine.recommend import _feature_freshness


class _FakeDateTime(__import__("datetime").datetime):
    """冻结日期：仅替换 recommend 模块内的 datetime 引用，不污染全局 datetime 类。"""

    @classmethod
    def now(cls, tz=None):
        return cls(2026, 8, 7, 10, 0)


class TestMarkStaleFunds:
    def _fresh_db(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "test.db")
        conn = get_db()  # 真实 schema（fund_basic.name/type NOT NULL）
        return conn

    def test_stale_funds_marked(self, monkeypatch, tmp_path):
        conn = self._fresh_db(monkeypatch, tmp_path)
        try:
            conn.execute("INSERT INTO fund_basic (code, name, type, is_buyable) "
                         "VALUES ('A', '甲', '股票型', 1), ('B', '乙', '股票型', 1), ('C', '丙', '股票型', 1)")
            # A 活跃（最新日期=全局最新）；B 滞后 5 个交易日；C 滞后 15 个交易日
            for i in range(1, 21):
                d = f"2026-01-{i:02d}"
                conn.execute("INSERT INTO fund_nav (code, date, cum_nav) VALUES ('A', ?, 1.0)", (d,))
                if i <= 15:
                    conn.execute("INSERT INTO fund_nav (code, date, cum_nav) VALUES ('B', ?, 1.0)", (d,))
                if i <= 5:
                    conn.execute("INSERT INTO fund_nav (code, date, cum_nav) VALUES ('C', ?, 1.0)", (d,))
            conn.commit()

            n = mark_stale_funds()
            assert n == 1  # 仅 C 滞后超 10 个交易日
            rows = dict(conn.execute("SELECT code, is_buyable FROM fund_basic").fetchall())
            assert rows == {"A": 1, "B": 1, "C": 0}
        finally:
            conn.close()

    def test_no_stale_no_change(self, monkeypatch, tmp_path):
        conn = self._fresh_db(monkeypatch, tmp_path)
        try:
            conn.execute("INSERT INTO fund_basic (code, name, type, is_buyable) VALUES ('A', '甲', '股票型', 1)")
            for i in range(1, 21):
                conn.execute("INSERT INTO fund_nav (code, date, cum_nav) VALUES ('A', ?, 1.0)",
                             (f"2026-01-{i:02d}",))
            conn.commit()
            assert mark_stale_funds() == 0
        finally:
            conn.close()


class TestFeatureFreshness:
    def _cache_db(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "test.db")
        conn = get_db()
        try:
            days = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"]
            meta_set(conn, "trade_dates_cache", json.dumps(days))
        finally:
            conn.close()

    def test_fresh_returns_zero(self, monkeypatch, tmp_path):
        self._cache_db(monkeypatch, tmp_path)
        monkeypatch.setattr(rec, "datetime", _FakeDateTime)
        assert _feature_freshness("2026-08-06") == 0  # 最近交易日 T-1，正常

    def test_one_day_stale(self, monkeypatch, tmp_path):
        self._cache_db(monkeypatch, tmp_path)
        monkeypatch.setattr(rec, "datetime", _FakeDateTime)
        assert _feature_freshness("2026-08-05") == 1  # 滞后 1 个交易日

    def test_two_days_stale(self, monkeypatch, tmp_path):
        self._cache_db(monkeypatch, tmp_path)
        monkeypatch.setattr(rec, "datetime", _FakeDateTime)
        assert _feature_freshness("2026-08-04") == 2

    def test_no_cache_returns_zero(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "test.db")
        monkeypatch.setattr(rec, "datetime", _FakeDateTime)
        assert _feature_freshness("2026-08-04") == 0  # 无缓存不误报


class TestMarkShortHistoryFunds:
    """数据不足打标：净值条数 < EMA_WARMUP_NAVS(62) → is_buyable=0（与 EMA 预热/特征窗口对齐）。"""

    def _fresh_db(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "test.db")
        conn = get_db()
        return conn

    @staticmethod
    def _seed_nav_rows(conn, code: str, n: int) -> None:
        from datetime import date, timedelta
        conn.execute("INSERT INTO fund_basic (code, name, type, is_buyable) "
                     "VALUES (?, ?, '股票型', 1)", (code, f"基金{code}"))
        start = date(2026, 1, 1)
        rows = [(code, (start + timedelta(days=i)).isoformat(), 1.0) for i in range(n)]
        conn.executemany("INSERT INTO fund_nav (code, date, cum_nav) VALUES (?, ?, ?)", rows)

    def test_short_history_marked_long_kept(self, monkeypatch, tmp_path):
        """净值 <62 条 → 打标；≥62 条 → 保留。"""
        from app.features.calculator import EMA_WARMUP_NAVS
        conn = self._fresh_db(monkeypatch, tmp_path)
        try:
            self._seed_nav_rows(conn, "OLD", EMA_WARMUP_NAVS + 8)  # 充足历史
            self._seed_nav_rows(conn, "NEW", 5)                    # 数据不足
            conn.commit()

            n = mark_short_history_funds()
            assert n == 1  # 仅 NEW 打标
            rows = dict(conn.execute("SELECT code, is_buyable FROM fund_basic").fetchall())
            assert rows == {"OLD": 1, "NEW": 0}
        finally:
            conn.close()

    def test_no_nav_not_marked(self, monkeypatch, tmp_path):
        """无净值记录的基金不打标（保持 buyable，等净值拉回后再判）。"""
        conn = self._fresh_db(monkeypatch, tmp_path)
        try:
            conn.execute("INSERT INTO fund_basic (code, name, type, is_buyable) "
                         "VALUES ('NO', '无净值', '股票型', 1)")
            conn.commit()
            assert mark_short_history_funds() == 0
            assert conn.execute(
                "SELECT is_buyable FROM fund_basic WHERE code='NO'").fetchone()[0] == 1
        finally:
            conn.close()

    def test_boundary_exactly_warmup_rows_kept(self, monkeypatch, tmp_path):
        """恰好 62 条不打标（边界含等号），61 条打标。"""
        from app.features.calculator import EMA_WARMUP_NAVS
        conn = self._fresh_db(monkeypatch, tmp_path)
        try:
            self._seed_nav_rows(conn, "EDGE", EMA_WARMUP_NAVS)      # 恰好达标 → 保留
            self._seed_nav_rows(conn, "LOW", EMA_WARMUP_NAVS - 1)   # 差一条 → 打标
            conn.commit()
            assert mark_short_history_funds() == 1
            rows = dict(conn.execute("SELECT code, is_buyable FROM fund_basic").fetchall())
            assert rows == {"EDGE": 1, "LOW": 0}
        finally:
            conn.close()

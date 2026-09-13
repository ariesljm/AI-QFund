"""B2：app/data/store.py 写盘路径单元测试。

背景：store.py 的写路径此前无直接测试（save_nav_batch / save_index_daily /
save_fund_list / backfill_guard）。本文件用临时 SQLite 库覆盖：增量去重、
NAV_RETENTION_DAYS 修剪边界、index_daily 增量 EMA60 递推与中间缺口回补重算、
full_refresh 全量重建、空/单点序列守卫、fund_basic DELETE+INSERT 语义、
backfill_guard total=0 边界。生产代码零改动，仅新增测试文件。

写库方式：save_nav_batch 接收外部 conn（按 schema.sql 手工建表）；其余函数内部
自开 db_conn（monkeypatch db_mod.DB_PATH 到临时库，get_db 首访自动执行 schema.sql）。
"""

import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import app.data.store as store
import app.database as db_mod

SCHEMA_SQL = Path(__file__).resolve().parent.parent / "data" / "schema.sql"


def _open_tmp_db(monkeypatch, tmp_path, name="store.db"):
    """monkeypatch db_mod.DB_PATH 到临时库；db_conn 首访自动执行 schema.sql 建表。"""
    db_path = tmp_path / name
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    return db_path


def _dates(n, start=date(2020, 1, 1)):
    """生成 n 个连续 ISO 日期字符串（交易日等价序列，测试用）。"""
    return [(start + timedelta(days=i)).isoformat() for i in range(n)]


def _nav_rows(dates, base=1.0, step=0.1):
    """把日期列表转成 save_nav_batch 的输入 dict 列表。"""
    return [{"date": d, "cum_nav": base + step * i} for i, d in enumerate(dates)]


def _ref_ema60(closes):
    """独立手算 EMA60 参考序列（与 store._recompute_ema60 同一递推公式）。

    前 _EMA_PERIOD-1 个点无足够种子 → None；第 _EMA_PERIOD 个点起为前 60 日均线，
    之后按 ema = c*k + ema*(1-k) 递推。
    """
    k = store._EMA_K
    period = store._EMA_PERIOD
    emas: list[float | None] = []
    ema: float | None = None
    seed: list[float] = []
    for c in closes:
        if ema is not None:
            ema = c * k + ema * (1 - k)
        else:
            seed.append(c)
            if len(seed) >= period:
                ema = sum(seed[-period:]) / period
        emas.append(ema)
    return emas


class TestSaveNavBatch:
    """save_nav_batch：增量去重（只写 local_max 之后）+ INSERT OR IGNORE。"""

    def test_incremental_dedup_only_new_dates(self, monkeypatch, tmp_path):
        """重复日期不重复写：只写 local_max 之后的新日期，返回值为实际写入条数。"""
        _open_tmp_db(monkeypatch, tmp_path)
        conn = sqlite3.connect(str(db_mod.DB_PATH))
        conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
        rows = _nav_rows(_dates(5))
        assert store.save_nav_batch(conn, "F001", rows) == 5
        # 第二次：rows[2:]（3 条重复/过期）+ 2 条新日期 → 只写 2 条
        new_rows = _nav_rows(_dates(2, date(2020, 1, 6)))
        assert store.save_nav_batch(conn, "F001", rows[2:] + new_rows) == 2
        cnt = conn.execute(
            "SELECT COUNT(*) FROM fund_nav WHERE code='F001'").fetchone()[0]
        assert cnt == 7                       # 5 + 2，重复日期未重复写
        # 原样重发（全过期）→ 0 写入、表不变
        assert store.save_nav_batch(conn, "F001", rows) == 0
        cnt = conn.execute(
            "SELECT COUNT(*) FROM fund_nav WHERE code='F001'").fetchone()[0]
        assert cnt == 7
        conn.close()

    def test_empty_batch_noop_no_trim(self, monkeypatch, tmp_path):
        """空批次：直接返回 0，不触发修剪（防御误删存量）。"""
        _open_tmp_db(monkeypatch, tmp_path)
        conn = sqlite3.connect(str(db_mod.DB_PATH))
        conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
        store.save_nav_batch(conn, "F001", _nav_rows(_dates(10)))
        assert store.save_nav_batch(conn, "F001", []) == 0
        cnt = conn.execute(
            "SELECT COUNT(*) FROM fund_nav WHERE code='F001'").fetchone()[0]
        assert cnt == 10
        conn.close()

    def test_retention_keeps_exact_window_1500(self, monkeypatch, tmp_path):
        """恰好 NAV_RETENTION_DAYS 条：修剪不触发，首末日期全部保留。"""
        _open_tmp_db(monkeypatch, tmp_path)
        conn = sqlite3.connect(str(db_mod.DB_PATH))
        conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
        ds = _dates(store.NAV_RETENTION_DAYS)
        assert store.save_nav_batch(conn, "F001", _nav_rows(ds)) == store.NAV_RETENTION_DAYS
        cnt = conn.execute(
            "SELECT COUNT(*) FROM fund_nav WHERE code='F001'").fetchone()[0]
        assert cnt == store.NAV_RETENTION_DAYS
        oldest = conn.execute(
            "SELECT MIN(date) FROM fund_nav WHERE code='F001'").fetchone()[0]
        assert oldest == ds[0]
        conn.close()

    def test_retention_trims_at_1501(self, monkeypatch, tmp_path):
        """1500 + 1 条：修剪回 1500，最旧一天被淘汰、最新一天保留。"""
        _open_tmp_db(monkeypatch, tmp_path)
        conn = sqlite3.connect(str(db_mod.DB_PATH))
        conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
        ds = _dates(store.NAV_RETENTION_DAYS)
        assert store.save_nav_batch(conn, "F001", _nav_rows(ds)) == store.NAV_RETENTION_DAYS
        last = _dates(1, date(2020, 1, 1) + timedelta(days=store.NAV_RETENTION_DAYS))
        assert store.save_nav_batch(conn, "F001", _nav_rows(last)) == 1
        cnt = conn.execute(
            "SELECT COUNT(*) FROM fund_nav WHERE code='F001'").fetchone()[0]
        assert cnt == store.NAV_RETENTION_DAYS
        oldest = conn.execute(
            "SELECT MIN(date) FROM fund_nav WHERE code='F001'").fetchone()[0]
        assert oldest == ds[1]                # 最旧 ds[0] 被修剪
        conn.close()

    def test_retention_trim_in_single_batch_1501(self, monkeypatch, tmp_path):
        """一次性写入 1501 条：同样修剪至 1500（单批写入也触发修剪）。"""
        _open_tmp_db(monkeypatch, tmp_path)
        conn = sqlite3.connect(str(db_mod.DB_PATH))
        conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
        ds = _dates(store.NAV_RETENTION_DAYS + 1)
        assert store.save_nav_batch(conn, "F001", _nav_rows(ds)) == store.NAV_RETENTION_DAYS + 1
        cnt = conn.execute(
            "SELECT COUNT(*) FROM fund_nav WHERE code='F001'").fetchone()[0]
        assert cnt == store.NAV_RETENTION_DAYS
        dates = sorted(r[0] for r in conn.execute(
            "SELECT date FROM fund_nav WHERE code='F001'").fetchall())
        assert dates[0] == ds[1]              # 最旧一天被修剪
        assert dates[-1] == ds[-1]            # 最新一天保留
        conn.close()


class TestSaveIndexDaily:
    """save_index_daily：EMA60 增量递推 / 缺口回补重算 / full_refresh / 空单点守卫。"""

    def _index_rows(self, dates, closes):
        return [
            {"date": d, "open": c, "high": c + 1, "low": c - 1,
             "close": c, "volume": 100.0}
            for d, c in zip(dates, closes)
        ]

    def _table(self, code):
        with db_mod.db_conn() as conn:
            return conn.execute(
                "SELECT date, close, ema60 FROM index_daily WHERE code=? ORDER BY date",
                (code,)).fetchall()

    def _assert_ema_matches(self, code, ref):
        """表内 ema60 序列必须与独立手算参考完全一致（含种子期 None 头）。"""
        rows = self._table(code)
        actual = [r[2] for r in rows]
        assert actual[:store._EMA_PERIOD - 1] == [None] * (store._EMA_PERIOD - 1)
        assert actual[store._EMA_PERIOD - 1:] == pytest.approx(ref[store._EMA_PERIOD - 1:])

    def test_incremental_append_ema60_sequence(self, monkeypatch, tmp_path):
        """增量追加：EMA60 递推延续，全序列与参考实现一致；重发旧数据 0 写入。"""
        _open_tmp_db(monkeypatch, tmp_path)
        closes = [round(100.0 + i * 0.7, 4) for i in range(65)]
        ds = _dates(65)
        assert store.save_index_daily("sh000001", self._index_rows(ds, closes)) == 65
        self._assert_ema_matches("sh000001", _ref_ema60(closes))
        # 再追加 3 条：递推从 prev_ema 延续，全序列仍等于参考
        closes2 = [round(145.0 + i, 4) for i in range(3)]
        ds2 = _dates(3, date(2020, 1, 1) + timedelta(days=65))
        assert store.save_index_daily("sh000001", self._index_rows(ds2, closes2)) == 3
        rows = self._table("sh000001")
        assert len(rows) == 68
        self._assert_ema_matches("sh000001", _ref_ema60(closes + closes2))
        # 重发全旧数据 → 0 写入、表不变
        assert store.save_index_daily("sh000001", self._index_rows(ds, closes)) == 0
        assert len(self._table("sh000001")) == 68

    def test_gap_backfill_recomputes_ema60(self, monkeypatch, tmp_path):
        """中间缺口回补：触发 _recompute_ema60 全量重算，成熟区与手算参考一致。

        缺口放在成熟区（第 60 行之后）——_recompute_ema60 只回写第 60 行起的行
        （前 59 行种子期保持原值），放在成熟区才能验证重算结果与参考全等。
        """
        _open_tmp_db(monkeypatch, tmp_path)
        all_dates = _dates(71)
        gap_idx = 65  # 缺口日位于成熟区（≥60）
        first_dates = [d for i, d in enumerate(all_dates) if i != gap_idx]
        first_closes = [round(100.0 + i * 0.7, 4) for i in range(70)]
        assert store.save_index_daily(
            "sh000001", self._index_rows(first_dates, first_closes)) == 70
        before = {r[0]: r[2] for r in self._table("sh000001")}
        # 回补缺口日 + 追加新末日 → backfilled=True → _recompute_ema60
        gap_row = self._index_rows([all_dates[gap_idx]], [123.45])
        tail_row = self._index_rows(
            _dates(1, date(2020, 1, 1) + timedelta(days=71)), [170.0])
        assert store.save_index_daily("sh000001", gap_row + tail_row) == 2
        # 成熟区既有行 ema60 发生变化 → 证明重算确实回写了既有行（仅增量不会改写）
        after = {r[0]: r[2] for r in self._table("sh000001")}
        assert before[all_dates[gap_idx + 1]] != after[all_dates[gap_idx + 1]]
        # 全量合并后的手算参考：缺口 close 按日期位置插入序列
        merged_closes = (first_closes[:gap_idx] + [123.45]
                         + first_closes[gap_idx:] + [170.0])
        self._assert_ema_matches("sh000001", _ref_ema60(merged_closes))

    def test_full_refresh_rebuilds_and_reseeds(self, monkeypatch, tmp_path):
        """full_refresh=True：删旧全量重算；不足 60 条时 ema60 全 None；重建 ≥60 条从零递推。"""
        _open_tmp_db(monkeypatch, tmp_path)
        closes = [round(100.0 + i * 0.7, 4) for i in range(65)]
        store.save_index_daily("sh000001", self._index_rows(_dates(65), closes))
        # 全量重建为 5 条（种子期不足 60）→ 表内只剩这 5 条，ema60 全 None
        small_dates = _dates(5, date(2021, 1, 1))
        small_closes = [500.0, 501.0, 502.0, 503.0, 504.0]
        assert store.save_index_daily(
            "sh000001", self._index_rows(small_dates, small_closes),
            full_refresh=True) == 5
        rows = self._table("sh000001")
        assert [r[0] for r in rows] == small_dates
        assert all(r[2] is None for r in rows)
        # 再全量重建 ≥60 条：EMA 从零重算（不继承旧链）
        big_dates = _dates(65, date(2021, 2, 1))
        big_closes = [round(1000.0 + i, 4) for i in range(65)]
        assert store.save_index_daily(
            "sh000001", self._index_rows(big_dates, big_closes),
            full_refresh=True) == 65
        rows = self._table("sh000001")
        assert [r[0] for r in rows] == big_dates
        self._assert_ema_matches("sh000001", _ref_ema60(big_closes))

    def test_empty_and_single_series_no_crash(self, monkeypatch, tmp_path):
        """空序列返回 0 不写；单点序列 ema60 为 None，均不炸。"""
        _open_tmp_db(monkeypatch, tmp_path)
        assert store.save_index_daily("sh000001", []) == 0
        single = [{"date": "2024-01-02", "open": 1.0, "high": 1.1, "low": 0.9,
                   "close": 1.05, "volume": 10.0}]
        assert store.save_index_daily("sh000002", single) == 1
        rows = self._table("sh000002")
        assert len(rows) == 1
        assert rows[0][2] is None


class TestSaveFundList:
    """save_fund_list：DELETE 全表 + 逐条 INSERT OR REPLACE。"""

    def test_delete_then_insert_semantics(self, monkeypatch, tmp_path):
        """旧列表不在新列表中的基金被清掉；同 code 行按新值 REPLACE。"""
        _open_tmp_db(monkeypatch, tmp_path)
        first = [
            {"code": "F001", "name": "甲", "type": "股票型", "is_buyable": 1},
            {"code": "F002", "name": "乙", "type": "混合型", "is_buyable": 1},
            {"code": "F003", "name": "丙", "type": "指数型", "is_buyable": 0},
        ]
        assert store.save_fund_list(first) == 3
        # 第二次：去掉 F002/F003，新增 F004，F001 更新为不可买
        second = [
            {"code": "F001", "name": "甲", "type": "股票型", "is_buyable": 0},
            {"code": "F004", "name": "丁", "type": "债券型", "is_buyable": 1},
        ]
        assert store.save_fund_list(second) == 2
        with db_mod.db_conn() as conn:
            got = {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
                "SELECT code, name, type, is_buyable FROM fund_basic")}
        assert set(got) == {"F001", "F004"}        # DELETE 语义：F002/F003 已清除
        assert got["F001"] == ("甲", "股票型", 0)   # REPLACE 语义：字段更新
        assert got["F004"] == ("丁", "债券型", 1)


class TestBackfillGuard:
    """backfill_guard：total=0 边界与失败率阈值语义。"""

    def test_total_zero_empty_failed_no_crash(self):
        """total=0 且无失败项：直接返回 False，不触碰除法（0/0）不炸。"""
        assert store.backfill_guard([], 0, "label") is False

    def test_threshold_semantics(self):
        assert store.backfill_guard([], 10, "label") is False       # 无失败 → False
        assert store.backfill_guard(["a"], 2, "label") is True      # 50% 未超阈值 → 继续补查
        assert store.backfill_guard(["a", "b"], 2, "label") is False  # 100% > 50% → 跳过
        assert store.backfill_guard(["a", "b", "c"], 10, "label",
                                    threshold=0.2) is False          # 30% > 20% → 跳过

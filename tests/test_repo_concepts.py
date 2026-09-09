"""候选2 深化测试：数据访问层单一归属收敛。

覆盖 Q3 概念聚合读服务（foundation/nav 内联读的单一归属）、
Q4 连接工厂缓存（同一库路径只初始化一次）、
Q1 store 写归（save_fund_features/trim_fund_features 已迁入 data/store）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.data import store
from app.database import db_conn
from app.repo import base as repo


def _seed() -> None:
    """造最小数据（幂等：session 级隔离库共享，重复调用先清表）。"""
    with db_conn() as conn:
        for t in ("fund_features", "fund_holdings", "fund_nav", "index_daily",
                  "stock_industry_map", "fund_basic"):
            conn.execute(f"DELETE FROM {t}")
        conn.execute(
            "INSERT INTO fund_basic (code, name, type, is_buyable) VALUES (?, ?, ?, 1)",
            ("000001", "测试基金", "股票型"))
        conn.execute(
            "INSERT INTO fund_nav (code, date, cum_nav) VALUES "
            "('000001','2026-08-01',1.0), ('000001','2026-08-02',1.02)")
        conn.execute(
            "INSERT INTO fund_holdings (code, report_date, stock_code, stock_name, weight) "
            "VALUES ('000001','2026-06-30','601899','紫金矿业',10.0)")
        conn.execute(
            "INSERT INTO index_daily (code, date, close, volume) "
            "VALUES ('sh000300','2026-08-02',4000.0,1e9)")


class TestConceptServices:
    """Q3：foundation/nav 内联读收敛后的概念服务（单一归属）。"""

    def test_buyable_codes_no_conn(self):
        """get_buyable_codes 不再暴露 conn 参数（Q5），纯查询可用。"""
        _seed()
        assert repo.get_buyable_codes() == ["000001"]

    def test_nav_time_state(self):
        """每基金净值区间 + 全日期集（打标/陈旧/增量下载共用）。"""
        _seed()
        ranges, dates = repo.get_nav_time_state()
        assert ranges["000001"] == ("2026-08-01", "2026-08-02")
        assert dates == ["2026-08-01", "2026-08-02"]

    def test_has_nav_data_and_index_data(self):
        _seed()
        assert repo.has_nav_data() is True
        assert repo.has_index_data() is True

    def test_holdings_report_dates(self):
        _seed()
        assert repo.get_holdings_report_dates() == {"000001": "2026-06-30"}

    def test_industry_map_services(self):
        """映射缺口 / 同步输入 / 统计三者语义衔接。"""
        _seed()
        assert repo.get_industry_map_gap_count() == 1          # 601899 未映射
        all_stocks, mapped = repo.get_industry_map_targets()
        assert all_stocks == ["601899"] and mapped == set()
        assert repo.get_industry_map_stats() == (0, 1)         # 已映射 0, 有持仓基金 1


class TestConnectionFactory:
    """Q4-B：连接工厂按库路径缓存初始化（同一路径不重复 init/migrate）。"""

    def test_initialized_marker_per_path(self):
        """首次连接后记录当前库路径；再次取连接不再重复 schema/migrate 成本。"""
        import app.database as db_mod
        _seed()
        # conftest 已隔离 DB_PATH 到临时库，首次 db_conn 触发一次初始化
        assert str(db_mod.DB_PATH) in db_mod._INITIALIZED_PATHS
        # 路径缓存先记录，重复连接不报错且数据可见（初始化幂等）
        with db_conn() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM fund_basic").fetchone()[0] == 1


class TestStoreFeatureWrite:
    """Q1：可重建表写归 data/store（save_fund_features/trim_fund_features 已迁入）。"""

    def test_save_and_trim_via_store(self):
        _seed()
        features = {"code": "000001", "date": "2026-08-02", "regime": "BULL",
                    "hurst_60d": 0.5, "momentum_20d": 1.2, "calmar": 0.3}
        store.save_fund_features(features)
        # 写路径不再经 repo（getattr 兜底断言：避免未来误引旧名）
        assert getattr(repo, "save_fund_features", None) is None
        with db_conn() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM fund_features WHERE code='000001'").fetchone()[0]
        assert n == 1
        store.trim_fund_features(0)
        with db_conn() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM fund_features WHERE code='000001'").fetchone()[0]
        assert n == 0

    def test_meta_goes_through_repo(self):
        """meta 写收敛到 repo.save_meta（Q2）：database.meta_set 无直接外部调用。"""
        repo.save_meta("test_key_xy", "v")
        assert repo.get_meta("test_key_xy") == "v"
        import app.repo.decision as dec
        assert not hasattr(dec, "meta_set")  # decision 不再持有 meta 直连


class TestHoldingsSummaries:
    """候选批量持仓素材（候选 8 N+1 收敛）：最新报告期 + top-N + 空降级。"""

    def test_batch_latest_report_topn(self, monkeypatch, tmp_path):
        import sqlite3

        import app.repo.base as base_mod

        db = tmp_path / "hold.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE fund_holdings (code TEXT, report_date TEXT, "
                     "stock_code TEXT, stock_name TEXT, weight REAL)")
        conn.execute("CREATE TABLE stock_industry_map (stock_code TEXT PRIMARY KEY, industry_name TEXT)")
        conn.executemany("INSERT INTO fund_holdings VALUES (?, ?, ?, ?, ?)", [
            ("F1", "2026-06-30", "S1", "股1", 30.0),
            ("F1", "2026-06-30", "S2", "股2", 20.0),
            ("F1", "2026-06-30", "S3", "股3", 10.0),
            ("F1", "2026-03-31", "S9", "旧股", 99.0),  # 历史报告期不应出现
            ("F2", "2026-06-30", "S4", "股4", 5.0),
        ])
        conn.execute("INSERT INTO stock_industry_map VALUES ('S1', '白酒')")
        conn.commit()
        monkeypatch.setattr(base_mod, "db_conn", lambda: sqlite3.connect(db))
        monkeypatch.setattr(repo, "db_conn", lambda: sqlite3.connect(db))

        res = repo.get_holdings_summaries(["F1", "F2", "F3"], limit=2)
        # F1：最新报告期 2026-06-30，top-2 权重降序（历史期 99% 旧股被排除）
        assert res["F1"]["report_date"] == "2026-06-30"
        assert [h["stock_code"] for h in res["F1"]["holdings"]] == ["S1", "S2"]
        assert res["F1"]["holdings"][0]["industry"] == "白酒"
        # F2：只有 1 条持仓；F3 无记录 → 空
        assert len(res["F2"]["holdings"]) == 1
        assert res["F3"] == {"holdings": [], "report_date": None}
        assert repo.get_holdings_summaries([]) == {}

"""部署健壮性测试：schema 单一真相 + meta 窄读（架构深化 E/I）。

架构深化 E：schema.sql 是唯一 DDL 真相，_migrate 仅做历史 ALTER 迁移；
架构深化 I：时间戳/游标解析收敛为 repo.get_interval_days / get_int_cursor 窄读。
"""

import sqlite3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime, timedelta

import app.database as db_mod
from app.database import get_db
import app.repo.base as repo_base


class TestMigrateNoFallbackCreates:
    """架构深化 E：兜底 CREATE 已删——schema 单一真相（schema.sql 负责建表，
    _migrate 仅做历史 ALTER 迁移；缺表时静默空跑，不恢复双真相）。"""

    def test_migrate_skips_when_no_tables(self, monkeypatch, tmp_path):
        """schema 未初始化（无表）时 _migrate 不建任何业务表（幂等空跑）。"""
        def _noop_init_schema(conn):
            pass
        monkeypatch.setattr(db_mod, "_init_schema", _noop_init_schema)
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "test.db")

        conn = get_db()
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        finally:
            conn.close()

        assert "quality_metrics" not in tables
        assert "recommend_log" not in tables

    def test_schema_file_creates_tables(self, monkeypatch, tmp_path):
        """schema.sql 正常加载 → 建全表（正常路径，Docker 镜像内备份同样可达）。"""
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "test.db")

        conn = get_db()
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        finally:
            conn.close()

        assert "quality_metrics" in tables
        assert "empty_recommendations" in tables


class TestMetaNarrowReads:
    """架构深化 I：meta 间隔/游标窄读（无记录/解析失败统一兜底）。"""

    @staticmethod
    def _seed(monkeypatch, tmp_path, kv: dict):
        db_path = tmp_path / "meta.db"
        monkeypatch.setattr(db_mod, "DB_PATH", db_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        for k, v in kv.items():
            conn.execute("INSERT INTO meta VALUES (?, ?)", (k, v))
        conn.commit()
        conn.close()

    def test_interval_days(self, monkeypatch, tmp_path):
        old = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
        self._seed(monkeypatch, tmp_path, {"k": old})
        assert repo_base.get_interval_days("k") == 5

    def test_interval_days_missing_none(self, monkeypatch, tmp_path):
        self._seed(monkeypatch, tmp_path, {})
        assert repo_base.get_interval_days("k") is None

    def test_interval_days_bad_format_none(self, monkeypatch, tmp_path):
        self._seed(monkeypatch, tmp_path, {"k": "not-a-date"})
        assert repo_base.get_interval_days("k") is None

    def test_int_cursor(self, monkeypatch, tmp_path):
        self._seed(monkeypatch, tmp_path, {"k": "42"})
        assert repo_base.get_int_cursor("k") == 42

    def test_int_cursor_missing_zero(self, monkeypatch, tmp_path):
        self._seed(monkeypatch, tmp_path, {})
        assert repo_base.get_int_cursor("k") == 0

    def test_int_cursor_bad_format_zero(self, monkeypatch, tmp_path):
        self._seed(monkeypatch, tmp_path, {"k": "abc"})
        assert repo_base.get_int_cursor("k") == 0


class TestGetHoldingCodesSector:
    """修复 A：get_holding_codes 的 sector 优先取推荐入库赛道（feature_snapshot.sector），
    回退当前 RBSA 第一行业——监控证伪与推荐使用同一赛道判定。"""

    def _setup(self, monkeypatch, tmp_path):
        import app.database as db_mod
        monkeypatch.setattr(db_mod, "DB_PATH", str(tmp_path / "test.db"))
        from app.repo import decision as decision_mod
        with decision_mod.db_conn() as conn:
            conn.execute(
                "INSERT INTO fund_basic (code, name, type) VALUES ('018517', '测试基金', '混合型')"
            )
            conn.execute(
                "INSERT INTO fund_features (code, date, rbsa_industry_1, rbsa_weight_1,"
                " rbsa_industry_2, rbsa_weight_2, rbsa_industry_3, rbsa_weight_3)"
                " VALUES ('018517', '2026-07-31', '半导体', 4.6, '通信设备', 4.1, '电源设备', 4.1)"
            )
        return decision_mod

    def test_sector_prefers_feature_snapshot(self, monkeypatch, tmp_path):
        """推荐时确定的赛道（电源设备）优先于当前 RBSA1（半导体）。"""
        decision_mod = self._setup(monkeypatch, tmp_path)
        with decision_mod.db_conn() as conn:
            conn.execute(
                "INSERT INTO recommend_log (recommend_date, code, name, status, feature_snapshot)"
                " VALUES ('2026-08-03', '018517', '测试基金', 'HOLD',"
                " '{\"sector\": \"电源设备\", \"momentum_20d\": 7.6}')"
            )
        rows = decision_mod.get_holding_codes(("HOLD",))
        assert len(rows) == 1
        assert rows[0]["sector"] == "电源设备"

    def test_sector_fallback_to_rbsa1(self, monkeypatch, tmp_path):
        """旧推荐无 feature_snapshot.sector → 回退 RBSA 第一行业。"""
        decision_mod = self._setup(monkeypatch, tmp_path)
        with decision_mod.db_conn() as conn:
            conn.execute(
                "INSERT INTO recommend_log (recommend_date, code, name, status)"
                " VALUES ('2026-08-03', '018517', '测试基金', 'HOLD')"
            )
        rows = decision_mod.get_holding_codes(("HOLD",))
        assert len(rows) == 1
        assert rows[0]["sector"] == "半导体"


class TestClearRecommendationsIncludesMacroNews:
    """清除推荐数据应一并清除每日宏观摘要（板块轮动/资金流向/AI赛道分析的数据源）。"""

    def test_clear_removes_macro_news(self, monkeypatch, tmp_path):
        import app.database as db_mod
        monkeypatch.setattr(db_mod, "DB_PATH", str(tmp_path / "test.db"))
        from app.repo import decision as decision_mod
        with decision_mod.db_conn() as conn:
            conn.execute(
                "INSERT INTO recommend_log (recommend_date, code, name, status)"
                " VALUES ('2026-08-03', '018517', '测试基金', 'HOLD')"
            )
            conn.execute(
                "INSERT INTO macro_news (date, news_summary, top_gainers)"
                " VALUES ('2026-08-03', '新闻', '半导体(+3.2%)')"
            )
        counts = decision_mod.clear_recommendations()
        assert counts["recommend_log"] == 1
        assert counts["macro_news"] == 1
        with decision_mod.db_conn() as conn:
            assert conn.execute("SELECT COUNT(*) FROM macro_news").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM recommend_log").fetchone()[0] == 0

    def test_count_includes_macro_news(self, monkeypatch, tmp_path):
        import app.database as db_mod
        monkeypatch.setattr(db_mod, "DB_PATH", str(tmp_path / "test.db"))
        from app.repo import decision as decision_mod
        with decision_mod.db_conn() as conn:
            conn.execute(
                "INSERT INTO macro_news (date, news_summary)"
                " VALUES ('2026-08-03', '新闻')"
            )
        counts = decision_mod.count_recommendation_domain()
        assert counts["macro_news"] == 1


class TestGetLatestRecommendationsDedupe:
    """UI 今日推荐去重：最新推荐日期内同一基金只出现一次（8-04 线上脏数据复现）。

    线上：基金 012428 同日被重复推荐三次（半导体/通信设备两赛道 + 重复运行），
    旧实现取“最新 N 条”导致 UI 今日推荐两只都是同一基金。
    """

    def test_same_fund_deduped_within_latest_date(self, monkeypatch, tmp_path):
        import app.database as db_mod
        monkeypatch.setattr(db_mod, "DB_PATH", str(tmp_path / "test.db"))
        from app.repo import decision as decision_mod
        with decision_mod.db_conn() as conn:
            # 同日三条记录均为同一基金（重复推荐脏数据）
            for _ in range(3):
                conn.execute(
                    "INSERT INTO recommend_log (recommend_date, code, name, score, status)"
                    " VALUES ('2026-08-04', '012428', '测试基金', 0.05, 'HOLD')"
                )
            # 更早日期的正常推荐，不应混入最新日期
            conn.execute(
                "INSERT INTO recommend_log (recommend_date, code, name, score, status)"
                " VALUES ('2026-08-02', '021180', '另一只', 0.02, 'HOLD')"
            )
        recs = decision_mod.get_latest_recommendations(2)
        assert len(recs) == 1, f"同日重复基金应去重为 1 条: {recs}"
        assert recs[0]["code"] == "012428"
        assert recs[0]["date"] == "2026-08-04"

    def test_different_funds_same_date_kept(self, monkeypatch, tmp_path):
        """最新日期同日两只不同基金 → 去重后保留两只，不混入更早日期。"""
        import app.database as db_mod
        monkeypatch.setattr(db_mod, "DB_PATH", str(tmp_path / "test.db"))
        from app.repo import decision as decision_mod
        with decision_mod.db_conn() as conn:
            conn.execute(
                "INSERT INTO recommend_log (recommend_date, code, name, score, status)"
                " VALUES ('2026-08-04', '012428', '基金A', 0.05, 'HOLD')"
            )
            conn.execute(
                "INSERT INTO recommend_log (recommend_date, code, name, score, status)"
                " VALUES ('2026-08-04', '021180', '基金B', 0.02, 'HOLD')"
            )
            conn.execute(
                "INSERT INTO recommend_log (recommend_date, code, name, score, status)"
                " VALUES ('2026-08-02', '017434', '更早', 0.01, 'HOLD')"
            )
        recs = decision_mod.get_latest_recommendations(2)
        assert {r["code"] for r in recs} == {"012428", "021180"}
        assert all(r["date"] == "2026-08-04" for r in recs)


class TestRecommendLogIdempotent:
    """同日幂等：同 (recommend_date, code) 重复写入更新原行，不追加（防重复推荐记录）。"""

    def _setup(self, monkeypatch, tmp_path):
        import app.database as db_mod
        monkeypatch.setattr(db_mod, "DB_PATH", str(tmp_path / "test.db"))
        from app.repo import decision as decision_mod
        return decision_mod

    def test_same_day_same_code_updates_in_place(self, monkeypatch, tmp_path):
        decision_mod = self._setup(monkeypatch, tmp_path)
        id1 = decision_mod.insert_recommendation(
            "2026-08-12", "002910", "易方达供给改革混合", 2, 0.05, 4.24,
            "BEAR", "第一次推荐")
        id2 = decision_mod.insert_recommendation(
            "2026-08-12", "002910", "易方达供给改革混合", 2, 0.06, 4.30,
            "BEAR", "重跑后的推荐")
        assert id1 == id2                      # id 稳定（监控/sector_selections 引用不悬空）
        with decision_mod.db_conn() as conn:
            rows = conn.execute(
                "SELECT code, score, combo, buy_reason FROM recommend_log"
                " WHERE recommend_date='2026-08-12'").fetchall()
        assert len(rows) == 1                  # 不追加
        assert rows[0][0] == "002910"
        assert rows[0][1] == 0.06              # 内容更新为重跑结果
        assert rows[0][3] == "重跑后的推荐"

    def test_different_codes_both_kept(self, monkeypatch, tmp_path):
        """同一天不同基金各自成行（不同赛道推荐互不覆盖）。"""
        decision_mod = self._setup(monkeypatch, tmp_path)
        decision_mod.insert_recommendation("2026-08-12", "019115", "东财卓越成长A", 1, 0.07, 5.0, "NEUTRAL", "a")
        decision_mod.insert_recommendation("2026-08-12", "002910", "易方达供给改革混合", 2, 0.05, 4.24, "BEAR", "b")
        with decision_mod.db_conn() as conn:
            rows = conn.execute(
                "SELECT code FROM recommend_log WHERE recommend_date='2026-08-12'"
                " ORDER BY rank").fetchall()
        assert [r[0] for r in rows] == ["019115", "002910"]

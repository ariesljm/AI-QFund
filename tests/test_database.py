"""部署健壮性测试：旧 schema 卷下 _migrate 兜底创建新表。

场景：生产 data/ 卷中存有旧版 schema.sql，会遮蔽镜像内新 schema.sql，
导致 quality_metrics / empty_recommendations 不会被 _init_schema 创建。
本测试关闭 schema 初始化，仅走 _migrate，验证两表仍被兜底创建。
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.database as db_mod
from app.database import get_db


class TestMigrateCreatesNewTables:
    def test_new_tables_created_when_schema_stale(self, monkeypatch, tmp_path):
        def _noop_init_schema(conn):
            pass

        monkeypatch.setattr(db_mod, "_init_schema", _noop_init_schema)
        # _done 是函数属性，仅在其他测试先触发迁移后存在；raising=False 允许单跑本文件
        monkeypatch.setattr(db_mod._migrate, "_done", False, raising=False)
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "test.db")

        conn = get_db()
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        finally:
            conn.close()

        assert "quality_metrics" in tables
        assert "empty_recommendations" in tables


class TestGetHoldingCodesSector:
    """修复 A：get_holding_codes 的 sector 优先取推荐入库赛道（feature_snapshot.sector），
    回退当前 RBSA 第一行业——监控证伪与推荐使用同一赛道判定。"""

    def _setup(self, monkeypatch, tmp_path):
        import app.database as db_mod
        monkeypatch.setattr(db_mod, "DB_PATH", str(tmp_path / "test.db"))
        from app.repo import decision as decision_mod
        with decision_mod.db() as conn:
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
        with decision_mod.db() as conn:
            conn.execute(
                "INSERT INTO recommend_log (recommend_date, code, name, status, feature_snapshot)"
                " VALUES ('2026-08-03', '018517', '测试基金', 'HOLD',"
                " '{\"sector\": \"电源设备\", \"momentum_20d\": 7.6}')"
            )
        rows = decision_mod.get_holding_codes(("HOLD",))
        assert len(rows) == 1
        assert rows[0][4] == "电源设备"

    def test_sector_fallback_to_rbsa1(self, monkeypatch, tmp_path):
        """旧推荐无 feature_snapshot.sector → 回退 RBSA 第一行业。"""
        decision_mod = self._setup(monkeypatch, tmp_path)
        with decision_mod.db() as conn:
            conn.execute(
                "INSERT INTO recommend_log (recommend_date, code, name, status)"
                " VALUES ('2026-08-03', '018517', '测试基金', 'HOLD')"
            )
        rows = decision_mod.get_holding_codes(("HOLD",))
        assert len(rows) == 1
        assert rows[0][4] == "半导体"


class TestClearRecommendationsIncludesMacroNews:
    """清除推荐数据应一并清除每日宏观摘要（板块轮动/资金流向/AI赛道分析的数据源）。"""

    def test_clear_removes_macro_news(self, monkeypatch, tmp_path):
        import app.database as db_mod
        monkeypatch.setattr(db_mod, "DB_PATH", str(tmp_path / "test.db"))
        from app.repo import decision as decision_mod
        with decision_mod.db() as conn:
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
        with decision_mod.db() as conn:
            assert conn.execute("SELECT COUNT(*) FROM macro_news").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM recommend_log").fetchone()[0] == 0

    def test_count_includes_macro_news(self, monkeypatch, tmp_path):
        import app.database as db_mod
        monkeypatch.setattr(db_mod, "DB_PATH", str(tmp_path / "test.db"))
        from app.repo import decision as decision_mod
        with decision_mod.db() as conn:
            conn.execute(
                "INSERT INTO macro_news (date, news_summary)"
                " VALUES ('2026-08-03', '新闻')"
            )
        counts = decision_mod.count_recommendation_domain()
        assert counts["macro_news"] == 1

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
        monkeypatch.setattr(db_mod._migrate, "_done", False)
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "test.db")

        conn = get_db()
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        finally:
            conn.close()

        assert "quality_metrics" in tables
        assert "empty_recommendations" in tables

"""架构深化 D：数据基座步骤编排（foundation.daily_steps）单元测试。

回归根因：步骤选择（pipeline._daily_data_steps）与执行（foundation.run_pipeline）
跨 module 分裂，靠魔法键与两套互不一致的步骤编号耦合（{1,2,3,4,6,7,8} vs
[1,2,3,4,7]）；现收敛为 foundation 单一来源（含步骤语义常量）。
"""

import sqlite3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime, timedelta

import app.database as db_mod
import app.data.foundation as fd


def _seed_meta(monkeypatch, tmp_path, holdings_last_run: str | None):
    """临时库 + meta 表 + holdings_last_run 记录。"""
    db_path = tmp_path / "steps.db"
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    if holdings_last_run is not None:
        conn.execute("INSERT INTO meta VALUES ('holdings_last_run', ?)", (holdings_last_run,))
    conn.commit()
    conn.close()


class TestDailySteps:
    def test_no_record_trigger_step4(self, monkeypatch, tmp_path):
        """首次部署无记录 → 视为到期，触发 Step 4（自举）。"""
        _seed_meta(monkeypatch, tmp_path, None)
        assert fd.daily_steps() == [1, 2, 3, 4, 7]

    def test_interval_expired_trigger_step4(self, monkeypatch, tmp_path):
        """距上次持仓 >7 天 → 追加 Step 4。"""
        old = (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d")
        _seed_meta(monkeypatch, tmp_path, old)
        assert fd.daily_steps() == [1, 2, 3, 4, 7]

    def test_interval_fresh_skip_step4(self, monkeypatch, tmp_path):
        """距上次持仓 <=7 天 → 仅基础步骤（Step 4 不重跑）。"""
        recent = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        _seed_meta(monkeypatch, tmp_path, recent)
        assert fd.daily_steps() == [1, 2, 3, 7]

    def test_corrupt_date_treated_as_expired(self, monkeypatch, tmp_path):
        """记录日期非法 → 按到期处理（触发 Step 4 自愈）。"""
        _seed_meta(monkeypatch, tmp_path, "not-a-date")
        assert fd.daily_steps() == [1, 2, 3, 4, 7]

    def test_step_semantics_single_source(self):
        """步骤语义常量与 run_pipeline 执行分支一致（编号单一来源）。"""
        assert fd._STEP_HOLDINGS == 4
        assert fd._STEP_FEATURES == 7
        assert fd.ALL_STEPS == frozenset({1, 2, 3, 4, 6, 7, 8})

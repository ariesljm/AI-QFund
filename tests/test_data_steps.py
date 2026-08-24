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


class TestUpdateFundListWeekly:
    """空列表守卫：源解析失败返回空时拒绝落库（防全表清空 + 周更不自愈）。"""

    def test_empty_list_skips_save_and_timestamp(self, monkeypatch, tmp_path):
        """fetch 返回空 → 不调 save_fund_list、不置位周更时间戳。"""
        _seed_meta(monkeypatch, tmp_path, None)
        saved = {"called": False}
        monkeypatch.setattr(fd, "fetch_fund_list", lambda: [])
        monkeypatch.setattr(fd, "save_fund_list",
                            lambda funds: saved.update(called=True) or 0)
        n = fd.update_fund_list_weekly()
        assert n == 0
        assert not saved["called"]          # 空列表绝不清空 fund_basic
        import sqlite3 as _s3
        conn = _s3.connect(str(tmp_path / "steps.db"))
        ts = conn.execute(
            "SELECT value FROM meta WHERE key = 'fund_list_last_update'").fetchone()
        conn.close()
        assert ts is None                    # 不置位 → 下次运行自动重试

    def test_nonempty_list_saves_and_marks(self, monkeypatch, tmp_path):
        """正常列表 → 落库并置位周更时间戳。"""
        _seed_meta(monkeypatch, tmp_path, None)
        monkeypatch.setattr(fd, "fetch_fund_list",
                            lambda: [{"code": "000001", "name": "测试", "type": "股票型", "is_buyable": 1}])
        monkeypatch.setattr(fd, "save_fund_list", lambda funds: len(funds))
        n = fd.update_fund_list_weekly()
        assert n == 1
        import sqlite3 as _s3
        conn = _s3.connect(str(tmp_path / "steps.db"))
        ts = conn.execute(
            "SELECT value FROM meta WHERE key = 'fund_list_last_update'").fetchone()
        conn.close()
        assert ts is not None

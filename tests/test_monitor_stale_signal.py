"""C5 净值陈旧语义分离测试：数据告警事件（is_stale）不进 WARNING 升级序列。

修复：净值陈旧（停牌/数据断裂）曾写入 WARNING 且计入 20 日升级链，
数据停更 20 个监控日会被误升级为离场——数据问题被伪装成持仓信号问题。
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.database as db_mod
from app.database import db_conn
import app.repo as repo


class TestStaleEventIsolation:
    """is_stale 事件与信号语义分离。"""

    def _seed_holding(self):
        with db_conn() as conn:
            conn.execute("INSERT INTO recommend_log (recommend_date, code, name, status) "
                         "VALUES ('2026-08-01', 'A', '甲', 'HOLD')")
            lid = conn.execute("SELECT id FROM recommend_log ORDER BY id DESC LIMIT 1").fetchone()[0]
        return lid

    def test_stale_event_excluded_from_upgrade_series(self, monkeypatch, tmp_path):
        """净值陈旧事件标记 is_stale=1，升级序列查询排除它。"""
        monkeypatch.setattr(db_mod, "DB_PATH", str(tmp_path / "test.db"))
        lid = self._seed_holding()
        # 正常防线 WARNING（赛道优势丧失）
        repo.insert_monitor_event("A", "2026-08-02", "WARNING", False, False, True,
                                  "维持", False, False, "赛道优势丧失", lid)
        # 净值陈旧数据告警
        repo.insert_monitor_event("A", "2026-08-03", "WARNING", False, False, False,
                                  "维持", False, False, "净值陈旧: 最新净值 2026-07-20 落后最近交易日", lid,
                                  is_stale=True)
        signals = repo.get_recent_monitor_signals("A", 10)
        assert signals == [("2026-08-02", "WARNING")]  # stale 事件被排除

    def test_latest_event_exposes_is_stale(self, monkeypatch, tmp_path):
        """web 展示侧：最新事件带 is_stale 标记，供「数据告警」样式区分。"""
        monkeypatch.setattr(db_mod, "DB_PATH", str(tmp_path / "test.db"))
        lid = self._seed_holding()
        repo.insert_monitor_event("A", "2026-08-03", "WARNING", False, False, False,
                                  "维持", False, False, "净值陈旧", lid, is_stale=True)
        ev = repo.get_latest_monitor_event("A")
        assert ev["is_stale"] == 1 and ev["signal"] == "WARNING"

    def test_normal_event_is_stale_zero(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db_mod, "DB_PATH", str(tmp_path / "test.db"))
        lid = self._seed_holding()
        repo.insert_monitor_event("A", "2026-08-02", "HOLD", False, False, False,
                                  "维持", False, False, "正常持有", lid)
        ev = repo.get_latest_monitor_event("A")
        assert ev["is_stale"] == 0


class TestMonitorStalePath:
    """run_monitor 净值陈旧分支：写数据告警事件，不改持仓状态。"""

    def _seed_holding(self):
        with db_conn() as conn:
            conn.execute("INSERT INTO recommend_log (recommend_date, code, name, status) "
                         "VALUES ('2026-08-01', 'A', '甲', 'HOLD')")
            return conn.execute("SELECT id FROM recommend_log ORDER BY id DESC LIMIT 1").fetchone()[0]

    def test_stale_writes_event_not_status(self, monkeypatch, tmp_path):
        import app.engine.monitor as mon
        monkeypatch.setattr(db_mod, "DB_PATH", str(tmp_path / "test.db"))
        lid = self._seed_holding()

        inserted = []
        monkeypatch.setattr(mon, "get_holding_codes",
                            lambda s: [{"code": "A", "name": "甲", "reco_date": "2026-08-01",
                                        "buy_reason": "逻辑", "sector": "半导体"}])
        monkeypatch.setattr(mon, "_check_nav_freshness", lambda code, dates: (True, "净值陈旧: 数据断裂"))
        monkeypatch.setattr(mon, "insert_monitor_event",
                            lambda *a, **k: inserted.append((a, k)))
        monkeypatch.setattr(mon, "get_holding_log_id", lambda code, s: lid)
        # 若旧逻辑调用 _update_signal 改状态 → 应失败（不应改持仓状态）
        monkeypatch.setattr(mon, "_update_signal",
                            lambda code, signal: (_ for _ in ()).throw(AssertionError("陈旧不应改变持仓状态")))
        monkeypatch.setattr(mon, "get_index_rows", lambda code: [])
        mon.run_monitor()
        assert len(inserted) == 1
        assert inserted[0][1].get("is_stale") is True

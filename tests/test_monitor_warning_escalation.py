"""WARNING 升级状态机测试：连续 N 个监控日未缓解 → 升级 EXIT。

回归：_warning_escalate 原实现要求"最近 20 条事件全为 WARNING"，而当日信号
尚未落库——实际需要前 20 天 + 当天共 21 个监控日才升级，比设计口径晚一日。
修复后：此前 N-1=19 个监控日全为 WARNING，加上当日即满足"连续 20 日未缓解"。
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.database as db_mod
from app.database import db_conn
import app.engine.monitor as mon
import app.repo as repo


class TestWarningEscalationWindow:
    """升级窗口 = 此前 19 个监控日全 WARNING + 当日。"""

    def _seed_holding(self):
        with db_conn() as conn:
            conn.execute("INSERT INTO recommend_log (recommend_date, code, name, status) "
                         "VALUES ('2026-06-01', 'A', '甲', 'HOLD')")
            return conn.execute("SELECT id FROM recommend_log ORDER BY id DESC LIMIT 1").fetchone()[0]

    @staticmethod
    def _seed_signals(lid: int, signals: list[str]) -> None:
        for i, sig in enumerate(signals):
            repo.insert_monitor_event("A", f"2026-07-{i + 1:02d}", sig,
                                      False, False, False, "维持",
                                      False, False, f"事件{i}", lid)

    def _setup(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db_mod, "DB_PATH", str(tmp_path / "test.db"))
        return self._seed_holding()

    def test_nineteen_prior_warnings_escalates(self, monkeypatch, tmp_path):
        """此前 19 个监控日全 WARNING（+当日）→ 升级。"""
        lid = self._setup(monkeypatch, tmp_path)
        self._seed_signals(lid, ["WARNING"] * 19)
        assert mon._warning_escalate("A") is True

    def test_eighteen_prior_warnings_does_not(self, monkeypatch, tmp_path):
        """此前仅 18 个监控日 WARNING → 窗口不足，不升级。"""
        lid = self._setup(monkeypatch, tmp_path)
        self._seed_signals(lid, ["WARNING"] * 18)
        assert mon._warning_escalate("A") is False

    def test_any_non_warning_breaks_streak(self, monkeypatch, tmp_path):
        """序列内任一非 WARNING（HOLD 缓解）→ 连续性中断，不升级。"""
        lid = self._setup(monkeypatch, tmp_path)
        signals = ["WARNING"] * 19
        signals[10] = "HOLD"
        self._seed_signals(lid, signals)
        assert mon._warning_escalate("A") is False

"""票 15 决策周期入口：状态机落库（种子数据构造触发条件 → 断言状态转移）。

apply_transition：读当前 → transition → 落库（EXIT 不可逆、信号快照可追溯）。
用种子基金走 HOLD → WATCH（脱轨）→ EXIT（跌破 EMA20）→ 不可逆全链。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.database as db_mod
from app.engine.state_machine import apply_transition
from app.repo.decision import get_tracked_state


def _s(**kw):
    base = {"drifted": False, "valuation_pctile": 50.0, "alpha_neg_days": 0,
            "below_ema20": False, "momentum_pos": True, "fatal_news": False}
    base.update(kw)
    return base


class TestApplyTransition:
    def test_full_chain_and_irreversibility(self, monkeypatch, tmp_path):
        """HOLD → WATCH（脱轨）→ EXIT（跌破 EMA20）→ 不可逆。"""
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "sm.db")
        # 无记录 → 从 HOLD 起算，无信号 → HOLD
        old, new = apply_transition("recommendation", "F001", _s(), "2026-09-10")
        assert (old, new) == ("HOLD", "HOLD")
        # 脱轨 → WATCH
        old, new = apply_transition("recommendation", "F001", _s(drifted=True), "2026-09-11")
        assert (old, new) == ("HOLD", "WATCH")
        # 跌破 EMA20 → EXIT
        old, new = apply_transition("recommendation", "F001", _s(below_ema20=True), "2026-09-12")
        assert (old, new) == ("WATCH", "EXIT")
        # EXIT 不可逆：任何信号都回不去
        old, new = apply_transition("recommendation", "F001", _s(), "2026-09-13")
        assert (old, new) == ("EXIT", "EXIT")

    def test_persisted_with_signal_snapshot(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "sm2.db")
        apply_transition("recommendation", "F002", _s(drifted=True,
                                                      valuation_pctile=90.0), "2026-09-11")
        st = get_tracked_state("recommendation", "F002")
        assert st["state"] == "WATCH" and st["date"] == "2026-09-11"
        assert '"drifted": true' in st["signals_json"]   # 信号快照可追溯

    def test_watch_fix_returns_hold(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "sm3.db")
        apply_transition("recommendation", "F003", _s(drifted=True), "2026-09-11")
        old, new = apply_transition("recommendation", "F003", _s(), "2026-09-15")
        assert (old, new) == ("WATCH", "HOLD")

    def test_object_type_seam(self, monkeypatch, tmp_path):
        """ADR-0011 留缝：不同 object_type 互不干扰（将来用户持仓加行即可）。"""
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "sm4.db")
        apply_transition("recommendation", "R1", _s(drifted=True), "2026-09-11")
        apply_transition("user_position", "U1", _s(), "2026-09-11")
        assert get_tracked_state("recommendation", "R1")["state"] == "WATCH"
        assert get_tracked_state("user_position", "U1")["state"] == "HOLD"

"""进化结算逻辑测试（Q2/Q3 共识落地）。

- 结算查全部待定（幂等防跨月遗漏）；
- 非 EXIT 持仓满 20 交易日按 20 日净值定标（与质量度量同口径），窗口未满保持待定；
- 标签阈值与 quality 对齐（PROFIT_THRESHOLD=1%）：胜 > 1%、负 ≤ 1%，不再产生"平"；
- run_evolve 默认进化上个月。
"""

import sys
from pathlib import Path
from datetime import datetime
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.engine import evolve
from app import domain


def _make_navs(n_start: float = 1.0, n: int = 21, end: float = 1.02) -> list[tuple]:
    """21 条净值序列：(date, cum_nav) 升序，第 0 条 n_start、第 20 条 end。"""
    rows = []
    for i in range(n):
        v = end if i == n - 1 else n_start
        rows.append((f"2026-06-{i + 1:02d}", v))
    return rows


def _P(ss_id, log_id, used_ids=None):
    """待定记录 3 元组 (id, recommend_log_id, used_insight_ids_json)。"""
    return (ss_id, log_id, None if used_ids is None else __import__("json").dumps(used_ids))


class TestSettleOutcomes:
    """月度结算：全部待定 + 非 EXIT 满窗口按 20 日净值定标。"""

    def _setup(self, monkeypatch, pending, by_id, navs_map=None):
        calls = []
        conf_calls = []
        monkeypatch.setattr(evolve.repo, "get_pending_sector_selections", lambda: pending)
        monkeypatch.setattr(evolve.repo, "get_recommendation_by_id", lambda log_id: by_id.get(log_id))
        if navs_map is not None:
            monkeypatch.setattr(evolve.repo.nav, "series",
                                lambda code, since=None, limit=None: navs_map.get(code, []))
        monkeypatch.setattr(evolve.repo, "update_sector_selection_outcome",
                            lambda ss_id, outcome, date, note, pool_outcomes=None: calls.append((ss_id, outcome, date, note)))
        monkeypatch.setattr(evolve.repo, "adjust_insight_confidence",
                            lambda iid, delta: conf_calls.append((iid, delta)))
        return calls, conf_calls

    def test_exit_above_threshold_win(self, monkeypatch):
        """EXIT 且退出收益 > 1% → 胜。"""
        calls, conf = self._setup(monkeypatch,
                                  pending=[_P(1, 10)],
                                  by_id={10: ("004936", domain.SIGNAL_EXIT, 0.03, "2026-06-10", 1.0)})
        assert evolve._settle_outcomes() == 1
        ss_id, outcome, date, note = calls[0]
        assert outcome == "胜" and "退出时收益" in note
        assert conf == []  # 无 used_insight_ids 不调权

    def test_exit_small_gain_is_loss(self, monkeypatch):
        """EXIT 但退出收益 +0.5% ≤ 1% → 负（阈值对齐 quality，1% 以下不算赚钱）。"""
        calls, conf = self._setup(monkeypatch,
                                  pending=[_P(2, 11)],
                                  by_id={11: ("004936", domain.SIGNAL_EXIT, 0.005, "2026-06-11", 1.0)})
        assert evolve._settle_outcomes() == 1
        assert calls[0][1] == "负"

    def test_exit_no_return_keeps_pending(self, monkeypatch):
        """EXIT 但无收益数据（入场净值缺失）→ 保持待定不结算。"""
        calls, conf = self._setup(monkeypatch,
                                  pending=[_P(3, 12)],
                                  by_id={12: ("004936", domain.SIGNAL_EXIT, None, "2026-06-12", None)})
        assert evolve._settle_outcomes() == 0
        assert calls == []

    def test_hold_full_window_settles_by_nav(self, monkeypatch):
        """非 EXIT 满 21 条净值 → 按第 20 条/入场净值定标（胜），note 为 20 日收益。"""
        calls, conf = self._setup(monkeypatch,
                                  pending=[_P(4, 13)],
                                  by_id={13: ("015412", domain.SIGNAL_HOLD, None, "2026-06-13", 1.0)},
                                  navs_map={"015412": _make_navs(n_start=1.0, end=1.03)})
        assert evolve._settle_outcomes() == 1
        ss_id, outcome, date, note = calls[0]
        assert outcome == "胜" and "20日收益" in note and "3.00%" in note

    def test_hold_full_window_loss(self, monkeypatch):
        """非 EXIT 满窗口但 20 日收益 ≤ 1% → 负。"""
        calls, conf = self._setup(monkeypatch,
                                  pending=[_P(5, 14)],
                                  by_id={14: ("015412", domain.SIGNAL_WARNING, None, "2026-06-14", 1.0)},
                                  navs_map={"015412": _make_navs(n_start=1.0, end=0.98)})
        assert evolve._settle_outcomes() == 1
        assert calls[0][1] == "负" and "20日收益" in calls[0][3]

    def test_hold_unfinished_window_keeps_pending(self, monkeypatch):
        """非 EXIT 净值不足 21 条（窗口未满）→ 保持待定，下月补齐。"""
        calls, conf = self._setup(monkeypatch,
                                  pending=[_P(6, 15)],
                                  by_id={15: ("004936", domain.SIGNAL_HOLD, None, "2026-07-28", 1.0)},
                                  navs_map={"004936": _make_navs(n=10)})
        assert evolve._settle_outcomes() == 0
        assert calls == []

    def test_all_pending_across_months(self, monkeypatch):
        """全部待定（跨月遗留）都被结算，不按月过滤（幂等防漏）。"""
        calls, conf = self._setup(monkeypatch,
                                  pending=[_P(1, 10), _P(2, 11), _P(7, 17)],
                                  by_id={
                                      10: ("A", domain.SIGNAL_EXIT, 0.03, "2026-05-10", 1.0),
                                      11: ("B", domain.SIGNAL_HOLD, None, "2026-06-11", 1.0),
                                      17: ("C", domain.SIGNAL_HOLD, None, "2026-07-17", 1.0),
                                  },
                                  navs_map={
                                      "B": _make_navs(end=1.04),
                                      "C": _make_navs(end=1.01),
                                  })
        assert evolve._settle_outcomes() == 3
        assert [c[1] for c in calls] == ["胜", "胜", "胜"]

    def test_settle_rewards_win_insights(self, monkeypatch):
        """结算为胜 → 该赛道用过的 sector 洞察 confidence +0.10（Q4 反馈回路）。"""
        calls, conf = self._setup(monkeypatch,
                                  pending=[_P(8, 18, used_ids=[101, 102])],
                                  by_id={18: ("A", domain.SIGNAL_EXIT, 0.03, "2026-06-18", 1.0)})
        assert evolve._settle_outcomes() == 1
        assert calls[0][1] == "胜"
        assert conf == [(101, 0.10), (102, 0.10)]

    def test_settle_punishes_loss_insights(self, monkeypatch):
        """结算为负 → 该赛道用过的 sector 洞察 confidence -0.10。"""
        calls, conf = self._setup(monkeypatch,
                                  pending=[_P(9, 19, used_ids=[103])],
                                  by_id={19: ("A", domain.SIGNAL_HOLD, None, "2026-06-19", 1.0)},
                                  navs_map={"A": _make_navs(n_start=1.0, end=0.97)})
        assert evolve._settle_outcomes() == 1
        assert calls[0][1] == "负"
        assert conf == [(103, -0.10)]


class TestDefaultEvolveMonth:
    """run_evolve 默认进化上个月（1 号自动触发 = 进化上月数据）。"""

    def test_defaults_to_last_month(self):
        assert evolve._default_evolve_month(datetime(2026, 8, 1)) == "2026-07"
        assert evolve._default_evolve_month(datetime(2026, 8, 15)) == "2026-07"
        assert evolve._default_evolve_month(datetime(2026, 3, 31)) == "2026-02"
        assert evolve._default_evolve_month(datetime(2026, 1, 1)) == "2025-12"

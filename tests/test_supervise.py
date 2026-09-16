"""US22-28 每日监控接线（票 15 状态机 × 2.0 推荐对象）纯函数与装配。"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.database as db_mod
from app.engine.supervise import (
    alpha_neg_streak,
    build_signals,
    daily_returns,
    ema20_below,
    momentum_pos,
    run_supervision,
    valuation_high,
)


def _navs(*vals: float) -> list[float]:
    return list(vals)


class TestPureSignals:
    def test_ema20_below_downward_trend(self):
        navs = _navs(*[100.0 - i * 0.9 for i in range(60)])
        assert ema20_below(navs) is True

    def test_ema20_below_uptrend_false(self):
        navs = _navs(*[100.0 + i * 0.9 for i in range(60)])
        assert ema20_below(navs) is False

    def test_ema20_insufficient_length(self):
        assert ema20_below(_navs(*[100.0] * 10)) is False

    def test_daily_returns_first_zero(self):
        r = daily_returns([10.0, 11.0, 11.0])
        assert r[0] == 0.0
        assert round(r[1], 6) == 0.1
        assert r[2] == 0.0

    def test_alpha_neg_streak_counts_recent_only(self):
        f = [100.0, 101.0, 101.5, 101.5, 101.0, 100.0]   # 后段走弱
        b = [100.0] * 6                                  # 基准走平
        assert alpha_neg_streak(f, b) == 2               # 末 2 日超额为负

    def test_alpha_streak_breaks_on_positive_excess(self):
        f = [100.0, 103.0, 103.0, 103.0, 103.0, 103.0, 103.0]
        b = [100.0] * 7                                  # 基准走平，末日超额为正 → streak 0
        assert alpha_neg_streak(f, b) == 0

    def test_momentum_pos(self):
        assert momentum_pos([100.0, 101.0, 102.0, 103.0, 104.0, 106.0, 107.0]) is True
        assert momentum_pos([110.0, 109.0, 108.0, 107.0, 106.0, 105.0, 104.0]) is False

    def test_valuation_high_threshold(self):
        assert valuation_high(86.0) is True
        assert valuation_high(84.0) is False
        assert valuation_high(None) is False


class TestBuildSignals:
    def test_missing_data_safe_defaults(self):
        s = build_signals("F1", None, None)
        assert s["below_ema20"] is False
        assert "valuation_pctile" not in s
        assert s.get("fatal_news") is False

    def test_insufficient_navs_no_signals(self):
        s = build_signals("F1", [100.0] * 10, [100.0] * 10)
        assert s["below_ema20"] is False
        assert s.get("alpha_neg_days") == 0

    def test_fatal_news_passthrough(self):
        s = build_signals("F1", None, None, fatal_news=True)
        assert s["fatal_news"] is True


class TestRunSupervision:
    def _seed(self, monkeypatch, tmp_path):
        from datetime import date, timedelta
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "supervise.db")
        import app.repo.base as base
        from app.repo import decision

        def _series(code, since=None, until=None):
            if code == "F000001":
                # 下行趋势：EMA20 跌破 + 相对基准连续负超额
                return [(f"2026-0{(i // 20) + 1:02d}-{(i % 20) + 1:02d}", 100.0 - i * 0.8)
                        for i in range(60)]
            return None

        def _index(code="sh000300"):
            # 基准走平 → 基金下行即负超额
            return [("2026-01-0%02d" % (i + 1), 100.0) for i in range(60)]

        monkeypatch.setattr("app.repo.nav.series", _series)
        monkeypatch.setattr(base, "get_index_rows", _index)
        monkeypatch.setattr(base, "get_holdings", lambda code, limit=10: [])
        monkeypatch.setattr(base, "get_pe_histories", lambda codes, days=750: {})
        monkeypatch.setattr("app.engine.supervise._pe_pctile", lambda code, limit=10: None)
        with db_mod.db_conn() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS recommend_v2 (date TEXT, code TEXT, "
                "final_score REAL, audit_json TEXT, PRIMARY KEY (date, code))")
            conn.execute(
                "INSERT INTO recommend_v2 VALUES ('2026-09-16', 'F000001', 0.5, '{}')")
            conn.commit()

    def test_run_supervision_tracks_and_persists(self, monkeypatch, tmp_path):
        self._seed(monkeypatch, tmp_path)
        r = run_supervision("2026-09-16")
        assert r["tracked"] == 1
        assert not r["empty"]
        # 下行 + 基准平 → 至少触发观察（估值/Alpha 任一）或保持 HOLD；落库可见
        from app.repo.decision import get_tracked_state
        st = get_tracked_state("fund", "F000001")
        assert st is not None
        assert st["state"] in ("HOLD", "WATCH")

    def test_run_supervision_empty_pool(self, monkeypatch, tmp_path):
        self._seed(monkeypatch, tmp_path)
        from app.repo import decision
        with db_mod.db_conn() as conn:
            conn.execute("DELETE FROM recommend_v2")
            conn.commit()
        r = run_supervision("2026-09-16")
        assert r["empty"] is True
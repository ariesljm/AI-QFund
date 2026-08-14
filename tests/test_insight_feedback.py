"""洞察反馈回路测试（Q4/Q6/Q7 前半落地）。

- get_active_insights 过滤 ranking 诊断（不混入定论 prompt 的"历史教训"）；
- mark_insights_applied：apply_count+1 / last_applied_date 更新；
- adjust_insight_confidence：clamp [0,1]；
- _collect_cases 超上限按三类比例抽样（每类保底 1 条）；
- insert_sector_selection 持久化 used_insight_ids（结算调权关联数据源）。
（Q6 整月收集函数 get_monthly_cases 已随架构深化 J 删除——自动元分析走增量游标。）
"""

import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.engine import evolve
import app.repo as repo
import app.database as db_mod


def _init_db(monkeypatch, tmp_path):
    """把 DB_PATH 指向临时库并返回 db_conn（初始化 schema + 迁移）。"""
    monkeypatch.setattr(db_mod, "DB_PATH", str(tmp_path / "test.db"))
    from app.database import db_conn
    return db_conn


class TestActiveInsightsFilter:
    """Q7：ranking 诊断不混入定论 prompt 教训。"""

    def test_ranking_excluded(self, monkeypatch, tmp_path):
        db_conn = _init_db(monkeypatch, tmp_path)
        with db_conn() as conn:
            cur = conn.execute("INSERT INTO evolution_insights (insight, insight_type, created_date, active) VALUES ('赛道教训A', 'sector', '2026-07-01', 1)")
            sector_id = cur.lastrowid
            cur = conn.execute("INSERT INTO evolution_insights (insight, insight_type, created_date, active) VALUES ('排分自纠偏诊断', 'ranking', '2026-07-01', 1)")
            ranking_id = cur.lastrowid
        ids = [i for i, _ in repo.get_active_insights(8)]
        assert sector_id in ids and ranking_id not in ids

    def test_sector_insights_return_ids(self, monkeypatch, tmp_path):
        db_conn = _init_db(monkeypatch, tmp_path)
        with db_conn() as conn:
            cur = conn.execute("INSERT INTO evolution_insights (insight, insight_type, created_date, active) VALUES ('赛道教训A', 'sector', '2026-07-01', 1)")
            iid = cur.lastrowid
        rows = repo.get_sector_insights(5)
        assert rows == [(iid, "赛道教训A")]


class TestMarkInsightsApplied:
    """Q4：进入 prompt 即 apply_count+1、last_applied_date 更新。"""

    def test_mark_applied_accumulates(self, monkeypatch, tmp_path):
        db_conn = _init_db(monkeypatch, tmp_path)
        with db_conn() as conn:
            cur = conn.execute("INSERT INTO evolution_insights (insight, insight_type, created_date, active) VALUES ('赛道教训A', 'sector', '2026-07-01', 1)")
            iid = cur.lastrowid
        repo.mark_insights_applied([iid], "2026-08-01")
        with db_conn() as conn:
            row = conn.execute("SELECT apply_count, last_applied_date FROM evolution_insights WHERE id=?", (iid,)).fetchone()
        assert row == (1, "2026-08-01")
        repo.mark_insights_applied([iid], "2026-08-02")
        with db_conn() as conn:
            row = conn.execute("SELECT apply_count, last_applied_date FROM evolution_insights WHERE id=?", (iid,)).fetchone()
        assert row == (2, "2026-08-02")

    def test_empty_ids_noop(self, monkeypatch, tmp_path):
        _init_db(monkeypatch, tmp_path)
        repo.mark_insights_applied([], "2026-08-01")  # 不应抛错


class TestAdjustInsightConfidence:
    """Q4：结算结果调权 clamp [0,1]。"""

    def _insert(self, db_conn, confidence):
        with db_conn() as conn:
            cur = conn.execute("INSERT INTO evolution_insights (insight, insight_type, created_date, active, confidence) VALUES ('赛道教训A', 'sector', '2026-07-01', 1, ?)", (confidence,))
            return cur.lastrowid

    def test_adjust_clamp_upper(self, monkeypatch, tmp_path):
        db_conn = _init_db(monkeypatch, tmp_path)
        iid = self._insert(db_conn, 0.98)
        repo.adjust_insight_confidence(iid, 0.05)
        with db_conn() as conn:
            conf = conn.execute("SELECT confidence FROM evolution_insights WHERE id=?", (iid,)).fetchone()[0]
        assert conf == 1.0  # clamp 上限

    def test_adjust_clamp_lower(self, monkeypatch, tmp_path):
        db_conn = _init_db(monkeypatch, tmp_path)
        iid = self._insert(db_conn, 0.02)
        repo.adjust_insight_confidence(iid, -0.05)
        with db_conn() as conn:
            conf = conn.execute("SELECT confidence FROM evolution_insights WHERE id=?", (iid,)).fetchone()[0]
        assert conf == 0.0  # clamp 下限


def _case_row(outcome: str, i: int) -> dict:
    """构造已结算案例的结构化行（键=列名，与 get_settled_cases_after 同构）。"""
    return {"id": i, "recommend_log_id": 1000 + i, "recommended_sectors": '["光伏"]',
            "sector_reasoning": "reasoning", "regime_label": "BULL", "outcome": outcome,
            "outcome_note": "note", "buy_reason": "buy_reason", "code": "000001",
            "name": "基金A", "signal": "HOLD", "trigger_trailing": 0, "trigger_drift": 0,
            "trigger_sector_adv": 0, "logic_verdict": "维持", "sector_risk": 0,
            "holding_risk": 0, "detail": "detail"}


class TestCollectCasesSampling:
    """Q6：超上限按三类比例抽样（每类保底 1 条）。增量收集：id > 游标 的已结算案例。"""

    def test_sampling_proportional(self, monkeypatch):
        rows = [_case_row("胜", i) for i in range(30)] \
             + [_case_row("负", i) for i in range(30, 45)] \
             + [_case_row("平", i) for i in range(45, 50)]
        monkeypatch.setattr(evolve.repo, "get_settled_cases_after", lambda ss_id: rows)
        successes, failures, neutrals = evolve._collect_cases(0)
        # 50 条 > 40 → 按比例抽 24/12/4 = 40（round(类数*40/50)）
        assert len(successes) == 24 and len(failures) == 12 and len(neutrals) == 4
        assert all(c["outcome"] == "胜" for c in successes)
        assert all(c["outcome"] == "负" for c in failures)
        assert all(c["outcome"] == "平" for c in neutrals)

    def test_below_limit_no_sampling(self, monkeypatch):
        rows = [_case_row("胜", i) for i in range(8)] + [_case_row("负", i) for i in range(8, 10)]
        monkeypatch.setattr(evolve.repo, "get_settled_cases_after", lambda ss_id: rows)
        successes, failures, neutrals = evolve._collect_cases(0)
        assert len(successes) == 8 and len(failures) == 2 and neutrals == []

    def test_cursor_excludes_processed(self, monkeypatch):
        """游标语义：id ≤ 游标的已结算案例不再收集（增量，避免重复分析）。"""
        rows = [_case_row("胜", i) for i in range(3, 6)]
        monkeypatch.setattr(evolve.repo, "get_settled_cases_after", lambda ss_id: rows)
        successes, failures, neutrals = evolve._collect_cases(3)
        assert [c["id"] for c in successes] == [3, 4, 5]
        monkeypatch.setattr(evolve.repo, "get_settled_cases_after", lambda ss_id: [r for r in rows if r["id"] > ss_id])
        assert evolve._collect_cases(5) == ([], [], [])


class TestSectorSelectionUsedInsights:
    """Q4：赛道选择落库 used_insight_ids，供月度结算调权关联。"""

    def test_persists_used_ids(self, monkeypatch, tmp_path):
        db_conn = _init_db(monkeypatch, tmp_path)
        repo.insert_sector_selection("2026-08-01", 1, ["光伏"], [], "reason", "BULL",
                                     used_insight_ids=[101, 102])
        with db_conn() as conn:
            row = conn.execute("SELECT used_insight_ids FROM sector_selections WHERE id=1").fetchone()
        assert json.loads(row[0]) == [101, 102]

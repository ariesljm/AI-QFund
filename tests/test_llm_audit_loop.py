"""T07（reco-hardening）：LLM 决策质量闭环验收测试。

背景（D2 用户决策）：AI 是时代优势，充分发挥——LLM 定论保留并强化；
但须以数据闭环度量其决策质量。本测试固化：否决结构化解析/统计、
审计报表构建、insert_recommendation 否决落库。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.repo import decision as dec
from backtest.llm_audit_report import build_veto_report, parse_vetoed


class TestParseVetoed:
    def test_parse_valid_json(self):
        rows = parse_vetoed('[{"code":"F1","name":"基金1","reason":"重仓股异常"},'
                            '{"code":"F2","name":"基金2","reason":"赛道归属可疑"}]')
        assert len(rows) == 2
        assert rows[0]["code"] == "F1"

    def test_empty_and_invalid(self):
        assert parse_vetoed("") == []
        assert parse_vetoed("not json") == []
        assert parse_vetoed('[{"name":"无代码"}]') == []  # 缺 code 过滤


class TestVetoReport:
    def test_no_cases_note(self):
        r = build_veto_report()
        if r["n_cases"] == 0:
            assert "暂无" in r["note"]

    def test_report_shape(self):
        r = build_veto_report()
        assert "n_cases" in r
        if r["n_cases"] == 0:
            assert "note" in r
        else:
            assert "avg_ret_40d_pct" in r
            assert 0 <= r["missed_rate_pct"] <= 100


class TestInsertRecommendationVetoed:
    def test_vetoed_stored_and_read(self, monkeypatch, tmp_path):
        """否决结构化落库 → get_vetoed_audit_rows 可读。"""
        import sqlite3



        db = tmp_path / "t7.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE recommend_log (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                     "recommend_date TEXT, code TEXT, name TEXT, rank INTEGER, score REAL, "
                     "combo REAL, regime TEXT, buy_reason TEXT, status TEXT, "
                     "feature_snapshot TEXT, entry_nav REAL, candidate_codes TEXT, "
                     "vetoed_json TEXT, reco_path TEXT DEFAULT 'sector', "
                     "decision_logic TEXT)")
        conn.commit()

        monkeypatch.setattr(dec, "db_conn", lambda: sqlite3.connect(db))
        dec.insert_recommendation(
            "2026-09-05", "F1", "基金1", 1, 0.05, 0.6, "BEAR", "理由",
            vetoed=[{"code": "F2", "name": "基金2", "reason": "重仓股异常"}])
        rows = dec.get_vetoed_audit_rows()
        assert len(rows) == 1
        assert rows[0][0] == "2026-09-05"
        vetoed = json.loads(rows[0][3])
        assert vetoed[0]["code"] == "F2"

    def test_no_vetoed_returns_empty(self, monkeypatch, tmp_path):
        import sqlite3



        db = tmp_path / "t7b.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE recommend_log (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                     "recommend_date TEXT, code TEXT, name TEXT, rank INTEGER, score REAL, "
                     "combo REAL, regime TEXT, buy_reason TEXT, status TEXT, "
                     "feature_snapshot TEXT, entry_nav REAL, candidate_codes TEXT, "
                     "vetoed_json TEXT, reco_path TEXT DEFAULT 'sector', "
                     "decision_logic TEXT)")
        conn.commit()
        monkeypatch.setattr(dec, "db_conn", lambda: sqlite3.connect(db))
        dec.insert_recommendation("2026-09-05", "F1", "基金1", 1, 0.05, 0.6, "BEAR", "理由")
        assert dec.get_vetoed_audit_rows() == []


class TestAuditSeamGuard:
    """ADR-0001 守卫：llm_audit 写经 decision seam，llm/client.py 无直连。"""

    def test_insert_llm_audit_through_seam(self, monkeypatch, tmp_path):
        """写读回环：insert_llm_audit → 可查（滚动保留/哈希由 seam 内部处理）。"""
        import sqlite3



        db = tmp_path / "audit.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE llm_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, "
                     "caller TEXT, prompt_hash TEXT, prompt_preview TEXT, raw_output TEXT, "
                     "parsed_result TEXT, duration_ms INTEGER, tokens INTEGER, ok INTEGER)")
        conn.commit()
        monkeypatch.setattr(dec, "db_conn", lambda: sqlite3.connect(db))

        dec.insert_llm_audit("test_caller", "sample prompt", "raw out", '{"a":1}', 12.5, 100, True)
        rows = conn.execute("SELECT caller, prompt_hash, ok FROM llm_audit").fetchall()
        assert rows == [("test_caller", "13:sample prompt", 1)]

    def test_llm_audit_only_via_seam(self):
        """守卫：llm/client.py 不再持有裸 llm_audit 写/连接（ADR-0001 写侧不变式）。"""
        src = Path(__file__).resolve().parent.parent / "app" / "llm" / "client.py"
        text = src.read_text(encoding="utf-8")
        assert "INSERT INTO llm_audit" not in text
        assert "db_conn" not in text

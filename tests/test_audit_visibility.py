"""票 23：审计可见性数据层（排雷过程可追溯）。

get_recent_audits：读取最近 LLM 审计（含 verdict/risk_score 解析），
供 Web 展示被 VETO 的基金与理由（非黑盒）。roundtrip + 解析测试。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.database as db_mod
from app.repo.decision import get_recent_audits, insert_llm_audit


class TestAuditVisibility:
    def test_insert_and_read(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "audit.db")
        parsed = json.dumps({"audit_verdict": "VETO", "risk_score": 85,
                             "veto_reasons": ["重仓股立案"]}, ensure_ascii=False)
        insert_llm_audit("screen_audit", "prompt text", "raw output", parsed,
                         120, 300, True)
        audits = get_recent_audits()
        assert len(audits) == 1
        a = audits[0]
        assert a["caller"] == "screen_audit" and a["ok"] is True
        assert a["verdict"] == "VETO" and a["risk_score"] == 85

    def test_latest_first_and_limit(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "audit2.db")
        for i in range(5):
            insert_llm_audit(f"caller{i}", "p", "r", None, 10, 0, True)
        audits = get_recent_audits(limit=3)
        assert len(audits) == 3
        assert audits[0]["caller"] == "caller4"   # 新→旧

    def test_invalid_parsed_degrades(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "audit3.db")
        insert_llm_audit("c", "p", "r", "not-json", 10, 0, False)
        audits = get_recent_audits()
        assert audits[0]["verdict"] is None and audits[0]["risk_score"] is None
        assert audits[0]["ok"] is False

    def test_empty(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "audit4.db")
        assert get_recent_audits() == []

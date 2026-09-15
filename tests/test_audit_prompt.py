"""票 13：模块二排雷审计 prompt（System/User 装配）。

覆盖：System 风控视角（VETO/四维度）；User 四组切片装配（缺失明示不脑补）、
JSON 输出约束与 app/llm/audit.py 的 validate_audit 契约一致。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.llm.audit import VALID_VERDICTS
from app.llm.prompts import audit_system_prompt, audit_user_prompt


class TestAuditSystemPrompt:
    def test_contains_veto_and_dimensions(self):
        p = audit_system_prompt()
        assert "VETO" in p
        for dim in ("风格漂移", "标的暴雷", "经理负荷", "流动性冲击"):
            assert dim in p
        assert "纯 JSON" in p

    def test_tough_stance(self):
        assert "极度挑剔" in audit_system_prompt()


class TestAuditUserPrompt:
    def _slices(self, **kw):
        base = {"holdings_change": "留存率 80%，新进宁德时代",
                "risk_radar": "重仓股无立案/预亏公告",
                "management": "经理任职 3 年，管理 5 只基金",
                "sentiment": "无集中争议"}
        base.update(kw)
        return base

    def test_assembly(self):
        p = audit_user_prompt({"code": "F001", "name": "测试基金", "quant_score": 0.85},
                              self._slices())
        assert "F001" in p and "测试基金" in p and "量化分: 0.85" in p
        assert "持仓异动" in p and "宁德时代" in p and "舆情争议" in p

    def test_missing_slice_explicit(self):
        """缺失切片明示（不得脑补）——票 13 验收。"""
        p = audit_user_prompt({"code": "F001", "name": "测试基金"},
                              self._slices(risk_radar=None))
        assert "【缺失——不得猜测" in p

    def test_json_contract_matches_validator(self):
        """prompt 约束的输出字段必须与 validate_audit 契约一致。"""
        p = audit_user_prompt({"code": "F001", "name": "x"}, self._slices())
        for v in VALID_VERDICTS:
            assert v in p                      # 枚举全部出现
        for field in ("audit_verdict", "risk_score", "veto_reasons",
                      "audit_details", "recommendation_summary"):
            assert field in p
        assert "0–100" in p and "50 字" in p

    def test_verdicts_must_be_exact(self):
        assert "PASS / CONDITIONAL_PASS / VETO" in audit_user_prompt(
            {"code": "F1", "name": "x"}, self._slices())

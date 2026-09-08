"""审计 P1-1：进化元分析健康度埋点。

闭环"学习-回流"此前空转不可见（生产实证：llm_audit 中 evolve 调用 0 条，无告警）。
修复：每次元分析记录 outcome 到 meta（ok/failed/no_cases + 原因/案例数/连续失败次数），
连续失败 >= 3 提升为 error 级告警。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.engine import evolve
from app.repo import meta_keys as META


class TestAnalysisOutcome:
    def test_ok_resets_streak(self, monkeypatch, caplog):
        """成功一次后连续失败计数复位；单次失败只 warning 不 error。"""
        saved: dict = {}
        monkeypatch.setattr(evolve.repo, "save_meta", lambda k, v: saved.update({k: v}))
        # 前置：已有 2 次连续失败
        monkeypatch.setattr(
            evolve.repo, "get_meta",
            lambda k: json.dumps({"status": "failed", "fail_streak": 2}) if k == META.LAST_ANALYSIS_OUTCOME else None)

        with caplog.at_level("ERROR"):
            evolve._record_analysis_outcome("ok", cases=10, added=3)
            # 成功 → fail_streak 归零
            assert json.loads(saved[META.LAST_ANALYSIS_OUTCOME])["fail_streak"] == 0
            assert not [r for r in caplog.records if r.levelno == 40]

    def test_three_consecutive_failures_raise_error(self, monkeypatch, caplog):
        """连续 3 次失败 → error 级告警（闭环空转可见性）。"""
        saved: dict = {}
        monkeypatch.setattr(evolve.repo, "save_meta", lambda k, v: saved.update({k: v}))
        monkeypatch.setattr(
            evolve.repo, "get_meta",
            lambda k: json.dumps({"status": "failed", "fail_streak": 2}) if k == META.LAST_ANALYSIS_OUTCOME else None)

        with caplog.at_level("ERROR"):
            evolve._record_analysis_outcome("failed", reason="LLM 不可用", cases=5)
        assert json.loads(saved[META.LAST_ANALYSIS_OUTCOME])["fail_streak"] == 3
        assert any("连续 3 次失败" in r.message for r in caplog.records if r.levelno == 40)

    def test_no_cases_is_not_failure(self, monkeypatch):
        """无新结算案例是正常状态，不算失败（不累计 fail_streak）。"""
        saved: dict = {}
        monkeypatch.setattr(evolve.repo, "save_meta", lambda k, v: saved.update({k: v}))
        monkeypatch.setattr(evolve.repo, "get_meta", lambda k: None)
        evolve._record_analysis_outcome("no_cases", "无新结算案例")
        out = json.loads(saved[META.LAST_ANALYSIS_OUTCOME])
        assert out["status"] == "no_cases"
        assert out["fail_streak"] == 0  # 正常状态，不累计失败
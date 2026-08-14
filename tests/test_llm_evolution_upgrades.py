"""P0/P1 优化项测试：R4 失败降级、洞察试用期、LLM 审计、影子对照、否决反事实。

覆盖 2026-08 评估报告建议的落地实现：
- P0-1：R4 逻辑证伪失败不再拖死规则层（复核层失败降级）
- P0-2：元分析新洞察以试用期置信度 0.5 起步；批次内近似洞察去重
- P0-3：LLM 调用统一写 llm_audit 审计（prompt 快照 + 原始输出 + 解析结果）
- P1-4：终选定论影子对照（LLM 终选 vs 量化 combo Top1）落库与结算
- P1-5：量化池内候选赛道反事实收益回填（否决正确率数据基础）
"""

import sys
import json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import app.database as db_mod
from app.database import db_conn
import app.repo as repo


# ── P0-1 R4 失败降级 ─────────────────────────────────────────

class TestR4Degradation:
    """LLM 逻辑证伪不可用/解析失败 → 跳过 R4，规则层信号照常。"""

    def test_logic_rule_skips_on_none(self):
        """r4_logic=None（LLM 失败，已预计算）→ 防线返回 None 且标记 r4_skipped。"""
        from app.engine.monitor import LogicVerificationRule, DefenseContext
        ctx = DefenseContext(code="F001", sector="半导体",
                             entry_snapshot={"rbsa_industry_1": "半导体"})
        ctx.r4_logic = None
        ctx.r4_precomputed = True  # 模拟并发预计算失败
        result = LogicVerificationRule().check(ctx)
        assert result is None
        assert ctx.r4_skipped is True

    def test_logic_rule_consumes_precomputed(self):
        """并发预计算结果被链阶段直接消费，不再重复调用 LLM。"""
        from app.engine.monitor import LogicVerificationRule, DefenseContext
        ctx = DefenseContext(code="F001", sector="半导体")
        ctx.r4_logic = {"logic_verdict": "断裂", "reason": "重仓退出",
                        "signal_hint": "HOLD", "sector_risk": False,
                        "holding_risk": True}
        ctx.r4_precomputed = True
        result = LogicVerificationRule().check(ctx)
        assert result is not None
        assert result.signal == "EXIT"
        assert "重仓退出" in result.reason  # reason = LLM逻辑证伪: <断裂原因>

    def test_check_logic_enhanced_returns_none_on_llm_error(self, monkeypatch):
        """LLM 技术失败（LLMError）→ _check_logic_enhanced 返回 None 而非 raise。"""
        from app.engine.monitor import _check_logic_enhanced, DefenseContext
        from app.llm.client import LLMError
        import app.engine.monitor as mon

        def _boom(prompt, temperature=0.1, max_tokens=16384, fallback=None,
                  validator=None, caller=""):
            raise LLMError("LLM 未配置")

        monkeypatch.setattr(mon, "call_llm_json", _boom)
        ctx = DefenseContext(code="F001", sector="半导体")
        assert _check_logic_enhanced(ctx) is None

    def test_check_logic_enhanced_returns_none_on_parse_fail(self, monkeypatch):
        """解析失败（validator 拒绝/返回 None）→ 返回 None，不 raise。"""
        from app.engine.monitor import _check_logic_enhanced, DefenseContext
        import app.engine.monitor as mon

        monkeypatch.setattr(mon, "call_llm_json",
                            lambda prompt, temperature=0.1, max_tokens=16384,
                                   fallback=None, validator=None, caller="": None)
        ctx = DefenseContext(code="F001", sector="半导体")
        assert _check_logic_enhanced(ctx) is None


# ── P0-2 洞察试用期与批次去重 ─────────────────────────────────

class TestInsightTrialPeriod:
    """元分析新洞察以 0.5 置信度起步（试用期），不再以满置信度 1.0 固化。"""

    def test_save_insight_uses_trial_confidence(self, monkeypatch):
        from app.engine import evolve
        inserted = {}
        monkeypatch.setattr(evolve.repo, "get_all_insights", lambda: [])
        monkeypatch.setattr(evolve.repo, "insert_insight",
                            lambda insight, itype, date, active, confidence=1.0,
                                   condition=None: inserted.update(
                                {"conf": confidence, "condition": condition}))
        ok = evolve._save_insight({"insight": "新教训", "type": "sector"})
        assert ok
        assert inserted["conf"] == evolve._INSIGHT_INITIAL_CONF == 0.5

    def test_save_insight_passes_condition(self, monkeypatch):
        """P3-11：结构化 condition 透传入库。"""
        from app.engine import evolve
        inserted = {}
        monkeypatch.setattr(evolve.repo, "get_all_insights", lambda: [])
        monkeypatch.setattr(evolve.repo, "insert_insight",
                            lambda insight, itype, date, active, confidence=1.0,
                                   condition=None: inserted.update(
                                {"conf": confidence, "condition": condition}))
        evolve._save_insight({"insight": "教训Y", "type": "sector",
                              "condition": "基金第一行业∈回避赛道"})
        assert inserted["condition"] == "基金第一行业∈回避赛道"

    def test_batch_dedup_keeps_one_of_similar(self, monkeypatch):
        """批次内近似洞察（Jaccard>0.5）只保留一条，防止同案例模式重复固化。"""
        from app.engine import evolve
        saved = []
        monkeypatch.setattr(evolve.repo, "get_meta", lambda k: None)
        monkeypatch.setattr(evolve.repo, "save_meta", lambda k, v: None)
        monkeypatch.setattr(evolve.repo, "get_settled_cases_after", lambda ss_id: [])
        monkeypatch.setattr(evolve, "_collect_cases",
                            lambda ss_id: ([], [], [{"id": 1, "outcome": "负"}]))
        monkeypatch.setattr(evolve, "_decision_loss_streak", lambda: 0)
        monkeypatch.setattr(evolve, "_batch_llm_analyze",
                            lambda *a, **k: [
                                {"insight": "回避赛道重合时不得推荐相关基金", "type": "sector"},
                                {"insight": "回避赛道重合的基金不得保留持仓并立即退出", "type": "sector"},
                                {"insight": "熊市单日领涨不构成买入信号", "type": "timing"},
                            ])
        monkeypatch.setattr(evolve.repo, "insert_insight",
                            lambda insight, itype, date, active, confidence=1.0,
                                   condition=None: saved.append(insight))
        evolve._run_meta_analysis({}, False)
        assert len(saved) == 2  # 前两条近似合并为一条 + 第三条独立


# ── P0-3 LLM 审计落库 ───────────────────────────────────────

class TestLlmAudit:
    """LLM 调用统一写 llm_audit：prompt 快照 + 原始输出 + 解析结果。"""

    def _install_fake(self, monkeypatch):
        import openai
        import app.llm.client as client_mod

        class _FakeMsg:
            def __init__(self, content):
                self.content = content

        class _FakeChoice:
            def __init__(self, content):
                self.message = _FakeMsg(content)

        class _FakeResp:
            def __init__(self, content):
                self.choices = [_FakeChoice(content)]
                self.usage = None

        class _FakeCompletions:
            def create(self, **kwargs):
                return _FakeResp('{"ok": true}')

        class _FakeChat:
            completions = _FakeCompletions()

        class _FakeOpenAI:
            def __init__(self, *a, **k):
                pass
            chat = _FakeChat()

        monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)
        monkeypatch.setattr(client_mod, "load_settings",
                            lambda: {"llm": {"api_key": "sk-test",
                                             "base_url": "http://x/v1", "model": "m"}})

    def test_call_llm_writes_audit(self, monkeypatch, tmp_path):
        import app.database as db_mod
        import app.llm.client as client_mod
        monkeypatch.setattr(db_mod, "DB_PATH", str(tmp_path / "t.db"))
        self._install_fake(monkeypatch)

        text = client_mod.call_llm("测试 prompt", caller="test_caller")
        assert text == '{"ok": true}'
        with db_conn() as conn:
            row = conn.execute("SELECT caller, prompt_preview, raw_output, ok "
                               "FROM llm_audit ORDER BY id DESC LIMIT 1").fetchone()
        assert row is not None
        assert row[0] == "test_caller"
        assert row[2] == '{"ok": true}'
        assert row[3] == 1

    def test_call_llm_json_writes_parsed(self, monkeypatch, tmp_path):
        import app.database as db_mod
        import app.llm.client as client_mod
        monkeypatch.setattr(db_mod, "DB_PATH", str(tmp_path / "t2.db"))
        self._install_fake(monkeypatch)

        parsed = client_mod.call_llm_json("测试 prompt", caller="test_json")
        assert parsed == {"ok": True}
        with db_conn() as conn:
            row = conn.execute("SELECT parsed_result, ok FROM llm_audit "
                               "ORDER BY id DESC LIMIT 1").fetchone()
        assert row is not None
        assert "ok" in row[0] and row[1] == 1

    def test_call_llm_json_writes_single_row(self, monkeypatch, tmp_path):
        """架构深化 G：一次 call_llm_json 只落一条审计（修复双写）。"""
        import app.database as db_mod
        import app.llm.client as client_mod
        monkeypatch.setattr(db_mod, "DB_PATH", str(tmp_path / "t_single.db"))
        self._install_fake(monkeypatch)

        client_mod.call_llm_json("测试 prompt", caller="single")
        with db_conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM llm_audit").fetchone()[0]
            row = conn.execute("SELECT parsed_result, ok, tokens FROM llm_audit").fetchone()
        assert n == 1  # 修复前：call_llm + call_llm_json 各写一行
        assert row[0] is not None and "ok" in row[0]  # 解析结果同记录
        assert row[1] == 1

    def test_not_configured_writes_failure_audit(self, monkeypatch, tmp_path):
        """架构深化 G：技术失败（未配置）也落一条 ok=0 审计（修复失败零记录）。"""
        import app.database as db_mod
        import app.llm.client as client_mod
        from app.llm.client import LLMError
        monkeypatch.setattr(db_mod, "DB_PATH", str(tmp_path / "t_fail.db"))
        monkeypatch.setattr(client_mod, "load_settings", lambda: {"llm": {}})

        with pytest.raises(LLMError):
            client_mod.call_llm("prompt", caller="fail_caller")
        with db_conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM llm_audit").fetchone()[0]
            row = conn.execute("SELECT caller, ok FROM llm_audit").fetchone()
        assert n == 1
        assert row[0] == "fail_caller" and row[1] == 0

    def test_parse_failure_fallback_writes_failure_audit(self, monkeypatch, tmp_path):
        """解析失败 → 返回 fallback 且审计 ok=0（单条，含原始输出）。"""
        import app.database as db_mod
        import app.llm.client as client_mod
        monkeypatch.setattr(db_mod, "DB_PATH", str(tmp_path / "t_parse.db"))
        self._install_fake(monkeypatch)

        result = client_mod.call_llm_json("prompt", caller="parse_fail", fallback={"fb": 1},
                                          validator=lambda d: None)
        assert result == {"fb": 1}
        with db_conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM llm_audit").fetchone()[0]
            row = conn.execute("SELECT ok, raw_output FROM llm_audit").fetchone()
        assert n == 1
        assert row[0] == 0 and row[1] == '{"ok": true}'


# ── P1-5 否决反事实 ──────────────────────────────────────────

class TestVetoCounterfactual:
    """量化池内候选赛道反事实收益：结算时回填，度量否决正确率。"""

    def _seed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db_mod, "DB_PATH", str(tmp_path / "v.db"))
        # 构造一条已结算赛道选择（含池内赛道）
        with db_conn() as conn:
            conn.execute(
                "INSERT INTO sector_selections (date, recommended_sectors, risk_sectors, "
                "sector_reasoning, regime_label, outcome, outcome_date, outcome_note, "
                "pool_sectors) VALUES ('2026-08-01', '[\"半导体\"]', '[]', 'r', 'BEAR', "
                "'胜', '2026-09-01', '20日收益 +3.00%', '[\"半导体\", \"食品\", \"贵金属\"]')")

    def test_pool_outcomes_filled(self, monkeypatch, tmp_path):
        from app.engine.evolve import _settle_pool_outcomes
        monkeypatch.setattr(db_mod, "DB_PATH", str(tmp_path / "v2.db"))
        outcomes = _settle_pool_outcomes([], "2026-08-01")
        assert outcomes == {}  # 空池直接返回空 dict

    def test_veto_stats_empty_pool(self, monkeypatch, tmp_path):
        """无池内反事实数据时不产生误报信号。"""
        from app.engine import evolve
        monkeypatch.setattr(db_mod, "DB_PATH", str(tmp_path / "v3.db"))
        monkeypatch.setattr(evolve.repo, "get_pool_outcomes_rows", lambda: [])
        monkeypatch.setattr(evolve.repo, "get_empty_reco_dates", lambda d: [])
        monkeypatch.setattr(evolve.repo, "get_reco_dates", lambda d: [])
        assert evolve._veto_stats() == []

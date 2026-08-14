"""C2/C4 进化行为测试：每日结算度量、月度重量活限频、元分析增量游标、self-fix 去重。

修复：月 1 号一次性进化 + 按月过滤收集 → 月中推荐永久丢失；GA 噪音重复入库。
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.engine import evolve


class TestMonthlyDue:
    """月度重量活间隔控制（≥28 天，经 get_interval_days 窄读）。"""

    def test_no_record_due(self, monkeypatch):
        monkeypatch.setattr(evolve.repo, "get_interval_days", lambda k: None)
        assert evolve._monthly_due() is True

    def test_recent_not_due(self, monkeypatch):
        monkeypatch.setattr(evolve.repo, "get_interval_days", lambda k: 10)
        assert evolve._monthly_due() is False

    def test_expired_due(self, monkeypatch):
        monkeypatch.setattr(evolve.repo, "get_interval_days", lambda k: 29)
        assert evolve._monthly_due() is True

    def test_bad_format_due(self, monkeypatch):
        monkeypatch.setattr(evolve.repo, "get_interval_days", lambda k: None)
        assert evolve._monthly_due() is True


class TestRunEvolveDaily:
    """每日自动路径（无 month 参数）：重量活未到期时只做结算+度量。"""

    @staticmethod
    def _stub_daily(monkeypatch, last_monthly_evolve_days_ago):
        monkeypatch.setattr(evolve, "_settle_outcomes", lambda: 0)
        monkeypatch.setattr(evolve, "compute_quality_metrics",
                            lambda s, e: {"sample_count": 0, "profit_rate": None})
        monkeypatch.setattr(evolve.repo, "save_quality_metrics", lambda m: None)
        if last_monthly_evolve_days_ago is None:
            monkeypatch.setattr(evolve.repo, "get_interval_days", lambda k: None)
        else:
            monkeypatch.setattr(evolve.repo, "get_interval_days",
                                lambda k: last_monthly_evolve_days_ago)

    def test_not_due_runs_only_settle(self, monkeypatch):
        """重量活未到期：自纠偏/GA/元分析/衰减均不执行。"""
        self._stub_daily(monkeypatch, 10)
        monkeypatch.setattr(evolve, "_review_ranking_all",
                            lambda: (_ for _ in ()).throw(AssertionError("不应触发自纠偏")))
        monkeypatch.setattr(evolve, "_ga_adjust",
                            lambda force=False: (_ for _ in ()).throw(AssertionError("不应触发GA")))
        monkeypatch.setattr(evolve, "_run_meta_analysis",
                            lambda m, d: (_ for _ in ()).throw(AssertionError("不应触发元分析")))
        monkeypatch.setattr(evolve, "_decay_insights",
                            lambda: (_ for _ in ()).throw(AssertionError("不应触发衰减")))
        evolve.run_evolve()  # 自动路径：无 month 参数

    def test_due_runs_heavy(self, monkeypatch):
        """重量活到期：自纠偏 → GA → 元分析 → 衰减 依次执行。"""
        self._stub_daily(monkeypatch, 29)
        order = []
        monkeypatch.setattr(evolve, "_review_ranking_all", lambda: [])
        monkeypatch.setattr(evolve, "_ga_adjust", lambda force=False: order.append("ga") or None)
        monkeypatch.setattr(evolve, "_run_meta_analysis", lambda m, d: order.append("meta"))
        monkeypatch.setattr(evolve, "_decay_insights", lambda: order.append("decay") or 0)
        evolve.run_evolve()
        assert order == ["ga", "meta", "decay"]

    def test_manual_month_always_heavy(self, monkeypatch):
        """手动补算历史月份（month 参数）无条件执行重量活。"""
        self._stub_daily(monkeypatch, 2)  # 未到期
        order = []
        monkeypatch.setattr(evolve, "_review_ranking_all", lambda: [])
        monkeypatch.setattr(evolve, "_ga_adjust", lambda force=False: order.append("ga") or None)
        monkeypatch.setattr(evolve, "_run_meta_analysis", lambda m, d: order.append("meta"))
        monkeypatch.setattr(evolve, "_decay_insights", lambda: order.append("decay") or 0)
        evolve.run_evolve("2026-07")
        assert order == ["ga", "meta", "decay"]

    def test_settle_always_runs_first(self, monkeypatch):
        """结算恒先于度量/重量活（每日路径也结算）。"""
        order = []
        monkeypatch.setattr(evolve, "_settle_outcomes", lambda: order.append("settle") or 0)
        monkeypatch.setattr(evolve, "compute_quality_metrics",
                            lambda s, e: order.append("measure") or {"sample_count": 0, "profit_rate": None})
        monkeypatch.setattr(evolve.repo, "save_quality_metrics", lambda m: None)
        monkeypatch.setattr(evolve.repo, "get_interval_days", lambda k: None)  # due=True
        monkeypatch.setattr(evolve, "_review_ranking_all", lambda: [])
        monkeypatch.setattr(evolve, "_ga_adjust", lambda force=False: None)
        monkeypatch.setattr(evolve, "_run_meta_analysis", lambda m, d: None)
        monkeypatch.setattr(evolve, "_decay_insights", lambda: 0)
        evolve.run_evolve()
        assert order[0] == "settle"


class TestMetaAnalysisCursor:
    """C2：元分析增量游标——已分析案例不重复，LLM 失败不推进。"""

    @staticmethod
    def _case(i, outcome="胜"):
        return {"id": i, "recommend_log_id": i, "recommended_sectors": '["光伏"]',
                "sector_reasoning": "r", "regime_label": "BULL", "outcome": outcome,
                "outcome_note": "", "buy_reason": "", "code": "000001", "name": "A",
                "signal": "HOLD", "trigger_trailing": 0, "trigger_drift": 0,
                "trigger_sector_adv": 0, "logic_verdict": "维持", "sector_risk": 0,
                "holding_risk": 0, "detail": ""}

    def test_cursor_advances_after_success(self, monkeypatch):
        """LLM 有产出 → 游标推进到 max(ss.id)，下次不再收集。"""
        saved = []
        monkeypatch.setattr(evolve.repo, "get_int_cursor", lambda k: 5)
        monkeypatch.setattr(evolve.repo, "save_meta", lambda k, v: saved.append((k, v)))
        monkeypatch.setattr(evolve.repo, "get_settled_cases_after",
                            lambda ss_id: [self._case(7), self._case(8, "负")])
        monkeypatch.setattr(evolve, "_batch_llm_analyze",
                            lambda *a, **k: [{"insight": "教训X", "type": "sector"}])
        monkeypatch.setattr(evolve, "_save_insight", lambda ins, degraded=False: True)
        monkeypatch.setattr(evolve, "_decision_loss_streak", lambda: 0)
        evolve._run_meta_analysis({}, False)
        assert ("last_analysis_ss_id", "8") in saved

    def test_cursor_holds_on_llm_failure(self, monkeypatch):
        """LLM 失败（返回 None）→ 游标不动，下次重试。"""
        saved = []
        monkeypatch.setattr(evolve.repo, "get_int_cursor", lambda k: 5)
        monkeypatch.setattr(evolve.repo, "save_meta", lambda k, v: saved.append((k, v)))
        monkeypatch.setattr(evolve.repo, "get_settled_cases_after",
                            lambda ss_id: [self._case(7)])
        monkeypatch.setattr(evolve, "_batch_llm_analyze", lambda *a, **k: None)
        evolve._run_meta_analysis({}, False)
        assert not any(k == "last_analysis_ss_id" for k, _ in saved)

    def test_no_new_cases_skips_llm(self, monkeypatch):
        monkeypatch.setattr(evolve.repo, "get_int_cursor", lambda k: 5)
        monkeypatch.setattr(evolve.repo, "get_settled_cases_after", lambda ss_id: [])
        monkeypatch.setattr(evolve, "_batch_llm_analyze",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应调用LLM")))
        evolve._run_meta_analysis({}, False)  # 不抛异常即通过


class TestSaveSelfFixDedup:
    """C4：self-fix 数字归一化去重——fitness 数值变化不再累积重复记录。"""

    def test_numeric_normalized_dedup(self, monkeypatch):
        inserted = []
        monkeypatch.setattr(evolve.repo, "get_all_insights",
                            lambda: ["GA寻优应用: fitness 28.000→30.500, 配置 {'model_weight': 0.8}"])
        monkeypatch.setattr(evolve.repo, "insert_insight",
                            lambda t, typ, d, active=1: inserted.append(t))
        evolve._save_self_fix("GA寻优应用: fitness 30.000→45.000, 配置 {'model_weight': 0.8}")
        assert inserted == []  # 数字归一化后与既有记录相同 → 去重

    def test_different_meaning_inserted(self, monkeypatch):
        inserted = []
        monkeypatch.setattr(evolve.repo, "get_all_insights", lambda: ["其他记录"])
        monkeypatch.setattr(evolve.repo, "insert_insight",
                            lambda t, typ, d, active=1: inserted.append(t))
        evolve._save_self_fix("GA寻优应用: fitness 30.000→45.000, 配置 {'model_weight': 0.8}")
        assert len(inserted) == 1

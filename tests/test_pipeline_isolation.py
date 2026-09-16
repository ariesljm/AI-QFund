"""C1 管线解耦测试：推荐/监控独立容错，进化每天附加（重任务内部限频）。

修复：推荐引擎失败（LLM 失败/模型缺失 raise）曾中断整条管线，持仓断盯；
监控信号链（R2c 连续确认、WARNING 升级）依赖每日盯盘，现推荐与监控各自容错。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.pipeline as pipeline


class TestEvolvePhaseDaily:
    """C2：进化 phase 每天附加（不再限月 1 号）。"""

    def test_attached_every_day(self):
        from datetime import datetime
        for d in (datetime(2026, 8, 15), datetime(2026, 9, 1), datetime(2026, 8, 8)):
            phases = pipeline._evolve_phase(d)
            assert len(phases) == 1 and phases[0][0] == "进化引擎"


class TestRunPhaseSafely:
    """单 phase 容错：失败仅记录，不向上抛（后续槽位继续）。"""

    def test_failure_swallowed(self):
        def boom():
            raise RuntimeError("LLM失败")
        pipeline._run_phase_safely("推荐引擎", boom, "cid")  # 不抛异常即通过

    def test_success_runs_fn(self):
        calls = []
        pipeline._run_phase_safely("监控引擎", lambda: calls.append(1), "cid")
        assert calls == [1]


class TestRecommendFailureKeepsMonitor:
    """推荐失败 → 2.0 监控仍执行（信号链连续）。"""
    def test_run_recommend_continues_monitor(self, monkeypatch):
        from datetime import datetime
        order = []
        monkeypatch.setattr(pipeline, "run_recommendation",
                            lambda: (_ for _ in ()).throw(RuntimeError("模型缺失")))
        monkeypatch.setattr(pipeline, "_run_supervise_safely",
                            lambda cid, today: order.append("supervise"))
        pipeline.run_recommend(datetime(2026, 8, 10))
        assert order == ["supervise"]

    def test_data_failure_keeps_recommend_and_monitor(self, monkeypatch):
        """数据基座失败但数据就绪（历史数据在）→ 推荐（旧特征护栏）与监控继续。"""
        from datetime import datetime
        order = []
        monkeypatch.setattr("app.engine.recommend_v2.recommend_v2_enabled", lambda: False)
        monkeypatch.setattr(pipeline, "run_data_foundation",
                            lambda steps=None: (_ for _ in ()).throw(RuntimeError("网络失败")))
        monkeypatch.setattr(pipeline, "_ensure_recommend_data_ready", lambda: True)
        monkeypatch.setattr(pipeline, "run_recommendation", lambda: order.append("recommend"))
        monkeypatch.setattr(pipeline, "_run_supervise_safely",
                            lambda cid, today: order.append("supervise"))
        monkeypatch.setattr(pipeline, "_run_style_track_safely", lambda cid: None)
        pipeline.run(datetime(2026, 8, 10))
        assert order == ["recommend", "supervise"]

    def test_gate_exception_keeps_monitor(self, monkeypatch):
        """门控自身 DB 异常 → 推荐跳过但 2.0 监控照常（架构深化 A 回归：不再拖死后续槽位）。"""
        from datetime import datetime
        called: list[str] = []

        def boom():
            raise RuntimeError("DB 连接失败")
        monkeypatch.setattr(pipeline, "_ensure_recommend_data_ready", boom)
        monkeypatch.setattr(pipeline, "run_recommendation", lambda: called.append("rec"))
        monkeypatch.setattr(pipeline, "_run_supervise_safely",
                            lambda cid, today: called.append("supervise"))
        pipeline.run_recommend(datetime(2026, 8, 10))
        assert called == ["supervise"]

    def test_run_supervise_slot_present(self, monkeypatch):
        """全流程（2.0 接管）：数据基座 → 推荐 → 2.0 监控纵向完整（无 1.x 进化槽位）。"""
        from datetime import datetime
        order = []
        monkeypatch.setattr("app.engine.recommend_v2.recommend_v2_enabled", lambda: False)
        monkeypatch.setattr(pipeline, "run_data_foundation", lambda steps=None: order.append("data"))
        monkeypatch.setattr(pipeline, "_ensure_recommend_data_ready", lambda: True)
        monkeypatch.setattr(pipeline, "run_recommendation", lambda: order.append("recommend"))
        monkeypatch.setattr(pipeline, "_run_supervise_safely",
                            lambda cid, today: order.append("supervise"))
        monkeypatch.setattr(pipeline, "_run_style_track_safely", lambda cid: None)
        pipeline.run(datetime(2026, 8, 10))
        assert order == ["data", "recommend", "supervise"]


class TestRecommendGateSelfHeal:
    """推荐门控自愈：数据未就绪时先补齐（复用数据基座机制），自愈失败冷却后再拦截。

    就绪判定经 repo.is_recommend_data_ready 单一谓词（异常兜底 False），
    自愈动作的计数细节仍由 repo.check_data_ready 提供。
    """

    def test_ready_skips_heal(self, monkeypatch):
        monkeypatch.setattr(pipeline.repo, "is_recommend_data_ready", lambda: True)
        called = []
        monkeypatch.setattr(pipeline, "run_data_foundation",
                            lambda steps=None: called.append(steps))
        monkeypatch.setattr(pipeline, "update_industry_map",
                            lambda: called.append("ind"))
        assert pipeline._ensure_recommend_data_ready() is True
        assert called == []  # 已就绪不触发自愈

    def test_industry_empty_triggers_incremental_map(self, monkeypatch):
        """持仓就绪但行业映射空 → 只增量补拉行业映射（不重跑持仓）。"""
        monkeypatch.setattr(pipeline.repo, "is_recommend_data_ready",
                            lambda: False)
        monkeypatch.setattr(pipeline.repo, "get_interval_days", lambda k: None)
        monkeypatch.setattr(pipeline.repo, "check_data_ready",
                            lambda: {"holdings_cnt": 10, "industry_cnt": 0})
        monkeypatch.setattr(pipeline.repo, "save_meta", lambda k, v: None)
        called = []
        monkeypatch.setattr(pipeline, "run_data_foundation",
                            lambda steps=None: called.append(("full", steps)))
        monkeypatch.setattr(pipeline, "update_industry_map",
                            lambda: called.append(("map",)))
        assert pipeline._ensure_recommend_data_ready() is False  # 模拟拉取后仍空
        assert called == [("map",)]  # 未触发全量 Step 4

    def test_holdings_empty_triggers_step4(self, monkeypatch):
        """持仓也空 → 触发数据基座 Step 4（首次自举）。"""
        monkeypatch.setattr(pipeline.repo, "is_recommend_data_ready",
                            lambda: False)
        monkeypatch.setattr(pipeline.repo, "get_interval_days", lambda k: None)
        monkeypatch.setattr(pipeline.repo, "check_data_ready",
                            lambda: {"holdings_cnt": 0, "industry_cnt": 0})
        monkeypatch.setattr(pipeline.repo, "save_meta", lambda k, v: None)
        called = []
        monkeypatch.setattr(pipeline, "run_data_foundation",
                            lambda steps=None: called.append(steps))
        assert pipeline._ensure_recommend_data_ready() is False
        assert called == [[4]]

    def test_heal_failure_cooldown_skips_retry(self, monkeypatch):
        """自愈失败落冷却标记 → 冷却期内直接拦截，不再触发拉取。"""
        monkeypatch.setattr(pipeline.repo, "is_recommend_data_ready",
                            lambda: False)
        monkeypatch.setattr(pipeline.repo, "get_interval_days", lambda k: 1)
        called = []
        monkeypatch.setattr(pipeline, "run_data_foundation",
                            lambda steps=None: called.append(steps))
        assert pipeline._ensure_recommend_data_ready() is False
        assert called == []

    def test_heal_success_clears_after_cooldown(self, monkeypatch):
        """冷却期过后重试成功 → 返回 True。"""
        state = {"ready": False}
        monkeypatch.setattr(pipeline.repo, "is_recommend_data_ready",
                            lambda: state["ready"])
        monkeypatch.setattr(pipeline.repo, "get_interval_days", lambda k: 2)
        monkeypatch.setattr(pipeline.repo, "save_meta", lambda k, v: None)

        def _check():
            if not state["ready"]:
                state["ready"] = True
                return {"holdings_cnt": 1, "industry_cnt": 0}
            return {"holdings_cnt": 1, "industry_cnt": 2}
        monkeypatch.setattr(pipeline.repo, "check_data_ready", _check)
        monkeypatch.setattr(pipeline, "update_industry_map", lambda: None)
        assert pipeline._ensure_recommend_data_ready() is True


class TestRecommendV2Switch:
    """2.0 切换点（票 11）：enabled 走 recommend_top5，否则 1.x run_recommendation。"""

    def test_disabled_keeps_1x_path(self, monkeypatch):
        called = []
        monkeypatch.setattr("app.pipeline._run_recommend_gated",
                            lambda: called.append("1x"))
        monkeypatch.setattr("app.engine.recommend_v2.recommend_v2_enabled",
                            lambda: False)
        pipeline._run_recommend_safely("cid")
        assert called == ["1x"]

    def test_enabled_uses_recommend_top5(self, monkeypatch):
        called = []
        fake_top5 = {"date": "2026-09-16", "top5": ["F1", "F2"]}
        monkeypatch.setattr("app.engine.recommend_v2.recommend_v2_enabled",
                            lambda: True)
        monkeypatch.setattr("app.engine.recommend_v2.recommend_top5",
                            lambda today: called.append(today) or fake_top5)
        monkeypatch.setattr("app.pipeline._run_recommend_gated",
                            lambda: called.append("1x"))
        pipeline._run_recommend_safely("cid")
        assert called == ["2026-09-16"]   # 只走 2.0，不碰 1.x 门控

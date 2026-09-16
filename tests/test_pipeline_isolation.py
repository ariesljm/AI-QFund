"""C1 管线解耦测试（2.0 精简，票 22）：数据基座/推荐/监控各自容错。

修复：推荐引擎失败（LLM 失败/模型缺失 raise）曾中断整条管线；现推荐与
监控各自容错，异常仅记录不阻断后续槽位。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.pipeline as pipeline


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


class TestRecommendFailureKeepsSupervise:
    """推荐失败 → 2.0 监控仍执行（信号链连续）。"""

    def test_run_recommend_continues_monitor(self, monkeypatch):
        from datetime import datetime
        order = []
        monkeypatch.setattr("app.engine.recommend_v2.recommend_top5",
                            lambda today: (_ for _ in ()).throw(RuntimeError("LLM失败")))
        monkeypatch.setattr("app.engine.supervise.run_supervision",
                            lambda today, limit=50: order.append("supervise"))
        pipeline.run_recommend(datetime(2026, 8, 10))
        assert order == ["supervise"]

    def test_data_failure_keeps_recommend_and_monitor(self, monkeypatch):
        """数据基座失败 → 推荐（2.0 自带健康门）与监控继续。"""
        from datetime import datetime
        order = []
        monkeypatch.setattr(pipeline, "run_data_foundation",
                            lambda steps=None: (_ for _ in ()).throw(RuntimeError("网络失败")))
        monkeypatch.setattr("app.engine.recommend_v2.recommend_top5",
                            lambda today: order.append("recommend"))
        monkeypatch.setattr("app.engine.supervise.run_supervision",
                            lambda today, limit=50: order.append("supervise"))
        pipeline.run(datetime(2026, 8, 10))
        assert order == ["recommend", "supervise"]

    def test_recommend_slot_order(self, monkeypatch):
        """推荐槽位纵向完整：推荐 → 监控。"""
        from datetime import datetime
        order = []
        monkeypatch.setattr("app.engine.recommend_v2.recommend_top5",
                            lambda today: order.append("recommend"))
        monkeypatch.setattr("app.engine.supervise.run_supervision",
                            lambda today, limit=50: order.append("supervise"))
        pipeline.run_recommend(datetime(2026, 8, 10))
        assert order == ["recommend", "supervise"]

    def test_full_run_has_data_slot(self, monkeypatch):
        """全流程：数据基座 → 推荐 → 监控。"""
        from datetime import datetime
        order = []
        monkeypatch.setattr(pipeline, "run_data_foundation",
                            lambda steps=None: order.append("data"))
        monkeypatch.setattr("app.engine.recommend_v2.recommend_top5",
                            lambda today: order.append("recommend"))
        monkeypatch.setattr("app.engine.supervise.run_supervision",
                            lambda today, limit=50: order.append("supervise"))
        pipeline.run(datetime(2026, 8, 10))
        assert order == ["data", "recommend", "supervise"]

    def test_supervise_exception_swallowed(self, monkeypatch):
        """监控自身异常 → 不向调用方抛出（槽位容错）。"""
        from datetime import datetime
        monkeypatch.setattr("app.engine.recommend_v2.recommend_top5", lambda today: None)
        monkeypatch.setattr("app.engine.supervise.run_supervision",
                            lambda today, limit=50: (_ for _ in ()).throw(RuntimeError("DB锁")))
        pipeline.run_recommend(datetime(2026, 8, 10))   # 不抛即通过

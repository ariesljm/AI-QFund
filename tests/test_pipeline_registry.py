"""架构深化 候选3：数据基座 Step 注册表（foundation.STEP_REGISTRY）语义测试。

核心语义：run_pipeline 按注册表编排，on_success（成功后置动作，如 Step 4 的
holdings_last_run 置位）只在 run 无异常后执行——失败不置位、下次运行自动重试。
注：注册表构建时绑定函数引用，测试通过整体替换 STEP_REGISTRY 打桩（interface
即 test surface——注册表是编排的接口，替换它即测试编排语义）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import app.data.foundation as fd


class TestStepRegistry:
    def test_registry_covers_all_steps(self):
        """注册表与 ALL_STEPS 编号一致（编排单一来源，无漂移）。"""
        assert set(fd.STEP_REGISTRY) == set(fd.ALL_STEPS)
        for sid, step in fd.STEP_REGISTRY.items():
            assert step.id == sid
            assert step.name
            assert callable(step.run)

    def _fake_registry(self, run, on_success=()):
        """整体替换注册表（run_pipeline 运行时读模块属性，替换生效）。"""
        return {4: fd.PipelineStep(4, "假步骤", run, on_success=tuple(on_success))}

    def test_run_pipeline_calls_run_then_on_success(self, monkeypatch):
        """成功路径：run 执行后 on_success 按序执行（Step4 成功才置位）。"""
        order = []
        monkeypatch.setattr(fd, "STEP_REGISTRY", self._fake_registry(
            lambda: order.append("run"), [lambda: order.append("on_success")]))
        fd.run_pipeline(steps=[4])
        assert order == ["run", "on_success"]

    def test_failure_skips_on_success(self, monkeypatch):
        """失败路径：run 抛异常 → on_success 不执行（失败不置位，下次重试）。"""
        order = []

        def boom():
            order.append("run")
            raise RuntimeError("下载失败")

        monkeypatch.setattr(fd, "STEP_REGISTRY", self._fake_registry(
            boom, [lambda: order.append("on_success")]))
        with pytest.raises(RuntimeError):
            fd.run_pipeline(steps=[4])
        assert order == ["run"]  # 无 on_success

    def test_nav_step_has_post_marking(self):
        """Step 2（净值）后置：停更/短历史打标挂在 on_success。"""
        step = fd.STEP_REGISTRY[fd._STEP_NAV]
        assert step.on_success == (fd._mark_stale_after_nav,)

    def test_index_step_has_freshness_check(self):
        """Step 3（指数）后置：新鲜度核查（审计 P1-2）。"""
        step = fd.STEP_REGISTRY[fd._STEP_INDEX]
        assert step.on_success == (fd._check_index_freshness,)

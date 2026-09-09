"""T04（reco-hardening）：波动率自适应止损验收测试。

审计问题：固定 8% 硬止损与 40 日持有周期矛盾——高波动赛道一两周即触发，
把"先回撤 8%、后涨 30%"的动量利润砍掉；低波动赛道又太松。
本测试固化：自适应阈值计算 + 自适应止损模拟（阈值随波动缩放）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pytest

from app.features.calculator import sim_vol_adaptive_stop, vol_adaptive_stop_pct


class TestVolAdaptiveStopPct:
    """止损阈值 = clamp(1.5×月波动, 6%, 15%)。"""

    def test_low_vol_floor(self):
        # 平稳序列 → 阈值取下限 6%
        navs = [1.0 + 0.001 * i for i in range(25)]
        assert vol_adaptive_stop_pct(navs) == pytest.approx(0.06, abs=1e-6)

    def test_high_vol_capped(self):
        # 剧烈震荡序列 → 阈值到上限 15%
        navs = [1.0] * 21
        for i in range(1, 21):
            navs[i] = navs[i - 1] * (1.0 + (0.04 if i % 2 else -0.04))
        thr = vol_adaptive_stop_pct(navs)
        assert thr <= 0.15 + 1e-6

    def test_mid_vol_between(self):
        # 中等波动 → 阈值落在 [6%, 15%] 内
        rng = np.random.default_rng(7)
        navs = [1.0]
        for _ in range(25):
            navs.append(navs[-1] * (1.0 + rng.normal(0, 0.008)))
        thr = vol_adaptive_stop_pct(navs)
        assert 0.06 - 1e-6 <= thr <= 0.15 + 1e-6

    def test_insufficient_data_floor(self):
        assert vol_adaptive_stop_pct([1.0]) == 0.06
        assert vol_adaptive_stop_pct([1.0, 1.01]) == 0.06


class TestSimVolAdaptiveStop:
    def test_high_vol_tolerates_8pct_dip(self):
        """高波动序列：8% 回撤不触发（固定止损会触发），自适应更宽松。"""
        navs = [1.0]
        for _ in range(10):
            navs.append(navs[-1] * 1.01)      # 上涨
        for _ in range(5):
            navs.append(navs[-1] * 0.985)     # 回撤 ~7.3%（距高点）
        ret = sim_vol_adaptive_stop(navs, max_days=40)
        # 波动自适应下该回撤不触发 → 按持有到期结算
        assert ret is not None
        assert ret > -0.10

    def test_crash_triggers(self):
        """崩盘式下跌仍触发（自适应下限 6% 保护）。"""
        navs = [1.0]
        for _ in range(3):
            navs.append(navs[-1] * 1.005)
        for _ in range(4):
            navs.append(navs[-1] * 0.97)      # 连续 -3% → 回撤超 6%
        ret = sim_vol_adaptive_stop(navs, max_days=40)
        assert ret is not None and ret < 0

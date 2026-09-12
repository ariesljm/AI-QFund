"""ticket 05 Style Tracking 反推引擎测试。

只测**外部契约**（不测求解器内部迭代）：
- 约束满足（非负、Σ=1）
- r_squared 落在 [0,1]
- 对已知合成持仓能还原主行业
- 数据不足/异常优雅降级
"""

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

import app.repo as repo
from app.engine.style_track import solve_style_weights, top_industries


def _synthetic(T=60, N=5, true_w=None, noise=0.0, seed=42):
    """构造板块收益率矩阵（QR 正交化 → 板块间零相关）。

    正交化用于验证**求解器还原能力**这一外部契约；真实板块间高度相关，
    权重会被摊平（RBSA 已知局限，见 style_track 模块 docstring）。
    """
    rng = np.random.default_rng(seed)
    A = rng.normal(0.0, 1.0, (T, N))
    A -= A.mean(axis=0)
    Q, _ = np.linalg.qr(A)                      # 正交列（单位范数）
    Q *= 0.01 * np.sqrt(T)                      # 缩放到日收益率量级（元素 ~1%）
    Q -= Q.mean(axis=0)                         # 零均值：无截距回归要求
    R = Q
    w_true = np.array(true_w if true_w is not None else [0.7, 0.3, 0, 0, 0], dtype=float)
    r = R @ w_true + rng.normal(0.0, noise, T)
    return r, R


class TestSolveStyleWeights:
    def test_constraints_satisfied(self):
        r, R = _synthetic(noise=0.002)
        w, r2 = solve_style_weights(r, R)
        assert w.shape == (5,)
        assert (w >= 0).all()                       # 非负
        assert abs(w.sum() - 1.0) < 1e-6            # Σ=1
        assert 0.0 <= r2 <= 1.0                     # r² 范围

    def test_recovers_primary_industry(self):
        """已知合成持仓（0.7/0.3）应还原出主行业 A 且权重量级接近。"""
        r, R = _synthetic(noise=0.001)
        w, r2 = solve_style_weights(r, R)
        assert int(np.argmax(w)) == 0               # 主行业 = A
        assert w[0] > 0.5                            # 主行业权重占优
        assert w[1] > w[2] and w[1] > w[3]          # B 次之
        assert r2 > 0.9                              # 低噪声下拟合优度应高

    def test_perfect_fit_noiseless(self):
        r, R = _synthetic(noise=0.0)
        w, r2 = solve_style_weights(r, R, ridge=0.0)
        assert r2 > 0.999
        assert abs(w[0] - 0.7) < 0.02
        assert abs(w[1] - 0.3) < 0.02

    def test_insufficient_window_degrades(self):
        w, r2 = solve_style_weights(np.array([0.01]), np.zeros((1, 3)))
        assert w.sum() == 0.0 and r2 == 0.0

    def test_shape_mismatch_degrades(self):
        w, r2 = solve_style_weights(np.zeros(10), np.zeros((5, 3)))
        assert r2 == 0.0

    def test_constant_sector_excluded(self):
        """零方差板块不应分走权重（Σ=1 不被其独占）。"""
        rng = np.random.default_rng(0)
        A = rng.normal(0.0, 1.0, (60, 3))
        A -= A.mean(axis=0)
        Q, _ = np.linalg.qr(A)
        Q *= 0.01 * np.sqrt(60)
        Q -= Q.mean(axis=0)
        R = Q
        R[:, 2] = 0.0                      # 第三个板块全为常数
        r = R @ np.array([0.6, 0.4, 0.0])
        w, r2 = solve_style_weights(r, R, ridge=0.0)
        assert w[2] == 0.0
        assert abs(w.sum() - 1.0) < 1e-6
        assert r2 > 0.99


class TestTopIndustries:
    def test_returns_top_k_descending(self):
        w = np.array([0.1, 0.6, 0.3])
        names = ["A", "B", "C"]
        assert top_industries(w, names, k=2) == [("B", 0.6), ("C", 0.3)]

    def test_skips_zero_weights(self):
        w = np.array([1.0, 0.0])
        assert top_industries(w, ["A", "B"], k=2) == [("A", 1.0)]

    def test_empty_inputs(self):
        assert top_industries(np.array([]), []) == []


class TestComputeFundStyleUnitAlignment:
    def test_percent_pct_chg_converted_to_decimal(self, monkeypatch):
        """pct_chg 存百分数、基金收益率是小数：不换算会让板块量级放大 100 倍、拟合失败。"""
        rng = np.random.default_rng(7)
        a_ret = 0.01 + rng.normal(0.0, 0.002, 60)    # 小数口径，有波动
        b_ret = -0.01 + rng.normal(0.0, 0.002, 60)
        base = date(2026, 1, 1)
        navs = [(base.isoformat(), 100.0)]
        for i, r in enumerate(a_ret, start=1):
            navs.append(((base + timedelta(days=i)).isoformat(),
                         navs[-1][1] * (1.0 + r)))
        # 板块收益率按**百分数**存（× 100），与生产口径一致
        rows = []
        for i, (d, _) in enumerate(navs[1:]):
            rows.append((d, "BK0001", "板块A", a_ret[i] * 100.0))
            rows.append((d, "BK0002", "板块B", b_ret[i] * 100.0))
        monkeypatch.setattr(repo.nav, "series",
                            lambda code, limit=None, until=None: navs)
        monkeypatch.setattr(repo, "get_sector_pct_series", lambda a, b: rows)
        saved: dict = {}
        monkeypatch.setattr(repo, "save_fund_style",
                            lambda c, d, top, r2: saved.update(code=c, top=top, r2=r2))

        from app.engine.style_track import compute_fund_style
        r = compute_fund_style("TEST9", window=60)

        assert r is not None
        assert r["industry_1"] == "板块A"      # 单位对齐后才能识别出真正跟随的赛道
        assert r["weight_1"] > 90.0        # 百分数（与季报 rbsa_weight_1 同口径）
        assert r["r_squared"] > 0.99
        assert saved["code"] == "TEST9"         # 落库被调用

    def test_insufficient_nav_returns_none(self, monkeypatch):
        monkeypatch.setattr(repo.nav, "series",
                            lambda code, limit=None, until=None: [("2026-01-01", 1.0)])
        from app.engine.style_track import compute_fund_style
        assert compute_fund_style("TEST9", window=60) is None

"""技术栈豁免 smoke test：特征计算域引入的 scipy/sklearn 可用。

ticket 01（ADR 豁免 + 科学计算依赖）验收锁：
- 特征计算域可 import sklearn/scipy；
- ElasticNet 的 positive=True（非负约束）能求解——这是 Style Tracking
  反推引擎（非负、和为 1 权重）的核心求解原语。
"""

import numpy as np
from sklearn.linear_model import ElasticNet


def test_elasticnet_positive_constrained_solves():
    """positive=True 求解非负稀疏权重，可恢复主要方向。"""
    rng = np.random.default_rng(0)
    X = rng.standard_normal((200, 5))
    true_w = np.array([0.5, 0.3, 0.2, 0.0, 0.0])
    y = X @ true_w + 0.01 * rng.standard_normal(200)

    model = ElasticNet(alpha=0.01, l1_ratio=0.5, positive=True,
                       fit_intercept=False, max_iter=100000)
    model.fit(X, y)
    w = model.coef_

    # 非负约束满足（数值容差）
    assert (w >= -1e-8).all()
    # 前 3 个方向被恢复、后 2 个被稀疏掉
    assert w[0] > 0.1 and w[1] > 0.1 and w[2] > 0.1
    assert w[3] < 0.05 and w[4] < 0.05


def test_scipy_linalg_available():
    """scipy.linalg 可做矩阵求逆/求解（后续 Kalman 的矩阵原语）。"""
    from scipy import linalg
    A = np.array([[4.0, 1.0], [1.0, 3.0]])
    x = linalg.solve(A, np.array([1.0, 2.0]))
    np.testing.assert_allclose(A @ x, [1.0, 2.0], atol=1e-10)

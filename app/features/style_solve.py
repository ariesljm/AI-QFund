"""带约束回归求解器（RBSA 反推核心，ADR-0007 豁免边界内）。

净值反推行业暴露的**求解部分**：约束最小二乘回归（非负、Σw=1）。
纯 numpy 凸优化，零引擎/DB 依赖——"家"在 features 域（ADR-0007 明文
豁免边界：仅 features 域可做带约束回归）。引擎层（style_track）只做
取数/落库编排，消费本模块的求解器。

模型：
    min_w  ‖r_fund − R_sector·w‖² + λ‖w‖²
    s.t.   w ≥ 0,  Σw = 1

λ 为岭正则项：板块数（约 140）常多于窗口样本数（60），不加正则会严重
过拟合；约束 Σw=1 本身也起稳定作用。
"""

import numpy as np

_DEFAULT_RIDGE = 0.05  # 默认岭正则强度：相对板块收益方差的比例（尺度无关）
_MAX_ITER = 2000
_TOL = 1e-13


def _project_simplex(v: np.ndarray) -> np.ndarray:
    """欧氏投影到单纯形 {w ≥ 0, Σw = 1}（Duchi et al. 2008）。"""
    n = v.size
    u = np.sort(v)[::-1]
    css = np.cumsum(u)
    cond = u * np.arange(1, n + 1) > (css - 1.0)
    rho = int(np.nonzero(cond)[0][-1])
    theta = (css[rho] - 1.0) / (rho + 1.0)
    return np.maximum(v - theta, 0.0)


def solve_style_weights(fund_ret: np.ndarray, sector_ret: np.ndarray,
                        ridge: float = _DEFAULT_RIDGE,
                        max_iter: int = _MAX_ITER) -> tuple[np.ndarray, float]:
    """约束最小二乘反推行业权重，返回 (weights, r_squared)。

    fund_ret: (T,) 基金日收益率；
    sector_ret: (T, N) 板块日收益率矩阵；
    weights: (N,) 非负且和=1；
    r_squared: 拟合优度（对 fund_ret 自身方差的解释比例，≤1）。

    求解用**投影梯度法**（问题凸，步长 1/L 保证收敛到全局最优）。
    不用 SLSQP：目标值量级 ~1e-6（收益率平方），其绝对容差会让求解器
    在初始均匀点附近假收敛（实测停在 [0.5,0.5] 而非真解 [0.6,0.4]）。

    数据异常（T<2 / N=0 / 形状不匹配 / 板块全常数）返回 (全 0 权重, 0.0)。
    """
    r = np.asarray(fund_ret, dtype=float).ravel()
    R = np.asarray(sector_ret, dtype=float)
    if R.ndim == 1:
        R = R.reshape(-1, 1)
    if r.size < 2 or R.size == 0 or R.shape[0] != r.size:
        return np.zeros(R.shape[1] if R.ndim == 2 else 0), 0.0
    n = R.shape[1]

    # 常量板块（零方差）无法贡献拟合，先剔除以免 Σw=1 约束被其独占
    keep = np.where(R.std(axis=0) > 1e-12)[0]
    if keep.size == 0:
        return np.zeros(n), 0.0

    Rk = R[:, keep]
    k = keep.size
    # 岭正则按数据尺度缩放（收益率量级 ~1e-2，绝对 λ 会压过残差项）
    scale = float((Rk ** 2).mean()) or 1.0
    lam = ridge * scale

    # 步长 1/L，L 为梯度 Lipschitz 常数：目标 = ‖r−Rw‖² + λ‖w‖²，
    # Hessian = 2(RᵀR + λI) → L 取 2·(λ_max(RᵀR) + λ)
    gram = Rk.T @ Rk
    l_max = float(np.linalg.eigvalsh(gram).max()) if gram.size else 0.0
    L = 2.0 * (max(l_max, 0.0) + lam) or 1.0
    step = 1.0 / L

    w_k = np.full(k, 1.0 / k)
    for _ in range(max_iter):
        grad = -2.0 * (Rk.T @ (r - Rk @ w_k)) + 2.0 * lam * w_k
        w_new = _project_simplex(w_k - step * grad)
        if np.max(np.abs(w_new - w_k)) < _TOL:
            w_k = w_new
            break
        w_k = w_new

    w = np.zeros(n)
    w[keep] = w_k
    resid = r - R @ w
    ss_res = float(resid @ resid)
    ss_tot = float(((r - r.mean()) ** 2).sum())
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-12 else 0.0
    return w, float(min(max(r2, 0.0), 1.0))


def top_industries(weights: np.ndarray, industry_names: list[str],
                   k: int = 2) -> list[tuple[str, float]]:
    """取权重最高的 k 个行业 [(行业名, 权重)]（降序，权重为 0 的不返回）。"""
    w = np.asarray(weights, dtype=float)
    if w.size == 0 or not industry_names:
        return []
    order = np.argsort(w)[::-1][:k]
    return [(industry_names[i], float(w[i])) for i in order
            if i < len(industry_names) and w[i] > 1e-9]

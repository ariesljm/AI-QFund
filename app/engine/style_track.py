"""净值反推风格暴露（RBSA，ticket 05）。

用基金日收益率对板块日收益率矩阵做**约束最小二乘**回归，反推当前行业暴露权重
（非负、Σ=1）——Return-Based Style Analysis。

模型：
    min_w  ‖r_fund − R_sector·w‖² + λ‖w‖²
    s.t.   w ≥ 0,  Σw = 1

λ 为岭正则项：板块数（约 140）常多于窗口样本数（60），不加正则会严重过拟合；
约束 Σw=1 本身也起稳定作用。

**局限**：RBSA 反推的是"与净值共变的行业组合"，不等于真实持仓；风格漂移
（持仓变更）会让窗口内权重成为两段持仓的混合。仅作监控信号，不替代季报持仓。
"""

import logging

import numpy as np

from app.features.sector import style_returns_matrix

logger = logging.getLogger(__name__)

# 默认回归窗口（交易日）
_WINDOW = 60

# 默认岭正则强度：相对板块收益方差的比例（尺度无关），仅用于抑制过拟合
_DEFAULT_RIDGE = 0.05
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


def compute_fund_style(fund_code: str, window: int = _WINDOW,
                       as_of: str | None = None) -> dict | None:
    """用最近 window 个交易日（截至 as_of，默认最新）净值反推基金风格暴露并落库。

    as_of：回填历史反推用（如持仓建仓日）——取 as_of 当日及之前最近 window+1 条净值，
    使监控 R2 能对比“建仓日反推 vs 当前反推”（同体系，避免季报/反推跨体系权重失真）。

    返回 {trade_date, industry_1, weight_1, industry_2, weight_2, r_squared}；
    数据不足（净值 < window+1 条 / 完整板块 < 2 个）返回 None（优雅降级，不报错）。
    只保留在窗口内**每个交易日**都有数据的板块，避免缺失日造成收益错位。
    """
    import app.repo as repo

    navs = repo.nav.series(fund_code, limit=window + 1, until=as_of)
    if len(navs) < window + 1:
        return None
    dates = [d for d, _ in navs]
    vals = [v for _, v in navs]
    if any(v is None or v <= 0 for v in vals):
        return None
    fund_ret = np.array([vals[i] / vals[i - 1] - 1.0 for i in range(1, len(vals))])
    ret_dates = dates[1:]

    by_sector: dict[str, dict[str, float]] = {}
    for d, _code, name, pct in repo.get_sector_pct_series(ret_dates[0], ret_dates[-1]):
        # 深模块（features/sector.py）按板块名分组做 ÷100 与全日期覆盖过滤
        by_sector.setdefault(name, {})[d] = pct
    m = style_returns_matrix(ret_dates, by_sector=by_sector)
    if m is None:
        return None
    R, names = m
    w, r2 = solve_style_weights(fund_ret, R)
    top = top_industries(w, names, k=2)
    if not top:
        return None
    # 权重统一为百分数（×100）：与季报 rbsa_weight_1 同口径，供监控 R2 对比
    top_pct = [(n, wgt * 100.0) for n, wgt in top]
    repo.save_fund_style(fund_code, dates[-1], top_pct, r2)
    return {
        "trade_date": dates[-1],
        "industry_1": top_pct[0][0],
        "weight_1": round(top_pct[0][1], 2),
        "industry_2": top_pct[1][0] if len(top_pct) > 1 else None,
        "weight_2": round(top_pct[1][1], 2) if len(top_pct) > 1 else None,
        "r_squared": round(r2, 4),
    }


def update_all_fund_styles(codes: list[str] | None = None,
                           window: int = _WINDOW) -> int:
    """批量反推（默认持仓基金），返回成功条数。"""
    import app.repo as repo

    if codes is None:
        # get_holding_codes 返回持仓 dict 列表（含 code/name/reco_date…），此处只要代码
        codes = [h["code"] for h in repo.get_holding_codes() if h.get("code")]
    n = 0
    for c in codes:
        try:
            if compute_fund_style(c, window) is not None:
                n += 1
        except Exception as e:
            logger.warning("风格反推失败 %s: %s", c, str(e)[:80])
    return n

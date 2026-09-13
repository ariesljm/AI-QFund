"""净值反推风格暴露编排（RBSA，ticket 05）。

用基金日收益率对板块日收益率矩阵做**约束最小二乘**回归，反推当前行业暴露权重。
**求解器在 features/style_solve（ADR-0007 豁免边界内，纯 numpy 零依赖）**——
本模块只做取数/落库编排（repo seam + 板块收益矩阵深模块），不实现回归数学。

**局限**：RBSA 反推的是"与净值共变的行业组合"，不等于真实持仓；风格漂移
（持仓变更）会让窗口内权重成为两段持仓的混合。仅作监控信号，不替代季报持仓。
"""

import logging

import numpy as np

from app.features.sector import style_returns_matrix
from app.features.style_solve import solve_style_weights, top_industries
from app.repo.decision import get_sector_pct_map

logger = logging.getLogger(__name__)

# 默认回归窗口（交易日）
_WINDOW = 60


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

    by_sector = get_sector_pct_map(ret_dates[0], ret_dates[-1])
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

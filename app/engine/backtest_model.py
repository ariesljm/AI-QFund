"""模型级 walk-forward 回测（spec 终极验收的模型级闭环 A3 + λ 标定 B）。

样本与生产训练完全同口径（现算特征含 style_r2 + 市场状态注入 + 时间衰减权重），
一次构建、多模型复用，避免 style_r2 反推（主要计算成本）重复计算：

- A3：style_r2 进特征与否，同一验证集 40 日赚钱胜率对比（预测分高低分组胜率）
- B：RISK_ADJ_DD_LAMBDA ∈ {0.3, 0.5, 0.7, 1.0} 标签重训，验证集胜率/IC 选最优 λ

用法：python -m app.engine.backtest_model [max_funds]
"""

import logging

import numpy as np
import pandas as pd
import lightgbm as lgb

import app
from app import domain
from app.features.stats import spearman as stats_spearman
from app.model import _FORWARD_WINDOW, get_lgb_params, panel_samples

logger = logging.getLogger(__name__)

STYLE_R2 = "style_r2"
LAMBDAS = [0.3, 0.5, 0.7, 1.0]


def _feat_cols(with_r2: bool) -> list[str]:
    cols = [c for c in domain.FEATURE_COLS if with_r2 or c != STYLE_R2]
    return cols + domain.MARKET_COLS


def _metrics(scores: np.ndarray, y_abs: np.ndarray) -> dict:
    """高分组（预测分≥中位数）vs 低分组的 40 日实际胜率 + Spearman IC。"""
    s = np.asarray(scores, dtype=float)
    y = np.asarray(y_abs, dtype=float)
    med = float(np.median(s))
    hi, lo = y[s >= med], y[s < med]
    hi_wr = float((hi > 0).mean()) if hi.size else float("nan")
    lo_wr = float((lo > 0).mean()) if lo.size else float("nan")
    res = stats_spearman(s, y)
    ic = res[0] if res is not None else float("nan")
    return {"n": int(s.size), "高分组胜率": hi_wr, "低分组胜率": lo_wr,
            "胜率差(+pp)": (hi_wr - lo_wr) * 100, "IC": ic}


def _train_eval(samples: list, split: int, with_r2: bool,
                lam: float) -> tuple[dict, list[str]]:
    """训练（前 split 样本，时间衰减权重）→ 验证集（后段）胜率/IC。不落盘。"""
    train_s, val_s = samples[:split], samples[split:]
    if len(train_s) < 200 or len(val_s) < 50:
        return _metrics(np.array([]), np.array([])), []
    t_max = train_s[-1][0]
    cols = _feat_cols(with_r2)
    X_tr = pd.DataFrame([s[1] for s in train_s])[cols].astype(float)
    y_tr = pd.Series([s[3][lam] for s in train_s], dtype=float)
    w_tr = np.array([np.exp(-(t_max - s[0]).days / 90.0) for s in train_s], dtype=float)
    X_va = pd.DataFrame([s[1] for s in val_s])[cols].astype(float)
    y_abs = np.array([s[2] for s in val_s], dtype=float)
    params = get_lgb_params()
    booster = lgb.train(params, lgb.Dataset(X_tr, label=y_tr, weight=w_tr),
                        num_boost_round=50)
    scores = booster.predict(X_va)
    return _metrics(scores, y_abs), cols


def run(max_funds: int = 2000, window_days: int = 365) -> str:
    """A3（r2 有无）+ B（λ 扫描）实验，返回文本报告。"""
    import app.repo as repo
    codes = repo.get_train_fund_codes(60 + _FORWARD_WINDOW, max_funds)
    logger.info("样本构建: %d 只基金（style_r2 反推为主成本）", len(codes))
    samples = panel_samples(codes, window_days=window_days,
                            lambdas=tuple(LAMBDAS))
    if len(samples) < 300:
        return f"样本不足（{len(samples)}），无法评估"
    samples.sort(key=lambda s: s[0])
    split = int(len(samples) * 0.8)
    lines = [
        "=== 模型级 walk-forward 回测（有/无 style_r2 × λ 扫描）===",
        f"样本: {len(samples)} 条（{len(codes)} 基金 × 决策日步 20） | "
        f"验证集: {len(samples) - split} 条（最新 20%） | 前瞻: {_FORWARD_WINDOW} 交易日",
        f"验证期: {samples[split][0].strftime('%Y-%m-%d')} ~ {samples[-1][0].strftime('%Y-%m-%d')}",
        "",
        "--- A3: style_r2 进特征对 40 日赚钱胜率的影响（λ=0.5 标签）---",
        "口径: 验证集按模型预测分分组，高分组=预测分≥中位数，胜率=40 日实际收益>0 占比",
    ]
    for name, with_r2 in [("含 style_r2", True), ("不含 style_r2", False)]:
        m, cols = _train_eval(samples, split, with_r2, 0.5)
        lines.append(
            f"{name:<10} 特征{len(cols)}维 | n={m['n']} | 高分组胜率 {m['高分组胜率']:.1%}"
            f" | 低分组 {m['低分组胜率']:.1%} | 胜率差 {m['胜率差(+pp)']:+.1f}pp | IC {m['IC']:+.4f}")
    lines.append("")
    lines.append("--- B: λ 标定（含 style_r2 特征，λ=回撤惩罚系数）---")
    for lam in LAMBDAS:
        m, _ = _train_eval(samples, split, True, lam)
        tag = "  ← 生产当前值" if abs(lam - domain.RISK_ADJ_DD_LAMBDA) < 1e-9 else ""
        lines.append(
            f"λ={lam:.1f}{tag:<10} 高分组胜率 {m['高分组胜率']:.1%}"
            f" | 低分组 {m['低分组胜率']:.1%} | 胜率差 {m['胜率差(+pp)']:+.1f}pp | IC {m['IC']:+.4f}")
    best = max(LAMBDAS, key=lambda lam: _train_eval(samples, split, True, lam)[0]["IC"])
    lines.append("")
    lines.append(f"λ 推荐: IC 最优 {best:.1f}"
                 + ("（维持生产 0.5 不变）" if abs(best - domain.RISK_ADJ_DD_LAMBDA) < 1e-9
                    else f"（当前 {domain.RISK_ADJ_DD_LAMBDA:.1f} 需更新）"))
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    import time
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    t0 = time.perf_counter()
    max_funds = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
    print(run(max_funds=max_funds))
    print(f"耗时 {round(time.perf_counter() - t0)}s")
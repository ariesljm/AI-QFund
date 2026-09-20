"""票 03 因子权重研究：滚动 IC 加权 + 正交化 + regime 分层诊断。

复用票 01 的 harness（backtest_robustness 的数据采集与 IC 原语），不重写公式。

三组离线对照（全部扣费 0.65%，同一 net_fwd 标签）：
1. regime 分层 IC：用 sh000300 bias_60d（±8%）切牛/熊/震荡，看各因子在哪个 regime 有效。
   ——诊断镜头，不是择时开关（Q3 不择时立场，永不接回生产）。
2. 滚动 IC 加权（expanding，无未来函数）vs 生产等权：看动态权重是否救回被稀释的 mom_250d。
3. 正交化：mom_250d 对 sharpe_60d 截面回归取残差，看残差 IC 是否提升。

结论分支：动态加权/正交化无显著增益 → 维持等权钉死现状；某因子熊市翻负 → 提调权方案。

用法：`python scripts/factor_weighting_study.py [funds] [step]`
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

from app.database import db_conn
from app.repo import base as repo
from app.features.stats import spearman, t_tail_p
import backtest_robustness as bt   # 复用票 01 harness
from app.features.calculator import nav_score_factors

# 打分三因子（与生产 multifactor_scores 同集）；ttr 反向（低=好）→ 用 1−rank 对齐
SCORE_FACTORS = ["sharpe_60d", "mom_250d", "ttr_60d"]
REGIME_BULL, REGIME_BEAR, REG_THRESH = "牛", "熊", 8.0


def index_bias_60d() -> dict[str, float]:
    """sh000300 每日 bias_60d = (close − MA60)/MA60×100。"""
    rows = repo.get_index_rows("sh000300")
    out = {}
    for i, (d, c, _v) in enumerate(rows):
        if i < 59 or not c:
            continue
        ma = float(np.mean([rows[j][1] for j in range(i - 59, i + 1)]))
        out[d] = (c - ma) / ma * 100.0 if ma > 0 else 0.0
    return out


def tag_regime(bias: float) -> str:
    if bias > REG_THRESH:
        return REGIME_BULL
    if bias < -REG_THRESH:
        return REGIME_BEAR
    return "震荡"


def per_factor_regime_ic(recs: list[dict], bias_map: dict[str, float]) -> dict:
    """逐 regime 逐因子截面 IC（与 net_fwd）。"""
    buckets = {"牛": [], "熊": [], "震荡": []}
    for r in recs:
        b = bias_map.get(r["t"])
        if b is None:
            continue
        buckets[tag_regime(b)].append(r)
    out = {}
    for reg, rs in buckets.items():
        out[reg] = {"n_dates": len({r["t"] for r in rs}),
                    "n": len(rs)}
        for f in SCORE_FACTORS:
            ics = bt.cross_sectional_ic(rs, f)
            out[reg][f] = bt.ic_stats(ics)
    return out


def _rank_pct(x: np.ndarray) -> np.ndarray:
    n = len(x)
    if n <= 1:
        return np.zeros_like(x)
    return np.argsort(np.argsort(x)) / (n - 1)


def weighted_score(feat: dict[str, dict], weights: dict[str, float]) -> dict[str, float]:
    """加权截面打分：sum_f w_f × rank_pct(f)，ttr 用 1−rank 对齐（低=好）。"""
    codes = [c for c in feat if feat[c]]
    if len(codes) <= 1:
        return {}
    shp = np.array([feat[c].get("sharpe_60d") or 0.0 for c in codes])
    mom = np.array([feat[c].get("mom_250d") or 0.0 for c in codes])
    ttr = np.array([feat[c].get("ttr_60d") if feat[c].get("ttr_60d") is not None else 60.0
                    for c in codes])
    rs, rm, rt = _rank_pct(shp), _rank_pct(mom), _rank_pct(ttr)
    w = weights
    score = (w.get("sharpe_60d", 0) * rs + w.get("mom_250d", 0) * rm
             + w.get("ttr_60d", 0) * (1.0 - rt))
    return {c: float(s) for c, s in zip(codes, score, strict=True)}


def score_ic_with(recs: list[dict], scorer) -> list[tuple[str, float]]:
    """给定打分函数 scorer(date, feats) → 逐日截面 IC（与 net_fwd）。"""
    by_date: dict[str, dict[str, dict]] = {}
    for r in recs:
        by_date.setdefault(r["t"], {})[r["code"]] = r["feat"]
    out = []
    for d in sorted(by_date):
        feats = by_date[d]
        if len(feats) < 10:
            continue
        scores = scorer(d, feats)
        pairs = [(scores.get(r["code"]), r["net_fwd"]) for r in recs
                 if r["t"] == d and r["code"] in scores]
        pairs = [(s, y) for s, y in pairs if s is not None]
        if len(pairs) < 10:
            continue
        sp = spearman([p[0] for p in pairs], [p[1] for p in pairs])
        if sp is not None:
            out.append((d, sp[0]))
    return out


def rolling_ic_weights(recs: list[dict]) -> dict[str, float]:
    """expanding：截至 T 之前所有日期的逐因子截面 IC 均值 → max(0,·) 归一化权重。"""
    dates = sorted({r["t"] for r in recs})
    prior_ic = {f: [] for f in SCORE_FACTORS}
    weights_by_date: dict[str, dict[str, float]] = {}
    for d in dates:
        w = {f: max(0.0, float(np.mean(v))) if v else 0.0 for f, v in prior_ic.items()}
        tot = sum(w.values())
        if tot <= 0:
            w = {f: 1 / 3 for f in SCORE_FACTORS}     # 无信号 → 等权兜底
        else:
            w = {f: v / tot for f, v in w.items()}
        weights_by_date[d] = w
        # 用当日截面更新 prior（无未来函数：weight 已算完才喂入）
        for f in SCORE_FACTORS:
            ics = bt.cross_sectional_ic([r for r in recs if r["t"] == d], f)
            if ics:
                prior_ic[f].append(ics[0][1])
    return weights_by_date


def orthogonal_mom_ic(recs: list[dict]) -> list[tuple[str, float]]:
    """mom_250d 对 sharpe_60d 截面回归取残差 → 残差 IC vs net_fwd（逐日）。"""
    by_date: dict[str, list[tuple[float, float, float]]] = {}
    for r in recs:
        f = r["feat"]
        shp, mom = f.get("sharpe_60d"), f.get("mom_250d")
        if shp is None or mom is None:
            continue
        by_date.setdefault(r["t"], []).append((shp, mom, r["net_fwd"]))
    out = []
    for d in sorted(by_date):
        rows = by_date[d]
        if len(rows) < 20:
            continue
        shp = np.array([x[0] for x in rows])
        mom = np.array([x[1] for x in rows])
        y = np.array([x[2] for x in rows])
        # 残差：mom − OLS(mom ~ shp)
        X = np.column_stack([np.ones(len(rows)), shp])
        coef, *_ = np.linalg.lstsq(X, mom, rcond=None)
        resid = mom - X @ coef
        sp = spearman(resid, y)
        if sp is not None:
            out.append((d, sp[0]))
    return out


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
    step = int(sys.argv[2]) if len(sys.argv) > 2 else 12
    start = sys.argv[3] if len(sys.argv) > 3 else "2023-06-01"
    end = sys.argv[4] if len(sys.argv) > 4 else "2026-04-01"
    since = sys.argv[5] if len(sys.argv) > 5 else "2022-01-01"
    codes, navs = bt.sample_active_equity(n, since=since)
    print(f"采集中（nav_score_factors 轻量路径，约 1-2 分钟）...", flush=True)
    recs = bt.run(start, end, step, codes, navs,
                  feat_fn=nav_score_factors)
    print(f"样本 {len(recs)} 条 / {len({r['t'] for r in recs})} 决策日\n", flush=True)

    bias = index_bias_60d()

    # 1) regime 分层 IC
    print("=== regime 分层 IC（bias_60d ±8%）===")
    reg = per_factor_regime_ic(recs, bias)
    for rname in ("牛", "震荡", "熊"):
        g = reg.get(rname, {})
        print(f"\n[{rname}] 日期={g.get('n_dates')}  样本={g.get('n')}")
        for f in SCORE_FACTORS:
            s = g.get(f, {})
            if s.get("n", 0) == 0:
                continue
            print(f"  {f:<12} meanIC={s['mean']:+.4f} t={s['t']:+.2f} p={s['p']:.3g}")

    # 2) 滚动 IC 加权 vs 等权
    print("\n=== 滚动 IC 加权（expanding，无前视）vs 生产等权 ===")
    eq_ic = bt.ic_stats(score_ic_with(recs, lambda d, f: bt.multifactor_scores(f)))
    wmap = rolling_ic_weights(recs)
    last_w = wmap[sorted(wmap)[-1]]
    print(f"最末日期权重: sharpe={last_w['sharpe_60d']:.3f} "
          f"mom={last_w['mom_250d']:.3f} ttr={last_w['ttr_60d']:.3f}")
    dyn_ic = bt.ic_stats(score_ic_with(
        recs, lambda d, f: weighted_score(f, wmap.get(d, last_w))))
    print(f"等权:   meanIC={eq_ic['mean']:+.4f} t={eq_ic['t']:+.2f} p={eq_ic['p']:.3g}"
          f"  CI=[{eq_ic['ci_lo']:+.4f},{eq_ic['ci_hi']:+.4f}]")
    print(f"滚动加权: meanIC={dyn_ic['mean']:+.4f} t={dyn_ic['t']:+.2f} p={dyn_ic['p']:.3g}"
          f"  CI=[{dyn_ic['ci_lo']:+.4f},{dyn_ic['ci_hi']:+.4f}]")

    # 3) 正交化
    print("\n=== 正交化：mom_250d 残差（对 sharpe 回归）IC vs 原始 mom_250d IC ===")
    raw_mom = bt.ic_stats(bt.cross_sectional_ic(recs, "mom_250d"))
    orth_mom = bt.ic_stats(orthogonal_mom_ic(recs))
    print(f"原始 mom_250d: meanIC={raw_mom['mean']:+.4f} t={raw_mom['t']:+.2f} p={raw_mom['p']:.3g}")
    print(f"正交残差:       meanIC={orth_mom['mean']:+.4f} t={orth_mom['t']:+.2f} p={orth_mom['p']:.3g}")


if __name__ == "__main__":
    main()

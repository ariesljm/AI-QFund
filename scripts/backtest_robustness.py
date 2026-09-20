"""票 01 回测地基加固：现状 3 因子排序扣费后 OOS IC 稳健性审计。

回答一个问题：**现状 3 因子排序（sharpe_60d + mom_250d + ttr_60d 反向，等权百分位）
在扣费后的样本外，是否仍有稳定预测力。**

单一来源（不重写公式）：
- 特征：`app.features.calculator.compute_fund_features`（生产同款；dummy 指数只影响
  capture_up/down/style_r2，不影响打分三因子 sharpe/mom/ttr）
- 打分：`app.engine.screen_pipeline.multifactor_scores`（生产同款）
- 前瞻收益：`app.repo.nav.forward_return_from_navs`（生产同款）

口径：
- 扣费 0.65%（120d 实际费率 ≈ 赎 0.5% + 申 0.15% 前端）；net_fwd = fwd − 0.0065
- PIT：因子只用 ≤T 的净值（cum_nav），标签用 T → T+120 满窗口净值
- 时间切分：前半段 = 因子选择期（原 120d 报告的样本），后半段 = 伪 OOS。
  ⚠️ 因原报告已看过全样本，后半段只是"时间稳健性"而非干净 OOS，报告中必须标注。

用法：`python scripts/backtest_robustness.py [funds] [step]`
"""

import os
import sys

# 确保项目根目录在 sys.path 上（直接 python scripts/xxx.py 运行时）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from app import domain
from app.database import db_conn
from app.engine.screen_pipeline import multifactor_scores
from app.features.calculator import compute_fund_features
from app.features.stats import spearman, t_tail_p
from app.repo.nav import forward_return_from_navs

COST = 0.0065          # 120d 实际费率（赎 0.5% + 申 0.15% 前端）
FORWARD = domain.FORWARD_DAYS  # 120

# 因子扫描候选集（与 120d 报告 §三 对齐，全部净值可算；方向"低=好"的因子在 IC 上反号）
SCAN_FACTORS = ["sharpe_60d", "sortino_60d", "mom_250d", "mom_60d",
                "momentum_20d", "ttr_60d", "vol_20d", "reversal_20d"]


def load_navs(codes: list[str], since: str) -> dict[str, list[tuple[str, float]]]:
    """批量预加载净值（单查询，避免 N+1）。返回 {code: [(date, cum_nav), ...] 升序}。"""
    q = "SELECT code, date, cum_nav FROM fund_nav WHERE date >= ? ORDER BY code, date"
    out: dict[str, list[tuple[str, float]]] = {}
    with db_conn() as c:
        for code, date, nav in c.execute(q, (since,)):
            out.setdefault(code, []).append((date, float(nav)))
    return out


def sample_active_equity(n: int, since: str, seed: int = 7) -> list[str]:
    """从主动权益池（混合型+股票型，剔指数型）抽 n 只，要求 since 前有净值历史。"""
    with db_conn() as c:
        types = c.execute(
            "SELECT code, type FROM fund_basic WHERE type IN ('混合型','股票型')").fetchall()
    codes = [code for code, _t in types]
    navs = load_navs(codes, since)
    codes = [c for c in codes if len(navs.get(c, [])) > 0]
    rng = np.random.default_rng(seed)
    chosen = rng.choice(codes, size=min(n, len(codes)), replace=False).tolist()
    return chosen, {c: navs[c] for c in chosen}


def _pos_at(dates: list[str], t: str) -> int:
    """最后一个 <= t 的下标（dates 升序）；无则 -1。"""
    import bisect
    return bisect.bisect_right(dates, t) - 1


def run(start: str, end: str, step: int, codes: list[str],
        navs_map: dict[str, list[tuple[str, float]]],
        feat_fn=None) -> list[dict]:
    """主循环：T × 基金 → PIT 因子 + 扣费前瞻收益。

    feat_fn: 特征计算函数（vals → dict|None）；缺省 compute_fund_features（全量，
    供因子扫描）。回测打分研究可传 nav_score_factors（仅打分三因子，快 3×）。
    """
    with db_conn() as c:
        idx_rows = c.execute(
            "SELECT date FROM index_daily WHERE code='sh000300' ORDER BY date").fetchall()
    trade_dates = [r[0] for r in idx_rows]
    ts = [d for d in trade_dates if start <= d <= end][::step]
    recs: list[dict] = []
    for t in ts:
        for code in codes:
            rows = navs_map[code]
            dates = [d for d, _ in rows]
            pos = _pos_at(dates, t)
            if pos < 0 or pos + FORWARD >= len(rows):
                continue
            vals = np.array([v for _, v in rows[:pos + 1]], dtype=float)
            if len(vals) < 62:      # 生产硬过滤：净值历史 <62 条剔除
                continue
            # PIT 特征：feat_fn 轻量路径（仅打分三因子）或全量 compute_fund_features
            if feat_fn is not None:
                feat = feat_fn(vals)
            else:
                dummy = np.ones(len(vals))
                feat = compute_fund_features(vals, dummy, dummy)
            if feat is None:
                continue
            fwd = forward_return_from_navs([v for _, v in rows], pos, FORWARD)
            if fwd is None:
                continue
            recs.append({"t": t, "code": code, "feat": feat,
                         "net_fwd": fwd - COST})
    return recs


def cross_sectional_ic(recs: list[dict], field: str) -> list[tuple[str, float]]:
    """逐日截面 Spearman IC（field 与 net_fwd）。返回 [(date, ic), ...]。"""
    by_date: dict[str, list[tuple[float, float]]] = {}
    for r in recs:
        by_date.setdefault(r["t"], []).append((r["feat"][field], r["net_fwd"]))
    out = []
    for d in sorted(by_date):
        pairs = by_date[d]
        x = [p[0] for p in pairs]
        y = [p[1] for p in pairs]
        if len(pairs) < 10:
            continue
        sp = spearman(x, y)
        if sp is not None:
            out.append((d, sp[0]))
    return out


def score_ic(recs: list[dict]) -> list[tuple[str, float]]:
    """组合分（生产 multifactor_scores）逐日截面 IC。"""
    by_date: dict[str, dict[str, dict]] = {}
    for r in recs:
        by_date.setdefault(r["t"], {})[r["code"]] = r["feat"]
    out = []
    for d in sorted(by_date):
        feats = by_date[d]
        if len(feats) < 10:
            continue
        scores = multifactor_scores(feats)
        # 与同日的 net_fwd 对齐
        pairs = [(scores[r["code"]], r["net_fwd"]) for r in recs if r["t"] == d and r["code"] in scores]
        x = [p[0] for p in pairs]
        y = [p[1] for p in pairs]
        if len(pairs) < 10:
            continue
        sp = spearman(x, y)
        if sp is not None:
            out.append((d, sp[0]))
    return out


def ic_stats(ics: list[tuple[str, float]]) -> dict:
    """IC 序列 → 均值 / t 统计量 / p / bootstrap 95% CI（按日期重采样）。"""
    vals = np.array([v for _, v in ics])
    n = len(vals)
    if n == 0:
        return {"n": 0}
    mean = float(vals.mean())
    sd = float(vals.std(ddof=1)) if n > 1 else 0.0
    t = mean / (sd / np.sqrt(n)) if sd > 1e-12 else float("nan")
    p = float(t_tail_p(np.array([t]), n - 1)[0]) if sd > 1e-12 else 1.0
    rng = np.random.default_rng(42)
    boots = [float(rng.choice(vals, size=n, replace=True).mean()) for _ in range(2000)]
    lo, hi = float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))
    return {"n": n, "mean": mean, "t": t, "p": p, "ci_lo": lo, "ci_hi": hi}


def time_split_ic(ics: list[tuple[str, float]], split_frac: float = 0.6,
                  purge: int = 5) -> tuple[dict, dict]:
    """按日期顺序切 IS / OOS（中间留 purge 日期隔离带）。"""
    ics = sorted(ics)
    n = len(ics)
    is_end = int(n * split_frac)
    oos_start = is_end + purge
    if oos_start >= n:
        oos_start = n
    return ic_stats(ics[:is_end]), ic_stats(ics[oos_start:])


def bh_fdr(pvals: list[tuple[str, float]], q: float = 0.10) -> list[str]:
    """Benjamini-Hochberg FDR；返回 (因子, p) 按 p 升序，标注 q 阈值下存活。"""
    order = sorted(pvals, key=lambda x: x[1])
    m = len(order)
    surv = []
    for i, (name, p) in enumerate(order, start=1):
        if p <= q * i / m:
            surv.append(name)
    return surv


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    n_funds = int(sys.argv[1]) if len(sys.argv) > 1 else 1200
    step = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    start = sys.argv[3] if len(sys.argv) > 3 else "2023-06-01"
    end = sys.argv[4] if len(sys.argv) > 4 else "2026-04-01"
    since = sys.argv[5] if len(sys.argv) > 5 else "2022-01-01"

    print(f"抽样 {n_funds} 只主动权益基金 / 步长 {step} 交易日 / {start} ~ {end}", flush=True)
    codes, navs_map = sample_active_equity(n_funds, since=since)
    print(f"有效样本基金: {len(codes)} 只", flush=True)

    recs = run(start, end, step, codes, navs_map)
    print(f"T×基金 样本: {len(recs)} 条 / 覆盖 {len({r['t'] for r in recs})} 个决策日", flush=True)

    # 组合分 IC（核心结论）
    sics = score_ic(recs)
    st = ic_stats(sics)
    print("\n=== 组合分（生产 multifactor_scores，现 mom0.7/sharpe0.3/ttr退役）截面 IC（扣费后）===")
    print(f"日期数={st['n']}  均值={st['mean']:+.4f}  t={st['t']:+.2f}  p={st['p']:.4g}"
          f"  bootstrap95%CI=[{st['ci_lo']:+.4f}, {st['ci_hi']:+.4f}]")

    st_is, st_oos = time_split_ic(sics)
    print(f"\nIS（前 60% 日期）: mean={st_is['mean']:+.4f} t={st_is['t']:+.2f} p={st_is['p']:.4g}")
    print(f"伪OOS（后段日期）: mean={st_oos['mean']:+.4f} t={st_oos['t']:+.2f} p={st_oos['p']:.4g}"
          f"  n={st_oos['n']}")

    # 因子扫描 + BH-FDR
    print("\n=== 因子扫描（逐因子截面 IC 扣费后 + BH-FDR）===")
    print(f"{'因子':<14}{'均值IC':>9}{'t':>7}{'p':>9}  方向")
    rows = []
    for f in SCAN_FACTORS:
        ics = cross_sectional_ic(recs, f)
        s = ic_stats(ics)
        if s.get("n", 0) == 0:
            continue
        direction = "低=好(反号)" if f in ("ttr_60d", "vol_20d") else "高=好"
        print(f"{f:<14}{s['mean']:>+9.4f}{s['t']:>+7.2f}{s['p']:>9.4g}  {direction}")
        rows.append((f, s["p"]))
    for q in (0.05, 0.10):
        surv = bh_fdr(rows, q)
        print(f"BH-FDR q={q}: 存活 = {surv if surv else '（无）'}")


if __name__ == "__main__":
    main()

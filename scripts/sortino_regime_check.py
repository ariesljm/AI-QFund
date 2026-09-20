"""sortino vs sharpe 的 regime 分层 IC（回答"索提诺真的不需要吗"）。

假设：sharpe 震荡市失效因惩罚上行波动；sortino 只罚下行 → 可能震荡市不失效。
若 sortino 在牛市+震荡都显著而 sharpe 只在牛市显著 → sortino 是 regime-稳健版，
应替换 sharpe 进打分（而非因"共线"丢弃）。

⚠️ 局部复算 sharpe/sortino/mom（与 nav_score_factors 同口径；sortino 非生产打分因子，
nav_score_factors 不含它，故此处诊断脚本本地算）。生产单一来源仍为 compute_fund_features。

用法：`python scripts/sortino_regime_check.py [funds] [step]`
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

from app.database import db_conn
from app.repo.nav import forward_return_from_navs
from app.features.stats import spearman
import backtest_robustness as bt

COST = bt.COST
FORWARD = bt.FORWARD
REG_THRESH = 8.0


def factors(navs: np.ndarray) -> dict | None:
    """sharpe / sortino / mom（与 compute_fund_features 同口径，诊断复算）。"""
    if len(navs) < 60:
        return None
    with np.errstate(divide="ignore", invalid="ignore"):
        returns = np.diff(navs) / navs[:-1]
    returns = returns[np.isfinite(returns)]
    feat = {"mom_250d": float((navs[-1] / navs[-251] - 1) * 100) if len(navs) >= 251 else 0.0}
    if len(returns) >= 60:
        r60 = returns[-60:]
        ann_ret = float(r60.mean() * 252)
        sd = float(r60.std() * np.sqrt(252))
        feat["sharpe_60d"] = ann_ret / sd if sd > 1e-10 else 0.0
        neg = r60[r60 < 0]
        dsd = float(neg.std() * np.sqrt(252)) if neg.size > 0 else 0.0
        feat["sortino_60d"] = ann_ret / dsd if dsd > 1e-10 else 0.0
    else:
        feat["sharpe_60d"] = feat["sortino_60d"] = 0.0
    return feat


def index_bias_60d():
    rows = bt._index_rows() if hasattr(bt, "_index_rows") else None
    with db_conn() as c:
        rows = c.execute("SELECT date, close FROM index_daily WHERE code='sh000300' ORDER BY date").fetchall()
    out = {}
    for i, (d, cl) in enumerate(rows):
        if i < 59 or not cl:
            continue
        ma = float(np.mean([rows[j][1] for j in range(i - 59, i + 1)]))
        out[d] = (cl - ma) / ma * 100.0 if ma > 0 else 0.0
    return out


def tag(b):
    if b > REG_THRESH:
        return "牛"
    if b < -REG_THRESH:
        return "熊"
    return "震荡"


def run(start, end, step, codes, navs_map):
    with db_conn() as c:
        idx = [r[0] for r in c.execute(
            "SELECT date FROM index_daily WHERE code='sh000300' ORDER BY date").fetchall()]
    ts = [d for d in idx if start <= d <= end][::step]
    recs = []
    for t in ts:
        for code in codes:
            rows = navs_map[code]
            pos = bt._pos_at([d for d, _ in rows], t)
            if pos < 0 or pos + FORWARD >= len(rows):
                continue
            vals = np.array([v for _, v in rows[:pos + 1]], dtype=float)
            if len(vals) < 62:
                continue
            f = factors(vals)
            if f is None:
                continue
            fwd = forward_return_from_navs([v for _, v in rows], pos, FORWARD)
            if fwd is None:
                continue
            recs.append({"t": t, "code": code, "feat": f, "net_fwd": fwd - COST})
    return recs


def regime_ic(recs, field, bias):
    buckets = {"牛": [], "震荡": [], "熊": []}
    for r in recs:
        b = bias.get(r["t"])
        if b is None:
            continue
        buckets[tag(b)].append(r)
    out = {}
    for reg, rs in buckets.items():
        by_date = {}
        for r in rs:
            by_date.setdefault(r["t"], []).append((r["feat"][field], r["net_fwd"]))
        ics = []
        for d in sorted(by_date):
            p = by_date[d]
            if len(p) < 10:
                continue
            sp = spearman([x[0] for x in p], [x[1] for x in p])
            if sp:
                ics.append(sp[0])
        if not ics:
            out[reg] = None
            continue
        a = np.array(ics)
        n = len(a)
        mean = float(a.mean())
        sd = float(a.std(ddof=1)) if n > 1 else 0.0
        tval = mean / (sd / np.sqrt(n)) if sd > 1e-12 else float("nan")
        from app.features.stats import t_tail_p
        p = float(t_tail_p(np.array([tval]), n - 1)[0]) if sd > 1e-12 else 1.0
        out[reg] = {"n": n, "mean": mean, "t": tval, "p": p}
    return out


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1500
    step = int(sys.argv[2]) if len(sys.argv) > 2 else 12
    start = sys.argv[3] if len(sys.argv) > 3 else "2023-06-01"
    end = sys.argv[4] if len(sys.argv) > 4 else "2026-04-01"
    since = sys.argv[5] if len(sys.argv) > 5 else "2022-01-01"
    codes, navs = bt.sample_active_equity(n, since=since)
    print(f"抽样 {len(codes)} 只；采集中...", flush=True)
    recs = run(start, end, step, codes, navs)
    print(f"样本 {len(recs)} 条 / {len({r['t'] for r in recs})} 决策日\n", flush=True)
    bias = index_bias_60d()
    print("=== sharpe_60d vs sortino_60d 的 regime 分层 IC ===")
    print(f"{'regime':<8}{'因子':<14}{'n日':>5}{'均值IC':>10}{'t':>8}{'p':>9}")
    for reg in ("牛", "震荡", "熊"):
        for f in ("sharpe_60d", "sortino_60d"):
            s = regime_ic(recs, f, bias)
            r = s.get(reg)
            if r is None:
                print(f"{reg:<8}{f:<14}{'-':>5}{'-':>10}{'-':>8}{'-':>9}  (无样本)")
                continue
            sig = " ★" if (r["p"] < 0.1 and r["mean"] > 0) else ""
            print(f"{reg:<8}{f:<14}{r['n']:>5}{r['mean']:>+10.4f}{r['t']:>+8.2f}{r['p']:>9.3g}{sig}")
    # 全样本对比
    print("\n=== 全样本（不分 regime）===")
    for f in ("sharpe_60d", "sortino_60d"):
        s = regime_ic(recs, f, {d: 0.0 for d in bias})  # 全归"震荡"=全样本
        r = s.get("震荡")
        if r:
            print(f"{f:<14} meanIC={r['mean']:+.4f} t={r['t']:+.2f} p={r['p']:.3g}  n={r['n']}")


if __name__ == "__main__":
    main()

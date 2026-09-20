"""票 02 组合层验证：分散约束 Top5 vs 纯分数 Top5（扣费后组合收益/风险对比）。

复用票 01/03 harness：nav_score_factors 轻量特征 + multifactor_scores（现 mom0.7/sharpe0.3）
+ forward_return_from_navs（扣费 0.65%）+ select_diversified（贪心去相关 + 同类≤2）。

每决策日：全市场算分 → Top30 候选 → 两条路：纯分数 Top5 / 分散 Top5；
对比组合均收益、收益标准差（风险代理）、两者重合度。

⚠️ 局限：rbsa_industry_1 用 fund_features 最新快照（PIT 泄漏，RBSA 第一行业较稳定，
影响小）；相关性用最近 60 交易日净值收益（与特征窗口一致）。

用法：`python scripts/portfolio_diversify_study.py [funds] [step]`
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

from app.database import db_conn
from app.engine.screen import select_diversified
from app.engine.screen_pipeline import multifactor_scores
from app.features.calculator import nav_score_factors
from app.repo.nav import forward_return_from_navs
import backtest_robustness as bt

COST = bt.COST
FORWARD = bt.FORWARD
MAX_CORR = 0.85
MAX_SAME_PEER = 2


def load_rbsa_peer(codes: list[str]) -> dict[str, str | None]:
    """每 code 的 rbsa_industry_1（最新快照，PIT 泄漏见模块头）。"""
    if not codes:
        return {}
    placeholders = ",".join("?" * len(codes))
    with db_conn() as c:
        rows = c.execute(
            "SELECT code, rbsa_industry_1 FROM fund_features WHERE code IN "
            f"({placeholders}) AND rbsa_industry_1 IS NOT NULL "
            "ORDER BY date DESC", codes).fetchall()
    seen = {}
    for code, ind in rows:
        if code not in seen:
            seen[code] = ind
    return {code: seen.get(code) for code in codes}


def _returns(navs_rows):
    """[(date, cum_nav)] → {date: 日收益}。"""
    ret = {}
    for i in range(1, len(navs_rows)):
        d, v0 = navs_rows[i - 1]; v1 = navs_rows[i][1]
        if v0 and v1 and v0 > 0:
            ret[d] = v1 / v0 - 1.0
    return ret


def run(start, end, step, codes, navs_map, peer_map):
    trade_dates = bt._trade_dates(start, end) if hasattr(bt, "_trade_dates") else None
    # 复用 bt.run 的日期取法
    with db_conn() as c:
        idx_rows = c.execute(
            "SELECT date FROM index_daily WHERE code='sh000300' ORDER BY date").fetchall()
    trade_dates = [r[0] for r in idx_rows]
    ts = [d for d in trade_dates if start <= d <= end][::step]
    rows_out = []
    for t in ts:
        feats = {}
        netfwd = {}
        rets_cache = {}
        for code in codes:
            rows = navs_map[code]
            pos = bt._pos_at([d for d, _ in rows], t)
            if pos < 0 or pos + FORWARD >= len(rows):
                continue
            vals = np.array([v for _, v in rows[:pos + 1]], dtype=float)
            if len(vals) < 62:
                continue
            f = nav_score_factors(vals)
            if f is None:
                continue
            fwd = forward_return_from_navs([v for _, v in rows], pos, FORWARD)
            if fwd is None:
                continue
            feats[code] = f
            netfwd[code] = fwd - COST
            # 仅缓存 60 日收益（供 corr；用 ≤T 的净值，无前视）
            rets_cache[code] = _returns(rows[max(0, pos - 60):pos + 1])
        if len(feats) < 30:
            continue
        scores = multifactor_scores(feats)
        ranked = sorted(scores, key=scores.get, reverse=True)
        top30 = ranked[:30]
        pure = top30[:5]

        def corr(c1, c2):
            r1, r2 = rets_cache.get(c1), rets_cache.get(c2)
            if not r1 or not r2:
                return None
            common = sorted(set(r1) & set(r2))
            if len(common) < 20:
                return None
            from app.features.stats import pearson
            r, _ = pearson([r1[d] for d in common], [r2[d] for d in common])
            return float(r)

        def peer(code):
            return peer_map.get(code)

        ranked_dicts = [{"code": c, "final_score": scores[c]} for c in top30]
        div = [d["code"] for d in select_diversified(
            ranked_dicts, corr, peer, MAX_CORR, MAX_SAME_PEER, 5)]
        rows_out.append({
            "t": t,
            "pure_ret": float(np.mean([netfwd[c] for c in pure])),
            "div_ret": float(np.mean([netfwd[c] for c in div])),
            "overlap": len(set(pure) & set(div)),
            "n_cands": len(feats),
        })
    return rows_out


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1500
    step = int(sys.argv[2]) if len(sys.argv) > 2 else 12
    codes, navs = bt.sample_active_equity(n, since="2022-01-01")
    print(f"抽样 {len(codes)} 只；加载 rbsa 同类标签...", flush=True)
    peer_map = load_rbsa_peer(codes)
    has_peer = sum(1 for v in peer_map.values() if v)
    print(f"  有 rbsa 同类标签: {has_peer}/{len(codes)}", flush=True)
    print("回测中（nav_score_factors 轻量路径）...", flush=True)
    rows = run("2023-06-01", "2026-04-01", step, codes, navs, peer_map)
    if not rows:
        print("无有效样本"); return
    pr = np.array([r["pure_ret"] for r in rows])
    dr = np.array([r["div_ret"] for r in rows])
    ov = np.array([r["overlap"] for r in rows])
    print(f"\n样本：{len(rows)} 决策日")
    print(f"{'':20}{'均收益':>10}{'标准差':>10}{'夏普代理':>10}")
    print(f"{'纯分数 Top5':<20}{pr.mean():>+10.4f}{pr.std():>10.4f}"
          f"{(pr.mean()/pr.std() if pr.std() > 0 else 0):>10.3f}")
    print(f"{'分散 Top5':<20}{dr.mean():>+10.4f}{dr.std():>10.4f}"
          f"{(dr.mean()/dr.std() if dr.std() > 0 else 0):>10.3f}")
    print(f"\n两者重合度（/5）: 均值 {ov.mean():.2f}  最小 {ov.min()}  最大 {ov.max()}")
    print(f"分散相对纯分数: 收益差 {dr.mean()-pr.mean():+.4f}  "
          f"风险差 {dr.std()-pr.std():+.4f}")


if __name__ == "__main__":
    main()

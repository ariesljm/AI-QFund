"""walk-forward 回测：风格跟踪特征（反推主线权重）对 40 日赚钱胜率的预测增益。

spec 终极验收口径（对齐 `quant-overhaul-plan` §7）：不是"还原持仓"，而是——
用"截至 T 日反推的主线权重"预测 T+1~T+40 收益，看风格跟踪特征能否提升
40 交易日绝对收益的赚钱胜率。

**无前视保证**：
- 特征（weight_1 / r_squared）只用 ≤ T 的净值与板块数据（60 日窗口反推）；
- 标签（fwd_ret）用 T+1~T+forward 的区间收益（指数日历定目标日）；
- 反推为纯计算（不落库），复用 `solve_style_weights`，与生产同算法。

**结论判据**：
1. 按 weight_1 分层（Q1 最低 ~ Q4 最高）：Q4 胜率 > Q1 胜率（风格特征有增益）；
2. 单调性：胜率随 weight_1 递增；
3. 相关性：weight_1 / r_squared 与 fwd_ret 的相关系数显著为正。
"""

import random

import numpy as np

import app.repo as repo
from app.database import db_conn
from app.engine.style_track import solve_style_weights
from app.features.sector import style_returns_matrix
from app.features.stats import pearson, t_tail_p

FORWARD = 40   # 与 domain.FORWARD_DAYS 对齐
WINDOW = 60    # 与 style_track._WINDOW 对齐


def _style_at(code: str, as_of: str, window: int = WINDOW,
              sector_cache: dict | None = None,
              navs: list[tuple[str, float]] | None = None) -> tuple[float, float] | None:
    """截至 as_of 的 60 日窗口反推（不落库），返回 (weight_1_pct, r_squared)。

    weight_1 取权重最高的板块，换算百分数（与生产 `compute_fund_style` 同口径）。
    sector_cache：同一 (start,end) 区间内所有基金共享板块矩阵（避免重复查询）；
    navs：预加载的基金净值序列（升序），None 时回退 DB 查询（供测试用）。
    """
    if navs is None:
        navs = repo.nav.series(code, limit=window + 1, until=as_of)
    else:
        navs = _slice_window(navs, as_of, window)
    if len(navs) < window + 1:
        return None
    dates = [d for d, _ in navs]
    vals = [v for _, v in navs]
    if any(v is None or v <= 0 for v in vals):
        return None
    fund_ret = np.array([vals[i] / vals[i - 1] - 1.0 for i in range(1, len(vals))])
    ret_dates = dates[1:]

    key = (ret_dates[0], ret_dates[-1])
    if sector_cache is not None and key in sector_cache:
        by_sector = sector_cache[key]
    else:
        by_sector: dict[str, dict[str, float]] = {}
        for d, c, _name, pct in repo.get_sector_pct_series(ret_dates[0], ret_dates[-1]):
            by_sector.setdefault(c, {})[d] = pct
        if sector_cache is not None:
            sector_cache[key] = by_sector
    # 深模块：÷100（pct 百分数→小数）、全日期覆盖过滤单点收敛（与生产同口径）
    m = style_returns_matrix(ret_dates, by_sector=by_sector)
    if m is None:
        return None
    R, _names = m
    w, r2 = solve_style_weights(fund_ret, R)
    top = int(np.argmax(w))
    return float(w[top] * 100.0), float(r2)


def _fwd_ret(code: str, t_date: str, forward: int = FORWARD,
             navs: list[tuple[str, float]] | None = None) -> float | None:
    """T 日 → T 后满 forward 个基金净值日的区间收益（与生产 forward_return 同口径）。

    navs：预加载净值（升序）；None 时回退 DB。纯计算复用
    repo.nav.forward_return_from_navs（架构审查候选 3：单一来源，窗口不足 → None，
    不再以指数日历 target 截断混入短窗口收益）。
    """
    if navs is None:
        navs = repo.nav.series(code, since=t_date)
    dates = [d for d, _ in navs]
    lo, hi = 0, len(dates) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if dates[mid] <= t_date:
            lo = mid + 1
        else:
            hi = mid - 1
    if hi < 0 or hi >= len(navs):
        return None
    return repo.nav.forward_return_from_navs([v for _, v in navs], hi, forward)


def _slice_window(navs: list[tuple[str, float]], as_of: str,
                  window: int) -> list[tuple[str, float]]:
    """升序净值序列 → as_of 及之前最近 window+1 条（二分定位）。"""
    dates = [d for d, _ in navs]
    lo, hi = 0, len(dates) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if dates[mid] <= as_of:
            lo = mid + 1
        else:
            hi = mid - 1
    end = hi  # 最后一个 <= as_of 的下标
    if end < 0:
        return []
    start = max(0, end - window)
    return navs[start:end + 1]


def _preload_navs(funds: list[str],
                  since: str = "2023-06-01") -> dict[str, list[tuple[str, float]]]:
    """预加载基金净值到内存（升序），回测主循环不再逐次查 DB（~2 万次 → n 次）。

    since 取回测起点前 ~1 年：既覆盖 60 日反推窗口的预热，又不载入无关早期数据。
    """
    return {code: repo.nav.series(code, since=since) for code in funds}


def _rbsa_weight_at(code: str, as_of: str) -> float | None:
    """截至 as_of 最近一期的季报 RBSA 权重（静态口径，fund_features）。"""
    with db_conn() as c:
        row = c.execute(
            "SELECT rbsa_weight_1 FROM fund_features WHERE code = ? AND date <= ? "
            "AND rbsa_weight_1 IS NOT NULL ORDER BY date DESC LIMIT 1",
            (code, as_of)).fetchone()
    return float(row[0]) if row else None


def _momentum(code: str, as_of: str, window: int = WINDOW,
              navs: list[tuple[str, float]] | None = None) -> float | None:
    """截至 as_of 的过去 window 日累计收益（动量因子，控制变量）。"""
    if navs is None:
        navs = repo.nav.series(code, limit=window + 1, until=as_of)
    else:
        navs = _slice_window(navs, as_of, window)
    if len(navs) < 2:
        return None
    v0, v1 = navs[0][1], navs[-1][1]
    if not v0 or not v1 or v0 <= 0:
        return None
    return v1 / v0 - 1.0


def sample_funds(n: int = 300, seed: int = 7,
                 since: str = "2024-01-01") -> list[str]:
    """有 since 前净值历史的基金随机抽样（seed 固定可复现）。

    注：fund_features 只有近期快照（无 2024 年历史 RBSA），季报对照列
    （rbsa_w）在早期回测区间覆盖率低，多元回归会自动剔除缺失行。
    """
    with db_conn() as c:
        rows = c.execute(
            "SELECT DISTINCT code FROM fund_nav WHERE date < ? AND cum_nav > 0",
            (since,)).fetchall()
    codes = [r[0] for r in rows]
    return random.Random(seed).sample(codes, min(n, len(codes)))


def run_walk_forward(funds: list[str], start: str = "2024-06-01",
                     end: str = "2026-07-01",
                     step: int = 20) -> list[dict]:
    """主循环：采样 T（每 step 交易日） × 基金 → 特征 + 40 日标签。

    返回 [{t, code, weight_1, r2, fwd_ret}, ...]。
    """
    trade_dates = [d for d, _, _ in repo.get_index_rows("sh000300")]
    ts = [d for d in trade_dates if start <= d <= end][::step]
    sector_cache: dict[tuple[str, str], dict] = {}
    navs_map = _preload_navs(funds)
    recs: list[dict] = []
    for t_date in ts:
        for code in funds:
            navs = navs_map.get(code) or []
            st = _style_at(code, t_date, sector_cache=sector_cache, navs=navs)
            if st is None:
                continue
            w1, r2 = st
            fr = _fwd_ret(code, t_date, navs=navs)
            if fr is None:
                continue
            recs.append({"t": t_date, "code": code,
                         "weight_1": w1, "r2": r2, "fwd_ret": fr,
                         "momentum": _momentum(code, t_date, navs=navs)})
    return recs


def _quantile_layers(recs: list[dict], field: str) -> list[tuple[str, float, float]]:
    """按字段四分位返回 [(层名, 下界, 上界)]（Q1 最小 ~ Q4 最大）。"""
    vals = sorted(r[field] for r in recs)
    n = len(vals)
    cuts = [vals[int(n * k / 4)] for k in range(1, 4)]
    layers = []
    lo = float("-inf")
    for i, hi in enumerate(cuts + [float("inf")], start=1):
        layers.append((f"Q{i}", lo, hi))
        lo = hi
    return layers


def _layer_winrate(recs: list[dict], field: str) -> list[dict]:
    """分层胜率统计：每层样本数、40 日赚钱胜率、平均收益。"""
    layers = _quantile_layers(recs, field)
    out = []
    for name, lo, hi in layers:
        group = [r for r in recs if (lo if lo != float("-inf") else -1e18) <= r[field] < hi]
        if not group:
            out.append({"layer": name, "n": 0, "winrate": None, "mean_ret": None})
            continue
        wins = sum(1 for r in group if r["fwd_ret"] > 0)
        out.append({"layer": name, "n": len(group),
                    "winrate": wins / len(group),
                    "mean_ret": float(np.mean([r["fwd_ret"] for r in group]))})
    return out


def report(recs: list[dict]) -> str:
    """生成分层胜率 + 相关性报告。"""
    if not recs:
        return "无有效样本（数据窗口不足）"
    n = len(recs)
    lines = [f"=== 风格跟踪 walk-forward 回测报告 ===",
             f"样本: {n} 条（T × 基金） | 前瞻: {FORWARD} 交易日 | 反推窗口: {WINDOW} 日",
             f"整体 40 日赚钱胜率: {sum(1 for r in recs if r['fwd_ret'] > 0) / n:.1%}",
             f"整体平均收益: {np.mean([r['fwd_ret'] for r in recs]):.2%}"]

    for field, label in (("weight_1", "反推主线权重 weight_1"),
                         ("r2", "反推拟合优度 r_squared")):
        lines.append(f"\n--- 按 {label} 分层 ---")
        lines.append(f"{'层':<4}{'范围':<22}{'样本':>7}{'胜率':>9}{'平均收益':>10}")
        layers = _layer_winrate(recs, field)
        for L in layers:
            if L["n"] == 0:
                lines.append(f"{L['layer']:<4}{'-':<22}{0:>7}")
                continue
            lines.append(f"{L['layer']:<4}{L['layer'] + ' 四分位':<22}"
                         f"{L['n']:>7}{L['winrate']:>9.1%}{L['mean_ret']:>10.2%}")
        q1, q4 = layers[0], layers[-1]
        if q1["n"] and q4["n"]:
            diff = q4["winrate"] - q1["winrate"]
            lines.append(f"Q4 - Q1 胜率差: {diff:+.1%}"
                         + ("  ← 风格特征有增益" if diff > 0 else "  ← 无增益/负增益"))
        # 单调性
        wins = [L["winrate"] for L in layers if L["n"] > 0]
        mono = all(b >= a for a, b in zip(wins, wins[1:]))
        lines.append(f"胜率随 {field} 单调递增: {'是' if mono else '否'}")

    lines.append("\n--- 相关性（Pearson）---")
    w1 = np.array([r["weight_1"] for r in recs])
    r2 = np.array([r["r2"] for r in recs])
    fr = np.array([r["fwd_ret"] for r in recs])
    for name, x in ((f"weight_1(反推)", w1), ("r2(反推拟合)", r2)):
        rho, p = pearson(x, fr)
        lines.append(f"{name} ~ fwd_ret: r={rho:+.4f} (p={p:.4g})"
                     + ("  ← 显著" if p < 0.05 else ""))

    lines.append("\n--- 多元回归（fwd_ret ~ 风格特征 + 动量，控制变量）---")
    lines.append("注：静态季报 RBSA 对照列（rbsa_w）因 fund_features 无历史快照不可得，"
                 "未纳入（待季报多期 RBSA 就绪后补）。")
    reg = _ols(recs)
    n_eff = int(reg.pop("_n")[1])
    lines.append(f"有效样本: {n_eff}/{len(recs)}")
    lines.append("各因子边际系数：正=该因子越大未来 40 日收益越高（>0 即该因子有独立预测力）")
    for k, (name, coef, p) in reg.items():
        lines.append(f"{name:<22} coef={coef:+.5f} (p={p:.4g})"
                     + ("  ← 独立正贡献" if coef > 0 and p < 0.05
                        else "  ← 独立负贡献" if coef < 0 and p < 0.05 else ""))
    return "\n".join(lines)


def _ols(recs: list[dict]) -> dict[str, tuple[str, float, float]]:
    """最小二乘回归 fwd_ret ~ 1 + weight_1 + r2 + momentum，返回 OLS 系数与 p 值。"""
    fields = ["weight_1", "r2", "momentum"]
    # 剔除缺失变量（rbsa_w 可能为空）
    clean = [r for r in recs if all(r[f] is not None for f in fields)]
    out: dict[str, tuple[str, float, float]] = {}
    if len(clean) < 50:
        for f in fields:
            out[f] = (f, float("nan"), 1.0)
        out["_n"] = ("有效样本", float(len(clean)), 0.0)
        return out
    X = np.column_stack([np.ones(len(clean))] +
                        [[r[f] for r in clean] for f in fields])
    y = np.array([r["fwd_ret"] for r in clean])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    n, k = X.shape
    resid = y - X @ coef
    dof = n - k
    s2 = float(resid @ resid / dof)
    inv = np.linalg.inv(X.T @ X)
    se = np.sqrt(np.diag(inv) * s2)
    t = coef / se
    pvals = t_tail_p(t, dof)
    for i, f in enumerate(fields):
        out[f] = (f, float(coef[i + 1]), float(pvals[i + 1]))
    out["_n"] = ("有效样本", float(len(clean)), 0.0)
    return out


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    n_funds = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    funds = sample_funds(n_funds)
    print(f"抽样基金: {len(funds)} 只", flush=True)
    recs = run_walk_forward(funds)
    print(report(recs))

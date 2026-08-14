"""组合估值模块：等权组合累计收益、多周期收益、夏普、回撤、累计超额。

Web 面板与推荐质量度量共用的业务数学，独立于渲染层。
"""

from datetime import datetime

import app.repo as repo


def portfolio_series() -> tuple[list[str], list[float], list[float]]:
    """等权买入持有组合的每日累计收益序列 + 同期沪深300累计收益序列。

    对每只被推荐基金，从首次推荐日起的累计净值收益率为其贡献；
    已离场（EXIT + exit_date）基金截至离场日截断；
    每日组合收益 = 当日所有已入场且未离场基金累计收益率的等权平均。
    返回 (dates, port_pcts, hs_pcts)；有效点不足2个时返回空。
    """
    tracks = repo.get_tracking_list()
    if not tracks:
        return [], [], []
    today = datetime.now().strftime("%Y-%m-%d")
    funds = []
    min_date = None
    for t in tracks:
        fd = t["first_date"]
        if not fd:
            continue
        entry = repo.get_entry_nav(t["code"], fd)
        if entry is None:
            entry = repo.nav.at(t["code"], fd)
        if entry is None or entry <= 0:
            continue
        funds.append({"code": t["code"], "fd": fd, "end": t["exit_date"] or today, "entry": entry})
        if min_date is None or fd < min_date:
            min_date = fd
    if not funds or not min_date:
        return [], [], []
    rows = repo.nav.batch_latest([f["code"] for f in funds])
    nav_by_fund = {}
    date_set = set()
    for code, d, nav in rows:
        if nav is None or nav <= 0:
            continue
        nav_by_fund.setdefault(code, {})[d] = nav
        date_set.add(d)
    # 推荐日当天无净值时，以入场净值作为基线点（组合曲线从 0% 起步）
    for f in funds:
        if f["fd"] not in nav_by_fund.get(f["code"], {}):
            nav_by_fund.setdefault(f["code"], {})[f["fd"]] = f["entry"]
            date_set.add(f["fd"])
    dates = sorted(date_set)
    if len(dates) < 2:
        return [], [], []
    # 每日组合累计收益率（等权平均，离场基金截断到 end）
    port_pcts: list[float | None] = []
    for d in dates:
        vals = []
        for f in funds:
            if d < f["fd"] or d > f["end"]:
                continue
            nav = nav_by_fund.get(f["code"], {}).get(d)
            if nav is None:
                continue
            vals.append((nav / f["entry"] - 1) * 100)
        port_pcts.append(sum(vals) / len(vals) if vals else None)
    # 沪深300同期序列（相对首个可用 close，交易日向前取最近值）
    hs_rows = repo.get_index_series("sh000300", ("date", "close"), min_date)
    hs_vals = [(d, c) for d, c in hs_rows if c is not None and c > 0]
    if not hs_vals:
        return [], [], []
    hs_base = hs_vals[0][1]
    hs_pcts: list[float | None] = []
    idx = -1
    for d in dates:
        while idx + 1 < len(hs_vals) and hs_vals[idx + 1][0] <= d:
            idx += 1
        if idx >= 0 and hs_base:
            hs_pcts.append((hs_vals[idx][1] / hs_base - 1) * 100)
        else:
            hs_pcts.append(None)
    # 裁剪到组合与基准均有值的连续区间
    pairs = [(d, p, h) for d, p, h in zip(dates, port_pcts, hs_pcts) if p is not None and h is not None]
    if len(pairs) < 2:
        return [], [], []
    dates, port_pcts, hs_pcts = zip(*pairs)
    return list(dates), list(port_pcts), list(hs_pcts)


def period_returns(code: str) -> dict[str, float | None]:
    """计算基金多周期收益率及同期沪深300收益。"""
    rows = repo.nav.series(code, limit=250)
    if not rows or not rows[0][1]:
        return {}
    dates = [r[0] for r in rows]
    navs = [r[1] or 0 for r in rows]
    latest_nav = navs[-1]
    # 周≈5交易日，月≈22，季≈66，半年≈126
    periods = {"1周": 5, "1月": 22, "3月": 66, "6月": 126}
    hs_rows = repo.get_index_series("sh000300", ("date", "close"), dates[0])
    hs_map = {r[0]: r[1] for r in hs_rows}
    result: dict[str, float | None] = {}
    for label, lookback in periods.items():
        idx = max(0, len(navs) - 1 - lookback)
        old_nav = navs[idx]
        result[label] = round((latest_nav / old_nav - 1) * 100, 2) if old_nav else None
        old_date = dates[idx]
        old_hs = hs_map.get(old_date)
        latest_hs = hs_map.get(dates[-1])
        if old_hs and latest_hs:
            result[label + "_hs"] = round((latest_hs / old_hs - 1) * 100, 2)
        else:
            result[label + "_hs"] = None
    return result


def sharpe_ratio(pcts: list[float]) -> float | None:
    """组合日收益年化夏普比率（无风险利率按 0）。"""
    rets = [(pcts[i] - pcts[i - 1]) / 100 for i in range(1, len(pcts))]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    std = var ** 0.5
    if std == 0:
        return None
    return round(mean / std * (252 ** 0.5), 2)


def max_drawdown(pcts: list[float]) -> float:
    """组合累计收益曲线的最大峰谷回撤（%）。"""
    peak = pcts[0]
    mdd = 0.0
    for v in pcts:
        if v > peak:
            peak = v
        if peak - v > mdd:
            mdd = peak - v
    return round(mdd, 2)


def alpha_series(candidates: list[dict]) -> list[float]:
    """逐基金累计超额 alpha 序列（按推荐日期排序，已离场基金截断到离场日）。

    candidates 每项需含 first_date/return/status/first_nav/code/exit_date。
    """
    sorted_candidates = sorted(candidates, key=lambda x: x["first_date"] or "")
    today = datetime.now().strftime("%Y-%m-%d")
    cum_alpha = 0.0
    alpha_pcts = []
    for c in sorted_candidates:
        if c["return"] is not None and c["first_date"]:
            hs_start = repo.get_index_close("sh000300", c["first_date"])
            end_str = c["exit_date"] or today
            hs_end = repo.get_index_close("sh000300", end_str)
            if hs_start and hs_end:
                hs_ret = (hs_end / hs_start - 1) * 100
                fund_ret = c["return"]
                if c.get("exit_date") and c["status"] == "EXIT":
                    end_nav = repo.nav.at_or_before(c["code"], end_str)
                    if end_nav and c["first_nav"] and c["first_nav"] > 0:
                        fund_ret = round((end_nav / c["first_nav"] - 1) * 100, 2)
                cum_alpha += fund_ret - hs_ret
                alpha_pcts.append(round(cum_alpha, 2))
    return alpha_pcts

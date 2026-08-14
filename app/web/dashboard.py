"""页面上下文 module：把 repo 数据组装为模板上下文，每区块一个窄函数。

FastAPI 路由层只调用 index_context()；区块函数独立可测。
"""

from datetime import datetime
from pathlib import Path

from app.web.charts import smooth_svg_path, make_dual_svg, quality_curve_svg as chart_quality_curve
from app.engine.valuation import (portfolio_series, period_returns,
                                  sharpe_ratio, max_drawdown, alpha_series)
import app.repo as repo
from app import domain


def candidate_summary(candidates: list[dict]) -> tuple[list[dict], float, int, float]:
    """追踪监控列表 → 展示项 + 累计收益/命中率统计。"""
    candidate_list = []
    # 批量汇总（_candidate_summary N+1 收敛）：信号 / 首次净值 / 最新净值一次查询
    summaries = repo.get_candidate_nav_summaries(
        [(c["code"], c["first_date"]) for c in candidates])
    for c in candidates:
        code, first_date = c["code"], c["first_date"]
        name = c["name"] or ""
        rec_count = c["rec_count"]
        s = summaries[code]
        # 展示状态与基金详情一致：取 monitor_events 最新监控信号（无信号时回退推荐状态）
        status = s["signal"] or (c["status"] or "HOLD")
        exit_date = c["exit_date"] or ""
        # 首次推荐净值（优先读 recommend_log.entry_nav，缺失时查 fund_nav 当日净值，无则 --）
        first_nav = s["entry_nav"] or s["nav_at_first"]
        # 当前净值（取最新盘后净值，今日无则自动回退到前一日）
        cur_nav = s["latest_nav"] if first_nav is not None else None        # 累计收益
        ret = None
        if first_nav and cur_nav and first_nav > 0:
            ret = round((cur_nav / first_nav - 1) * 100, 2)
        candidate_list.append({
            "code": code, "name": name,
            "first_date": first_date or "",
            "first_nav": round(first_nav, 4) if first_nav else None,
            "cur_nav": round(cur_nav, 4) if cur_nav else None,
            "return": ret,
            "rec_count": rec_count,
            "status": status,
            "exit_date": exit_date,
            "type": "",
        })
    # 累计收益总和
    total_return = round(sum(c["return"] for c in candidate_list if c["return"] is not None), 2) if candidate_list else 0
    rec_count = len(candidate_list)
    hit_count = sum(1 for c in candidate_list if c["return"] is not None and c["return"] > 0)
    hit_rate = round(hit_count / rec_count * 100, 1) if rec_count > 0 else 0
    return candidate_list, total_return, rec_count, hit_rate


def fund_profile_block(code: str) -> tuple[dict | None, list[dict]]:
    """基金特征画像 + 十大持仓（最新推荐 / 次新推荐复用）。"""
    fund_features = None
    feat = repo.get_latest_features(code)
    if feat:
        fund_features = {
            "hurst": feat["hurst_60d"],
            "momentum": round(feat["momentum_20d"] or 0, 2) if feat["momentum_20d"] is not None else None,
            "calmar": round(feat["calmar"] or 0, 2) if feat["calmar"] is not None else None,
            "downside_vol": round(feat["downside_vol"] or 0, 2) if feat["downside_vol"] is not None else None,
            "capture_up": round(feat["capture_up"] or 0, 1) if feat["capture_up"] is not None else None,
            "capture_down": round(feat["capture_down"] or 0, 1) if feat["capture_down"] is not None else None,
            "bias": round(feat["bias_60d"] or 0, 2) if feat["bias_60d"] is not None else None,
            "top_industry": feat["rbsa_industry_1"] or "",
            "top_industry_weight": round(feat["rbsa_weight_1"] or 0, 1),
        }
    top_holdings = [
        {"code": h["stock_code"], "name": h["stock_name"], "weight": h["weight"],
         "industry": h["industry"] or ""}
        for h in repo.get_holdings(code, 10)
    ]
    return fund_features, top_holdings


def alpha_block(candidate_list: list[dict], total_return: float) -> tuple[float | None, str, float]:
    """超额阿尔法（跑赢沪深300）+ 逐基金 alpha 贡献曲线。"""
    alpha = None
    start_date = repo.get_first_reco_date()
    if start_date and total_return is not None:
        hs300_start = repo.get_index_close("sh000300", start_date)
        hs300_now = repo.get_index_close("sh000300")
        if hs300_start and hs300_now:
            hs300_pct = round((hs300_now / hs300_start - 1) * 100, 2)
            alpha = round(total_return - hs300_pct, 2)
    alpha_svg, alpha_baseline_y = smooth_svg_path(alpha_series(candidate_list))
    return alpha, alpha_svg, alpha_baseline_y


def nav_chart(code: str) -> tuple[list[float], list[str], list[float], list[str]]:
    """返回近3个月基金净值+沪深300数据，用于双线走势图。"""
    rows = repo.nav.series(code, limit=65)
    if not rows:
        return [], [], [], []
    # 基金净值归一化为收益率
    base_nav = rows[0][1] or 1
    nav_pcts = [round(((r[1] or 0) / base_nav - 1) * 100, 2) for r in rows]
    dates = [r[0] for r in rows]
    # 沪深300同日期
    hs_rows = repo.get_index_series("sh000300", ("date", "close"), dates[0])
    hs_map = {r[0]: r[1] for r in hs_rows}
    hs_pcts = []
    hs_dates = []
    base_hs = None
    for d in dates:
        v = hs_map.get(d)
        if v and base_hs is None:
            base_hs = v
        if v and base_hs:
            hs_pcts.append(round(((v / base_hs) - 1) * 100, 2))
            hs_dates.append(d)
    return nav_pcts, dates, hs_pcts, hs_dates


def build_latest_recos(recs: list[dict], today: str) -> tuple[dict | None, list[dict], int]:
    """最新 2 条推荐组装为模板数据（latest / latest_list / latest_rec_id）。

    纯组装：从 repo 行投影出模板字段；latest 取第一条（最新），
    latest_rec_id 为最新记录的 id（供前端轮询推荐刷新比对）。
    """
    latest = None
    latest_rec_id = None
    latest_list: list[dict] = []
    for rec in recs:
        if latest_rec_id is None:
            latest_rec_id = rec["id"]
        entry = {
            "code": rec["code"], "name": rec["name"],
            "pred_alpha": rec["score"],
            "regime": rec["regime"] or "NEUTRAL", "reason": rec["reason"],
            "status": rec["status"], "date": rec["date"] or today, "return": rec["return"],
            "type": rec["type"] or "",
        }
        latest_list.append(entry)
        if latest is None:
            latest = entry
    return latest, latest_list, latest_rec_id or 0


def macro_block(today: str) -> tuple:
    """宏观摘要块：macro_news 行 → 展示结构 + 空推荐日标记（模板上下文用）。"""
    mn = repo.get_latest_macro_news()
    macro = domain.parse_macro_summary(mn)
    empty_today = repo.get_empty_recommendation(today)
    return (macro["macro"], macro["sector_gainers"], macro["sector_losers"],
            macro["flow_inflows"], macro["flow_outflows"], macro["max_inflow"],
            macro["max_outflow"], macro["sector_reasoning"], macro["regime_label"],
            empty_today, macro["flow_net_total"], macro["macro_date"])


def model_trained_at() -> str | None:
    """模型实际训练时刻：模型文件 mtime（比 meta 记录更真实，训练失败不落 meta）。

    mtime 口径与 app/model.py 的 MODEL_PATH 一致（相对项目根）；无模型文件时回退 meta 记录。
    """
    try:
        p = Path("models/lgb_model.txt")
        if p.exists():
            return datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    except OSError:
        pass
    return repo.get_model_last_trained()


def quality_block() -> tuple:
    """质量度量块：最近 6 期度量 + 累计超额曲线 SVG + 最新一期指标（模板上下文用）。"""
    quality_metrics = repo.get_quality_metrics(6)
    quality_curve_svg = ""
    quality_curve_baseline = 50
    if quality_metrics:
        _pts = quality_metrics[0].get("points") or []
        if len(_pts) >= 2:
            quality_curve_svg, quality_curve_baseline = chart_quality_curve(_pts)
    latest_ic = None
    latest_excess_win_rate = None
    latest_profit_rate = None
    if quality_metrics:
        latest_ic = quality_metrics[0].get("ic")
        latest_excess_win_rate = quality_metrics[0].get("excess_win_rate")
        latest_profit_rate = quality_metrics[0].get("profit_rate")
    return (quality_metrics, quality_curve_svg, quality_curve_baseline,
            latest_ic, latest_excess_win_rate, latest_profit_rate)


def sector_heatmap_block() -> list[dict]:
    """行业热力图块：行业名 + 权重/动量（模板上下文用）。"""
    sectors = repo.get_sector_heatmap()
    return [
        {"name": s["name"], "weight": round(s["weight"] or 0, 1),
         "momentum": round(s["momentum"] or 0, 1)}
        for s in sectors
    ]


def portfolio_block() -> tuple:
    """等权组合块：累计收益双线 SVG + 夏普 + 最大回撤（模板上下文用）。"""
    _, port_pcts, port_hs_pcts = portfolio_series()
    if not port_pcts:
        return "", "", 50, None, None
    svg, hs_svg, baseline = make_dual_svg(port_pcts, port_hs_pcts)
    return svg, hs_svg, baseline, sharpe_ratio(port_pcts), max_drawdown(port_pcts)


def index_context() -> dict[str, object]:
    """首页上下文组装：各区块窄函数 → 模板变量 dict。"""
    today = datetime.now().strftime("%Y-%m-%d")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 今日推荐（最新 2 条 recommend_log）
    recs = repo.get_latest_recommendations(2)
    latest, latest_list, latest_rec_id = build_latest_recos(recs, today)

    # 宏观摘要 + 空推荐日标记
    (macro_data, sector_gainers, sector_losers, flow_inflows, flow_outflows,
     max_inflow, max_outflow, sector_reasoning, regime_label, empty_today,
     flow_net_total, macro_date) = macro_block(today)

    # 质量度量 + 累计超额曲线 + 最新一期指标
    (quality_metrics, quality_curve_svg, quality_curve_baseline,
     latest_ic, latest_excess_win_rate, latest_profit_rate) = quality_block()

    # 行业热力图
    sector_list = sector_heatmap_block()

    # 基金池总数 + 按类型分组
    fund_pool, pool_by_type = repo.get_fund_pool_stats()
    pool_types = [{"type": t["type"], "count": t["count"]} for t in pool_by_type]

    # 追踪监控列表 + 累计收益/命中率
    candidate_list, total_return, rec_count, hit_rate = candidate_summary(repo.get_tracking_list())

    # 净值图表（近3个月双线走势）
    nav_pcts, nav_dates, hs_pcts, hs_dates = nav_chart(latest["code"]) if latest else ([], [], [], [])
    fund_svg, hs_svg, baseline_y = make_dual_svg(nav_pcts, hs_pcts)
    period_ret = period_returns(latest["code"]) if latest else {}
    period_ret2 = period_returns(latest_list[1]["code"]) if len(latest_list) > 1 else {}

    # 基金特征画像 + 十大持仓（最新 / 次新推荐）
    fund_features, top_holdings = fund_profile_block(latest["code"]) if latest else (None, [])
    top_holdings2 = fund_profile_block(latest_list[1]["code"])[1] if len(latest_list) > 1 else []

    # 运行天数
    uptime_days = repo.get_uptime_days()

    # 超额阿尔法 + 逐基金 alpha 贡献曲线
    alpha, alpha_svg, alpha_baseline_y = alpha_block(candidate_list, total_return)

    # 等权组合累计收益序列（用于 Alpha 双线图 + 夏普/回撤）
    (portfolio_svg, portfolio_hs_svg, portfolio_baseline_y,
     sharpe_ratio_value, max_drawdown) = portfolio_block()

    return {
        "latest": latest,
        "latest_list": latest_list,
        "latest_rec_id": latest_rec_id,
        "macro": macro_data,
        "candidates": candidate_list,
        "fund_pool": fund_pool,
        "pool_types": pool_types,
        "now": now_str,
        "today": today,
        "data_latest_date": repo.get_data_latest_date(),
        "model_last_trained": model_trained_at(),
        "sector_list": sector_list,
        "sector_reasoning": sector_reasoning,
        "regime_label": regime_label,
        "nav_pcts": nav_pcts,
        "nav_dates": nav_dates,
        "hs_pcts": hs_pcts,
        "fund_svg": fund_svg,
        "hs_svg": hs_svg,
        "baseline_y": baseline_y,
        "period_ret": period_ret,
        "period_ret2": period_ret2,
        "fund_features": fund_features,
        "top_holdings": top_holdings,
        "top_holdings2": top_holdings2,
        "sector_gainers": sector_gainers,
        "sector_losers": sector_losers,
        "uptime_days": uptime_days,
        "alpha": alpha,
        "alpha_svg": alpha_svg,
        "alpha_baseline_y": alpha_baseline_y,
        "total_return": total_return,
        "rec_count": rec_count,
        "hit_rate": hit_rate,
        "flow_inflows": flow_inflows,
        "flow_outflows": flow_outflows,
        "flow_net_total": flow_net_total,
        "max_inflow": max_inflow,
        "max_outflow": max_outflow,
        "empty_today": empty_today,
        "macro_date": macro_date,
        "quality_metrics": quality_metrics,
        "quality_curve_svg": quality_curve_svg,
        "quality_curve_baseline": quality_curve_baseline,
        "portfolio_svg": portfolio_svg,
        "portfolio_hs_svg": portfolio_hs_svg,
        "portfolio_baseline_y": portfolio_baseline_y,
        "sharpe_ratio": sharpe_ratio_value,
        "max_drawdown": max_drawdown,
        "latest_ic": latest_ic,
        "latest_excess_win_rate": latest_excess_win_rate,
        "latest_profit_rate": latest_profit_rate,
        "signal_labels": domain.SIGNAL_LABELS,
        "regime_labels": domain.REGIME_LABELS,
    }

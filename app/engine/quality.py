"""推荐质量度量模块：信息系数(IC)、20日超额胜率、累计超额。

度量回答"进化后推荐质量是否提升"：IC 衡量模型预测分与未来 20 日
实际超额收益（相对沪深300）的相关性；超额胜率衡量跑赢基准的推荐占比。

IC 的 Spearman 秩相关用 numpy 手写（含并列平均秩），不依赖 scipy。
"""

import numpy as np

from app.utils.log import get_logger

logger = get_logger("quality")

_FORWARD_ROWS = 20


def _rankdata(x: np.ndarray) -> np.ndarray:
    """计算平均秩（tie 取平均），与 scipy.stats.rankdata(average) 一致。"""
    x = np.asarray(x, dtype=float)
    n = x.size
    sorter = np.argsort(x, kind="stable")
    inv = np.empty(n, dtype=np.intp)
    inv[sorter] = np.arange(n)
    sx = x[sorter]
    obs = np.concatenate(([True], sx[1:] != sx[:-1]))
    dense = obs.cumsum()[inv]
    group_idx = np.flatnonzero(obs)
    counts = np.diff(np.concatenate((group_idx, [n])))
    avg_rank = group_idx + 1 + 0.5 * (counts - 1)
    return avg_rank[dense - 1]


def spearman(x, y) -> float | None:
    """Spearman 秩相关；常数序列（无秩差异）返回 None。"""
    rx = _rankdata(np.asarray(x, dtype=float))
    ry = _rankdata(np.asarray(y, dtype=float))
    with np.errstate(invalid="ignore", divide="ignore"):
        corr = np.corrcoef(rx, ry)[0, 1]
    return float(corr) if not np.isnan(corr) else None


def compute_metrics_from_pairs(pairs: list[tuple[float, float]]) -> dict:
    """从 (预测分, 实现超额收益) 序列计算质量指标。样本 <2 时 IC 为 None。"""
    if not pairs:
        return {"ic": None, "excess_win_rate": None, "mean_excess": None,
                "cum_excess": 0.0, "sample_count": 0}
    scores = [p[0] for p in pairs]
    alphas = [p[1] for p in pairs]
    ic = spearman(scores, alphas) if len(pairs) >= 2 else None
    if ic is not None and np.isnan(ic):
        ic = None  # 常数序列无秩相关，视为不可用
    return {
        "ic": round(ic, 4) if ic is not None else None,
        "excess_win_rate": round(sum(1 for a in alphas if a > 0) / len(alphas), 4),
        "mean_excess": round(sum(alphas) / len(alphas), 6),
        "cum_excess": round(sum(alphas), 6),
        "sample_count": len(pairs),
    }


def compute_quality_metrics(conn, period_start: str, period_end: str) -> dict:
    """统计区间内推荐的 20 日实际超额收益并计算质量指标。

    对每条推荐取入场后 21 条净值（含入场日），用第 0 与第 20 条计算基金收益；
    同期沪深300 按同日期收盘价计算基准收益；alpha = 基金收益 - 基准收益。
    数据不足（净值 <21 条或指数缺失）的样本跳过。
    """
    rows = conn.execute(
        "SELECT code, recommend_date, score FROM recommend_log "
        "WHERE recommend_date >= ? AND recommend_date <= ? "
        "AND score IS NOT NULL AND status != 'REJECT' "
        "ORDER BY recommend_date ASC, code ASC",
        (period_start, period_end),
    ).fetchall()

    pairs: list[tuple[float, float]] = []
    points: list[dict] = []
    for code, reco_date, score in rows:
        nav_rows = conn.execute(
            "SELECT date, cum_nav FROM fund_nav WHERE code = ? AND date >= ? "
            "ORDER BY date ASC LIMIT ?",
            (code, reco_date, _FORWARD_ROWS + 1),
        ).fetchall()
        if len(nav_rows) < _FORWARD_ROWS + 1:
            continue
        start_date, start_nav = nav_rows[0][0], nav_rows[0][1]
        end_date, end_nav = nav_rows[_FORWARD_ROWS][0], nav_rows[_FORWARD_ROWS][1]
        hs_start = conn.execute(
            "SELECT close FROM index_daily WHERE code = 'sh000300' AND date = ?",
            (start_date,),
        ).fetchone()
        hs_end = conn.execute(
            "SELECT close FROM index_daily WHERE code = 'sh000300' AND date = ?",
            (end_date,),
        ).fetchone()
        if not (start_nav and end_nav and hs_start and hs_end and start_nav > 0 and hs_start[0] > 0):
            continue
        fund_ret = end_nav / start_nav - 1.0
        hs_ret = hs_end[0] / hs_start[0] - 1.0
        alpha = fund_ret - hs_ret
        pairs.append((float(score), alpha))
        points.append({"date": reco_date, "code": code,
                       "alpha": round(alpha, 6)})

    metrics = compute_metrics_from_pairs(pairs)
    # 累计超额曲线：按时间序累加
    cum = 0.0
    for p in points:
        cum += p["alpha"]
        p["cum_alpha"] = round(cum, 6)
    metrics["points"] = points
    metrics["period_start"] = period_start
    metrics["period_end"] = period_end
    logger.info("推荐质量度量: 区间 %s~%s, 样本 %d 条",
                period_start, period_end, metrics["sample_count"])
    return metrics

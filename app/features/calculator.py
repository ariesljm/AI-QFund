"""特征计算模块：Hurst、动量、卡玛、RBSA、大盘状态机。"""

import time
from datetime import datetime

import numpy as np
import pandas as pd

import app.repo as repo
from app import domain
from app.data.store import save_fund_features, trim_fund_features
from app.repo import meta_keys as META
from app.utils.log import get_logger

logger = get_logger("features")

_FEATURE_RETENTION_ROWS = 250

# 特征列 schema 版本：FEATURE_COLS 增删时**必须递增**。
# 否则 skip 逻辑会把"已最新"但缺新列（NULL）的旧快照留下，
# 而候选过滤的 dropna(FEATURE_COLS) 会直接清空整个候选池。
# v2: 新增 sharpe_60d / sortino_60d / ttr_60d（风险调整三指标）
# v3: 新增 style_r2（净值反推拟合优度=风格清晰度，walk-forward 回测 P5 证实有独立预测增益）
_FEATURE_SCHEMA_VERSION = "v4"
"""fund_features 每只基金保留的特征快照行数（与净值保留窗口一致，覆盖监控风格漂移的历史查询）。"""



def vol_adaptive_stop_pct(daily_navs: list[float], mult: float = 1.5,
                          floor_pct: float = 0.06, cap_pct: float = 0.15,
                          vol_window: int = 20) -> float:
    """波动率自适应止损阈值（reco-hardening T04）。

    阈值 = clamp(mult × 近 vol_window 日收益波动率(σ×√20), floor_pct, cap_pct)。
    高波动赛道（AI/半导体）8% 固定阈值一两周即触发，低波动赛道又太松；
    自适应让止损随标的实际波动缩放。数据不足 vol_window 时用可用天数。
    """
    valid = [n for n in daily_navs if n and n > 0]
    if len(valid) < 2:
        return floor_pct
    rets = [valid[i] / valid[i - 1] - 1.0 for i in range(1, len(valid))]
    window = min(vol_window, len(rets))
    if window < 2:
        return floor_pct
    recent = rets[-window:]
    import statistics
    vol = statistics.pstdev(recent) * (20.0 ** 0.5)  # 日波动 → 月波动近似
    thr = max(floor_pct, min(mult * vol, cap_pct))
    return thr


def sim_vol_adaptive_stop(daily_navs: list[float], mult: float = 1.5,
                          floor_pct: float = 0.06, cap_pct: float = 0.15,
                          max_days: int = 20) -> float | None:
    """模拟波动自适应止损（T04）：阈值随净值波动缩放，其余同 sim_hard_stop。

    返回结算收益；数据不足返回 None。
    """
    if len(daily_navs) < 2:
        return None
    entry = daily_navs[0]
    if entry is None or entry <= 0:
        return None
    highest = entry
    for i in range(1, min(len(daily_navs), max_days + 1)):
        nav = daily_navs[i]
        if nav is None or nav <= 0:
            break
        if nav > highest:
            highest = nav
        # 阈值用截至前一日的净值算波动（T04 信号及时性：当日崩盘不自抬当日阈值）
        thr = vol_adaptive_stop_pct(daily_navs[:i], mult, floor_pct, cap_pct)
        if (highest - nav) / highest > thr:
            return nav / entry - 1.0
    settle_idx = min(len(daily_navs) - 1, max_days)
    return daily_navs[settle_idx] / entry - 1.0


def sim_take_profit(daily_navs: list[float], profit_threshold: float = 0.15,
                    pullback_pct: float = 0.12, max_days: int = 40) -> float | None:
    """盈利保护止盈（reco-hardening T08）：盈利 ≥ profit_threshold 后，
    从盈利期最高点回撤 ≥ pullback_pct 即结算——让利润奔跑，同时锁住已实现收益。

    未达盈利阈值前按持有到期结算（不提前止损；与硬止损并存时由调用方组合）。
    """
    if len(daily_navs) < 2:
        return None
    entry = daily_navs[0]
    if entry is None or entry <= 0:
        return None
    armed = False
    peak_after_arm = entry
    for i in range(1, min(len(daily_navs), max_days + 1)):
        nav = daily_navs[i]
        if nav is None or nav <= 0:
            break
        if not armed and nav / entry - 1.0 >= profit_threshold:
            armed = True
        if armed:
            if nav > peak_after_arm:
                peak_after_arm = nav
            if (peak_after_arm - nav) / peak_after_arm >= pullback_pct:
                return nav / entry - 1.0
    settle_idx = min(len(daily_navs) - 1, max_days)
    return daily_navs[settle_idx] / entry - 1.0


_EMA_SPAN = 60
_EMA_CONFIRM_DAYS = 2
EMA_WARMUP_NAVS = _EMA_SPAN + _EMA_CONFIRM_DAYS
"""EMA60 预热所需最少净值条数（span+confirm=62）。

公开常量：监控趋势防线（ema60_exit）、候选池数据不足打标（mark_short_history_funds）
共用此单一来源，避免各自硬编码阈值漂移。"""


def _ema_series(navs: np.ndarray, span: int = _EMA_SPAN) -> np.ndarray:
    """EMA(span) 序列（单一来源：ema60_exit / sim_ema60_exit 共用）。"""
    k = 2.0 / (span + 1.0)
    ema = np.empty(len(navs))
    ema[0] = navs[0]
    for i in range(1, len(navs)):
        ema[i] = navs[i] * k + ema[i - 1] * (1.0 - k)
    return ema


def calc_sector_heat(cum_rets: list[float]) -> float:
    """赛道热度（市场级、**可历史化**）：近 5 日各赛道累计涨幅的横截面分化度。

    取 (P90 − 中位数)：赛道间涨幅分化越极端，说明资金越集中、拥挤风险越高。
    因基于板块日涨幅（sector_daily_snapshot，自 2024-03 有历史），可与指数列
    一样回溯到任意决策日，故能真进 LightGBM 训练（net_flow 仅 ~17 日，不可行）。

    纯函数；有效样本 < 5 个赛道 → 0.0（优雅降级，不窃动模型）。
    """
    arr = np.asarray([float(x) for x in cum_rets if x is not None], dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size < 5:
        return 0.0
    return float(np.percentile(arr, 90) - np.median(arr))


def load_sector_pct_frame(start: str | None = None, end: str | None = None):
    """加载板块日涨幅矩阵（index=date，columns=sector_name，值为百分数）。

    一次查询建成宽表，供训练（逐决策日切片）/推荐（最新切片）复用，避免 N+1。
    无数据返回 None（调用方降级为热度 0）。
    """
    import app.repo as repo

    rows = repo.get_sector_pct_series(start or "0000-01-01", end or "9999-12-31")
    if not rows:
        return None
    df = pd.DataFrame([(d, n, p) for d, _c, n, p in rows],
                      columns=["date", "sector", "pct"])
    return df.pivot_table(index="date", columns="sector", values="pct", aggfunc="last")


def sector_heat_from_frame(sector_frame, as_of: str | None = None, window: int = 5) -> float:
    """从板块日涨幅矩阵算截至 as_of 的赛道热度（严格不含未来数据）。"""
    if sector_frame is None or len(sector_frame) == 0:
        return 0.0
    df = sector_frame if as_of is None else sector_frame.loc[:as_of]
    if len(df) < window:
        return 0.0
    cum = df.iloc[-window:].sum(axis=0)          # 各赛道近 window 日累计涨幅
    return calc_sector_heat(list(cum.dropna().values))


_latest_heat_cache: dict = {}


_mkt_state_cache: dict | None = None
_mkt_state_cache_date: str = ""


def latest_market_state() -> dict:
    """最新市场状态列（指数 20 日动量/波动率），按天缓存。

    归属 features 域（market_state_features 的最新态包装，架构深化 A5：web 与
    model 统一从此消费，web 不再直摸 model 层）；调用方显式传入 model.score。
    """
    global _mkt_state_cache, _mkt_state_cache_date
    today = datetime.now().strftime("%Y-%m-%d")
    if _mkt_state_cache is None or _mkt_state_cache_date != today:
        idx_rows = repo.get_index_series("sh000300", ("date", "close", "volume"))
        if idx_rows:
            closes = np.array([r[1] for r in idx_rows], dtype=float)
            vols = np.array([r[2] for r in idx_rows], dtype=float)
            _mkt_state_cache = market_state_features(closes, vols,
                                                     sector_heat=0.0)
        else:
            # 审计 P2-4：指数缺失显式告警（不静默 0）——模型在分布外输入打分
            logger.warning("指数数据缺失（sh000300 无行）：市场状态列填 0，模型打分失真风险")
            _mkt_state_cache = {c: 0.0 for c in domain.MARKET_COLS}
        _mkt_state_cache_date = today
    return _mkt_state_cache


def market_state_features(idx_close: np.ndarray, idx_vol: np.ndarray,
                          sector_heat: float = 0.0) -> dict:
    """市场状态列（单一来源）：指数 20 日动量/波动率、60 日偏离、赛道热度。

    R1 绝对收益目标配套：全基金共享的时变特征，让模型感知市场 beta 分量。
    入参为截至决策日的指数历史窗口（不含未来数据，训练/回测无前视）。
    sector_heat（ticket 08）：赛道拥挤度代理，缺数据传 0.0 不窃动。
    """
    feat: dict = {}
    if len(idx_close) >= 21:
        feat["idx_mom_20d"] = float((idx_close[-1] / idx_close[-21] - 1) * 100)
    else:
        feat["idx_mom_20d"] = 0.0
    if len(idx_close) >= 21:
        # 用收益率序列（非价格变动）计算年化波动率，与 vol_20d 同口径
        rets = np.diff(idx_close)[-20:] / idx_close[-21:-1]
        feat["idx_vol_20d"] = float(np.std(rets) * np.sqrt(252) * 100) if len(rets) > 0 else 0.0
    else:
        feat["idx_vol_20d"] = 0.0
    # bias_60d（顺带发现）：指数偏离60日均线，市场层面状态（从基金特征移入 MARKET_COLS）
    if len(idx_close) >= 60:
        idx_ma60 = np.mean(idx_close[-60:])
        feat["bias_60d"] = float((idx_close[-1] - idx_ma60) / idx_ma60 * 100)
    else:
        feat["bias_60d"] = 0.0
    feat["sector_heat_5d"] = float(sector_heat)
    return feat


def calc_hurst(series: np.ndarray, max_lag: int = 20) -> float:
    if len(series) < max_lag + 10:
        return 0.5
    lags = range(2, max_lag + 1)
    rs_values = []
    for lag in lags:
        n_blocks = len(series) // lag
        if n_blocks == 0:
            continue
        rs_list = []
        for i in range(n_blocks):
            block = series[i * lag : (i + 1) * lag]
            mean_block = np.mean(block)
            deviations: np.ndarray = np.cumsum(block - mean_block)
            r = np.max(deviations) - np.min(deviations)
            s = np.std(block, ddof=1) if np.std(block, ddof=1) > 0 else 1e-10
            rs_list.append(r / s)
        if rs_list:
            mean_rs = np.mean(rs_list)
            # 恒定 block（r/s=0）时 log(0) 无意义，跳过该 lag 避免 -inf 污染回归
            if mean_rs > 0:
                rs_values.append((np.log(lag), np.log(mean_rs)))
    if len(rs_values) < 2:
        return 0.5
    x = np.array([v[0] for v in rs_values])
    y = np.array([v[1] for v in rs_values])
    if len(x) < 2 or np.any(~np.isfinite(y)):
        return 0.5
    slope = np.polyfit(x, y, 1)[0]
    return float(np.clip(slope, 0, 1))


def _style_r2_from_frame(nav_dates: list[str], nav_vals: np.ndarray,
                         sector_frame, window: int = 60) -> float | None:
    """净值(带日期)×板块宽表 → 最近 window 日反推拟合优度 r_squared（不落库）。

    回测验收结论（spec P5）：r_squared（风格清晰度）对 40 日收益有独立正贡献
    （+0.045, p<0.001），主线权重 weight_1 无贡献——故只取 r2 进特征。
    数据不足/不可解释时返回 None（调用方降级为 0.0，避免 None 打崩候选池）。
    sector_frame：date × sector 宽表（pct 百分数），index 须为有序字符串日期。
    """
    from app.features.sector import style_returns_matrix
    from app.features.style_solve import solve_style_weights
    if sector_frame is None or len(nav_vals) < window + 1:
        return None
    vals = nav_vals[-window - 1:]
    if any(not np.isfinite(v) or v <= 0 for v in vals):
        return None
    ret = np.array([vals[i] / vals[i - 1] - 1.0 for i in range(1, len(vals))])
    ret_dates = nav_dates[-window:]
    # 深模块：宽表精确重排到净值窗口 + ÷100 + 全日期覆盖过滤（单点收敛）
    m = style_returns_matrix(ret_dates, sector_frame=sector_frame)
    if m is None:
        return None
    R, _names = m
    try:
        _w, r2 = solve_style_weights(ret, R)
    except Exception:
        return None
    return round(float(r2), 4)


def compute_fund_features(navs: np.ndarray, idx_closes: np.ndarray,
                          idx_volumes: np.ndarray,
                          nav_dates: list[str] | None = None,
                          sector_frame=None) -> dict | None:
    """从净值+指数数组计算特征（纯函数，不触碰 DB；数据不足返回 None）。

    特征公式单一来源：calc_features / 训练样本 / 回测均复用，避免多套公式漂移。
    nav_dates/sector_frame：style_r2（风格清晰度）反推用，可选——缺省时
    style_r2 取 0.0（无信号），保证旧调用与数据不足场景自动降级。
    """
    if len(navs) < 60:
        return None
    with np.errstate(divide="ignore", invalid="ignore"):
        returns = np.diff(navs) / navs[:-1]
    returns = returns[np.isfinite(returns)]

    feat: dict = {}
    # 打分三因子单一来源（生产与回测 PIT 共用，见 nav_score_factors）
    feat.update(nav_score_factors(navs))
    window = min(60, len(returns))
    feat["hurst_60d"] = float(calc_hurst(returns[-window:]))
    feat["momentum_20d"] = float((navs[-1] / navs[-20] - 1) * 100) if len(navs) >= 20 else 0.0

    if len(navs) >= 60:
        cum = navs[-60:] / navs[-60]
        peak = np.maximum.accumulate(cum)
        dd = (cum - peak) / peak
        max_dd = float(np.min(dd))
        feat["drawdown_60d"] = float(max_dd * 100)
        ann = float((navs[-1] / navs[-60] - 1) * 252 / 60)
        feat["calmar"] = ann / abs(max_dd) if abs(max_dd) > 1e-10 else 0.0
    else:
        feat["drawdown_60d"] = 0.0
        feat["calmar"] = 0.0

    if len(navs) >= 20:
        # 反转因子：后10日动量 − 前10日动量，正=下跌减速/企稳（超跌反弹先行信号）
        mom_hi = navs[-1] / navs[-11] - 1
        mom_lo = navs[-11] / navs[-20] - 1
        feat["reversal_20d"] = float((mom_hi - mom_lo) * 100)
    else:
        feat["reversal_20d"] = 0.0

    # 多窗口动量与波动率：让模型自行学习哪个窗口在何种市场状态有效
    feat["mom_5d"] = float((navs[-1] / navs[-6] - 1) * 100) if len(navs) >= 6 else 0.0
    feat["mom_60d"] = float((navs[-1] / navs[-61] - 1) * 100) if len(navs) >= 61 else 0.0
    if len(returns) >= 20:
        feat["vol_20d"] = float(np.std(returns[-20:]) * np.sqrt(252) * 100)
    else:
        feat["vol_20d"] = 0.0

    if len(returns) >= 20:
        neg = returns[-20:][returns[-20:] < 0]
        # 审计 P2-1：与 vol_20d 同口径（百分数）——此前为小数，与 vol_20d 差 100 倍
        # 误导调试/展示；树模型对单调变换自适应，无功能影响
        feat["downside_vol"] = float(np.std(neg) * np.sqrt(252) * 100) if len(neg) > 0 else 0.0
    else:
        feat["downside_vol"] = 0.0

    # sortino_60d（索提诺）：sharpe_60d / ttr_60d 已由 nav_score_factors 计算。
    # 无风险利率取 0（简化，公募基金比较口径一致）。
    if len(returns) >= 60:
        r60 = returns[-60:]
        ann_ret = float(r60.mean() * 252)
        neg60 = r60[r60 < 0]
        dsd = float(neg60.std() * np.sqrt(252)) if neg60.size > 0 else 0.0
        feat["sortino_60d"] = ann_ret / dsd if dsd > 1e-10 else 0.0
    else:
        feat["sortino_60d"] = 0.0

    if len(idx_closes) >= 60 and len(returns) >= 60:
        idx_ret = np.diff(idx_closes) / idx_closes[:-1]
        idx_ret = idx_ret[np.isfinite(idx_ret)]
        m = min(60, len(returns), len(idx_ret))
        fr, ir = returns[-m:], idx_ret[-m:]
        up, down = ir > 0, ir < 0
        feat["capture_up"] = float(np.mean(fr[up]) / np.mean(ir[up])) if up.sum() > 0 else 1.0
        feat["capture_down"] = float(np.mean(fr[down]) / np.mean(ir[down])) if down.sum() > 0 else 1.0
    else:
        feat["capture_up"] = feat["capture_down"] = 1.0

    # bias_60d（指数偏离60日均线）已移入 market_state_features（市场层面特征，顺带发现）
    # style_r2（ticket 05 + spec P5）：净值可被板块解释的程度 = 风格清晰度。
    # 回测证明其对 40 日收益有独立正贡献；数据不足/不可解释时降级 0.0（无信号）。
    feat["style_r2"] = 0.0
    if nav_dates is not None and sector_frame is not None and len(navs) >= 61:
        r2 = _style_r2_from_frame(list(nav_dates), np.asarray(navs, dtype=float),
                                  sector_frame)
        if r2 is not None:
            feat["style_r2"] = r2
    return feat


def _ttr_days(navs) -> float:
    """最大回撤恢复时间 TTR（交易日，ticket：风险调整三指标之一）。

    定义：从最深回撤谷底回到**回撤前高点**所需交易日数。
    - 窗口内未恢复 → 返回谷底到窗口末的长度（越长越差，作惩罚）
    - 无回撤 → 0.0
    - 数据不足（<2 点）→ 0.0

    纯函数，供 calc_features / 测试复用。
    """
    arr = np.asarray(navs, dtype=float)
    if arr.size < 2:
        return 0.0
    peak = np.maximum.accumulate(arr)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = (arr - peak) / peak
    trough = int(np.argmin(dd))
    if dd[trough] >= -1e-12:
        return 0.0                      # 窗口内无回撤
    prior_peak = int(np.argmax(arr[:trough + 1]))
    recover = np.nonzero(arr[trough:] >= arr[prior_peak])[0]
    if recover.size == 0:
        return float(arr.size - 1 - trough)   # 未恢复：剩余窗口长度
    return float(recover[0])




def nav_score_factors(navs: np.ndarray) -> dict[str, float]:
    """初筛打分三因子（sharpe_60d / mom_250d / ttr_60d）——净值单一来源。

    compute_fund_features（生产）与回测 PIT 重建共用，避免两套公式漂移。
    与 compute_fund_features 同口径（含缺省值约定）：
    - sharpe_60d：年化收益 / 年化波动（60 日，无风险利率 0）；sd<=1e-10 或样本<60 → 0.0
    - mom_250d：一年累计收益（百分数）；样本<251 → 0.0
    - ttr_60d：最大回撤恢复时间（交易日，_ttr_days）；样本<60 → 0.0
    """
    feat: dict[str, float] = {}
    with np.errstate(divide="ignore", invalid="ignore"):
        returns = np.diff(navs) / navs[:-1]
    returns = returns[np.isfinite(returns)]
    feat["mom_250d"] = float((navs[-1] / navs[-251] - 1) * 100) if len(navs) >= 251 else 0.0
    if len(returns) >= 60:
        r60 = returns[-60:]
        ann_ret = float(r60.mean() * 252)
        sd = float(r60.std() * np.sqrt(252))
        feat["sharpe_60d"] = ann_ret / sd if sd > 1e-10 else 0.0
        feat["ttr_60d"] = _ttr_days(navs[-61:])
    else:
        feat["sharpe_60d"] = 0.0
        feat["ttr_60d"] = 0.0
    return feat


def calc_rbsa(holdings: list[dict], industry_map: dict[str, str] | None = None) -> list[dict]:
    """按持仓权重聚合前 3 大行业暴露。

    industry_map 为预加载的 stock_code→industry_name 映射（由 calc_all_features 一次性载入，
    避免逐持仓查询）。
    """
    industry_weights: dict[str, float] = {}
    for h in holdings:
        stock_code = h["stock_code"]
        industry = (industry_map or {}).get(stock_code) or "其他"
        industry_weights[industry] = industry_weights.get(industry, 0) + h["weight"]
    sorted_industries = sorted(industry_weights.items(), key=lambda x: x[1], reverse=True)
    return [{"industry": ind, "weight": w} for ind, w in sorted_industries[:3]]


def calc_features(code: str,
                  idx_closes: np.ndarray | None = None,
                  idx_volumes: np.ndarray | None = None,
                  sector_frame=None) -> dict:
    """计算单只基金特征并返回（内部函数，仅 calc_all_features / 回测调用）。"""
    rows = repo.nav.series(code)
    if len(rows) < 60:
        logger.warning("基金 %s 净值数据不足 (%d 天)，跳过特征计算", code, len(rows))
        return {}
    dates = [r[0] for r in rows]
    navs = np.array([r[1] for r in rows], dtype=float)
    if idx_closes is None or idx_volumes is None:
        idx_rows = repo.get_index_rows()
        idx_volumes = np.array([r[2] for r in idx_rows], dtype=float) if idx_rows else np.array([])
        idx_closes = np.array([r[1] for r in idx_rows], dtype=float) if idx_rows else np.array([])
    feat = compute_fund_features(navs, idx_closes, idx_volumes,
                                 nav_dates=dates, sector_frame=sector_frame)
    if feat is None:
        return {}
    features: dict = {"code": code, "date": dates[-1]}
    features.update(feat)
    # 数据质量校验：检测 NaN/Inf/极端值
    for key in ("hurst_60d", "momentum_20d", "calmar", "downside_vol",
                 "capture_up", "capture_down"):
        v = features.get(key)
        if v is None or (isinstance(v, float) and (np.isnan(v) or np.isinf(v))):
            logger.warning("基金 %s 特征 %s 异常 (%s)，置为 0.0", code, key, v)
            features[key] = 0.0
    if abs(features.get("momentum_20d", 0)) > 30:
        logger.warning("基金 %s 20日动量异常: %.2f%%", code, features["momentum_20d"])

    return features


def calc_all_features(batch_commit: int = 500) -> int:
    # 连接管理已收敛（Q4-B/Q5）：repo/store 各自取连接，批量路径不再贯穿 conn——
    # 连接工厂缓存 schema 初始化后单次连接成本可忽略，5000 只基金的连接开销 <2%。
    # batch_commit 保留为进度日志节律（提交粒度由 store.save_fund_features 内部负责）。
    all_codes = repo.get_buyable_codes()
    total = len(all_codes)
    # 预加载全局不变的数据，避免逐基金/逐持仓重复查询（N+1）
    industry_map = repo.get_industry_map()
    idx_rows = repo.get_index_rows()
    idx_volumes = np.array([r[2] for r in idx_rows], dtype=float) if idx_rows else np.array([])
    idx_closes = np.array([r[1] for r in idx_rows], dtype=float) if idx_rows else np.array([])
    # style_r2 反推用的板块日涨幅宽表（一次加载，全基金共享；2024-03 起有数据）
    sector_frame = load_sector_pct_frame()
    rbsa_data: dict[str, list[dict]] = {}
    _rbsa_buf: dict[str, list[dict]] = {}
    for code, sc, sn, w in repo.get_latest_holdings_rows():
        _rbsa_buf.setdefault(code, []).append({"stock_code": sc, "stock_name": sn, "weight": w})
    for code, holdings in _rbsa_buf.items():
        top = calc_rbsa(holdings, industry_map)
        if top:
            rbsa_data[code] = top
    logger.info("RBSA 预加载完成: %d 只基金有行业暴露", len(rbsa_data))
    # 行业映射缺失告警：industry_map 为空时 calc_rbsa 会把持仓全部归为"其他"，
    # 直接导致可用赛道清单只剩"其他"、LLM 无法选赛道；此处显式暴露，避免静默降级。
    if rbsa_data:
        _other_cnt = sum(
            1 for tops in rbsa_data.values() if tops and tops[0]["industry"] == "其他")
        if _other_cnt / len(rbsa_data) > 0.3:
            logger.warning(
                "行业映射疑似缺失: RBSA 首位行业为'其他'的基金占 %.0f%% (%d/%d)；"
                "请检查 stock_industry_map 是否为空，必要时运行 --industry-map 强制拉取",
                _other_cnt / len(rbsa_data) * 100, _other_cnt, len(rbsa_data),
            )
    # 大盘状态机：沪深300 close vs MA60 → BULL/BEAR（repo 单一来源）
    regime = repo.get_market_regime()
    logger.info("大盘状态机: %s", regime)
    feature_dates = repo.get_feature_dates_map()
    nav_latest = repo.nav.latest_dates()
    holdings_need_rbsa = set()
    for c in repo.get_codes_missing_rbsa():
        if c in rbsa_data:
            holdings_need_rbsa.add(c)
    # 行业映射更新后，强制重算已过期RBSA
    industry_map_date = repo.get_meta(META.INDUSTRY_MAP_UPDATED)
    if industry_map_date:
        for c in repo.get_feature_codes_before(industry_map_date):
            if c in rbsa_data and c not in holdings_need_rbsa:
                holdings_need_rbsa.add(c)
    # schema 升级 → 旧快照缺列，必须全量重算一次（版本成功后落位，中断则下次重试）
    _prev_schema = repo.get_meta(META.FEATURE_SCHEMA_VERSION)
    schema_upgraded = _prev_schema != _FEATURE_SCHEMA_VERSION
    if schema_upgraded:
        logger.info("特征 schema 升级（%s → %s）：强制全量重算快照",
                    _prev_schema or "无", _FEATURE_SCHEMA_VERSION)
    skip_codes = {
        c for c in all_codes
        if not schema_upgraded
        and c in feature_dates and c in nav_latest and feature_dates[c] >= nav_latest[c]
        and c not in holdings_need_rbsa
    }
    logger.info(
        "待计算特征基金: %d 只, 跳过已最新 %d 只, 强制重算RBSA %d 只",
        total - len(skip_codes), len(skip_codes), len(holdings_need_rbsa),
    )
    done = 0
    saved = 0
    start_time = time.monotonic()
    for code in all_codes:
        if code in skip_codes:
            done += 1
            continue
        features = calc_features(code, idx_closes, idx_volumes, sector_frame)
        done += 1
        if features:
            top = rbsa_data.get(code, [])
            features["regime"] = regime
            features["rbsa_industry_1"] = top[0]["industry"] if len(top) > 0 else ""
            features["rbsa_weight_1"] = top[0]["weight"] if len(top) > 0 else 0.0
            features["rbsa_industry_2"] = top[1]["industry"] if len(top) > 1 else ""
            features["rbsa_weight_2"] = top[1]["weight"] if len(top) > 1 else 0.0
            features["rbsa_industry_3"] = top[2]["industry"] if len(top) > 2 else ""
            features["rbsa_weight_3"] = top[2]["weight"] if len(top) > 2 else 0.0
            save_fund_features(features)
            saved += 1
        if saved % batch_commit == 0:
            elapsed = time.monotonic() - start_time
            speed = done / elapsed if elapsed > 0 else 0
            logger.info("特征计算进度: %d/%d, speed=%.1f/s", done, total, speed)
    # 修剪：每只基金仅保留最近 N 行特征快照，防止历史快照无限累积
    trim_fund_features(_FEATURE_RETENTION_ROWS)
    elapsed = time.monotonic() - start_time
    logger.info("特征计算完成: %d/%d 只基金入库, 耗时 %.1f 秒", saved, total, elapsed)
    # schema 版本在成功后落位（计算异常则不落位，下次仍强制全量）
    if schema_upgraded:
        repo.save_meta(META.FEATURE_SCHEMA_VERSION, _FEATURE_SCHEMA_VERSION)
    return saved

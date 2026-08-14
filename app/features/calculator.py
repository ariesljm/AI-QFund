"""特征计算模块：Hurst、动量、卡玛、RBSA、大盘状态机。"""

from app.repo import meta_keys as META
from app.utils.log import get_logger
import time

import numpy as np
import pandas as pd

from app import domain
import app.repo as repo

logger = get_logger("features")

_FEATURE_RETENTION_ROWS = 250
"""fund_features 每只基金保留的特征快照行数（与净值保留窗口一致，覆盖监控风格漂移的历史查询）。"""

# combo 配方固定系数（combo_score 单一来源）：与 GA 可调权重（regime_combo_weights）区分。
_COMBO_SECTOR_REL_MOMENTUM_W = 0.15   # 赛道相对动量对 combo 的固定贡献
_COMBO_SECTOR_REL_CALMAR_W = 0.05     # 赛道相对卡玛的固定贡献
_COMBO_RBSA_W = 0.003                 # RBSA 行业权重暴露的固定贡献


def sim_trailing_stop(daily_navs: list[float], atr_mult: float = 2.0,
                      atr_period: int = 14, max_days: int = 20) -> float | None:
    """回测用 2×ATR 追踪止损模拟（历史版本对照，监控侧已改用 EMA60 趋势退出）。

    daily_navs: 入场日及之后每日净值（升序，含入场日）。
    从入场起逐日：跟踪最高净值、按净值收益率均值算 ATR(14)，
    回撤 > atr_mult×ATR 即提前结算（止损价 = 触发日净值）；否则持有到 max_days 结算。
    返回结算收益（-1~∞）；数据不足返回 None。
    """
    if len(daily_navs) < 2:
        return None
    entry = daily_navs[0]
    if entry is None or entry <= 0:
        return None
    highest = entry
    rets: list[float] = []
    for i in range(1, min(len(daily_navs), max_days + 1)):
        nav = daily_navs[i]
        if nav is None or nav <= 0:
            break
        if nav > highest:
            highest = nav
        rets.append(nav / daily_navs[i - 1] - 1.0)
        atr = float(np.mean(np.abs(rets[-atr_period:])))
        if atr > 0 and (highest - nav) / highest > atr_mult * atr:
            return nav / entry - 1.0
    settle_idx = min(len(daily_navs) - 1, max_days)
    return daily_navs[settle_idx] / entry - 1.0


def sim_hard_stop(daily_navs: list[float], stop_pct: float = 0.10,
                  max_days: int = 20) -> float | None:
    """模拟硬止损：净值从持仓期最高点回撤超过 stop_pct（如 10%）即提前结算。

    结构同 sim_trailing_stop，但阈值是固定百分比而非 ATR（极端保护场景）。
    返回结算收益（-1~∞）；数据不足返回 None。
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
        if (highest - nav) / highest > stop_pct:
            return nav / entry - 1.0
    settle_idx = min(len(daily_navs) - 1, max_days)
    return daily_navs[settle_idx] / entry - 1.0


_EMA_SPAN = 60
_EMA_CONFIRM_DAYS = 2


def _ema_series(navs: np.ndarray, span: int = _EMA_SPAN) -> np.ndarray:
    """EMA(span) 序列（单一来源：ema60_exit / sim_ema60_exit 共用）。"""
    k = 2.0 / (span + 1.0)
    ema = np.empty(len(navs))
    ema[0] = navs[0]
    for i in range(1, len(navs)):
        ema[i] = navs[i] * k + ema[i - 1] * (1.0 - k)
    return ema


def ema60_trigger_index(navs: list[float], confirm_days: int = _EMA_CONFIRM_DAYS,
                        span: int = _EMA_SPAN) -> int | None:
    """EMA60 连续 confirm 日 < EMA 的首个触发下标；不触发/数据不足返回 None。

    单一来源：ema60_exit（生产防线 R1 判定）、sim_ema60_exit（回测结算）、
    backtest_exit_walkforward（长窗口退出模拟）共用同一触发逻辑。
    navs 从入场日起（含入场日），前 span 日为 EMA 预热期不判定。
    """
    if len(navs) < span + 2:
        return None
    arr = np.asarray(navs, dtype=float)
    if np.any(arr <= 0):
        return None
    below = arr < _ema_series(arr, span)
    for i in range(span, len(below)):
        if below[i - confirm_days + 1:i + 1].all():
            return i
    return None


def ema60_exit(navs: list[float], confirm_days: int = _EMA_CONFIRM_DAYS) -> tuple[bool, str]:
    """EMA60 趋势退出（R1，单一来源）：NAV 连续 confirm_days 日 < EMA60 → 触发。

    生产监控防线 R1 与回测退出模拟共用此判定（替代 2×ATR 追踪止损，后者回测证明负贡献）。
    navs: 入场日及之后每日净值（升序，含入场日）；前 60 日为 EMA 预热期不判定。
    回测验证参数（勿改）：span=60, confirm=2 交易日。
    """
    idx = ema60_trigger_index(navs, confirm_days)
    if idx is None:
        return False, ""
    arr = np.asarray(navs, dtype=float)
    peak = float(np.max(arr[:idx + 1]))
    drawdown = (peak - arr[idx]) / peak
    return True, (
        f"EMA60趋势退出: NAV连续{confirm_days}日<EMA60"
        f"（自高点回撤{drawdown:.2%}）"
    )


def sim_ema60_exit(daily_navs: list[float], confirm_days: int = _EMA_CONFIRM_DAYS,
                   max_days: int = 20) -> float | None:
    """回测用 EMA60 趋势退出模拟：触发日按触发净值结算，否则持有到窗口末。

    与生产防线 R1 同判定（ema60_trigger_index），使回测退出语义 == 生产退出语义；
    触发后视为卖出持现金，收益 = 触发日净值 / 入场净值 - 1。
    注意：EMA 需 span+confirm 日预热，max_days 须大于预热期才可能触发（主回测 20 日
    窗口内生产 R1 本就不触发——这如实反映生产行为）。数据不足返回 None。
    """
    if len(daily_navs) < 2:
        return None
    entry = daily_navs[0]
    if entry is None or entry <= 0:
        return None
    arr = np.asarray(daily_navs[:max_days + 1], dtype=float)
    idx = ema60_trigger_index(arr)
    if idx is not None:
        return arr[idx] / arr[0] - 1.0
    return arr[-1] / arr[0] - 1.0


def market_state_features(idx_close: np.ndarray, idx_vol: np.ndarray) -> dict:
    """市场状态列（单一来源）：指数 20 日动量(%)、20 日波动率(%)。

    R1 绝对收益目标配套：全基金共享的时变特征，让模型感知市场 beta 分量。
    入参为截至决策日的指数历史窗口（不含未来数据，训练/回测无前视）。
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
    return feat


def forward_excess_alpha(nav_at: float, nav_fwd: float, idx_fwd_ret: float) -> float | None:
    """基金前向超额 alpha：基金前向收益 − 指数前向收益（训练样本/回测共用口径）。

    返回 None 表示数据不足（入场净值缺失或非正）。
    """
    if not nav_at or nav_at <= 0:
        return None
    fund_ret = nav_fwd / nav_at - 1.0
    if not np.isfinite(fund_ret):
        return None
    return fund_ret - idx_fwd_ret


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
            deviations = np.cumsum(block - mean_block)
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


def compute_fund_features(navs: np.ndarray, idx_closes: np.ndarray,
                          idx_volumes: np.ndarray) -> dict | None:
    """从净值+指数数组计算 7 个特征（纯函数，不触碰 DB；数据不足返回 None）。

    特征公式单一来源：calc_features / 训练样本 / 回测均复用，避免多套公式漂移。
    """
    if len(navs) < 60:
        return None
    with np.errstate(divide="ignore", invalid="ignore"):
        returns = np.diff(navs) / navs[:-1]
    returns = returns[np.isfinite(returns)]

    feat: dict = {}
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
        feat["downside_vol"] = float(np.std(neg) * np.sqrt(252)) if len(neg) > 0 else 0.0
    else:
        feat["downside_vol"] = 0.0

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

    if len(idx_closes) >= 60:
        idx_ma60 = np.mean(idx_closes[-60:])
        feat["bias_60d"] = float((idx_closes[-1] - idx_ma60) / idx_ma60 * 100)
    else:
        feat["bias_60d"] = 0.0
    return feat


def combo_score(score_norm: float, rel_strength: float, calmar: float, hurst: float,
                w: dict[str, float], sector_rel_momentum: float = 0.0,
                sector_rel_calmar: float = 0.0, rbsa_weight: float = 0.0) -> float:
    """组合打分公式单一来源（主路径/降级路径/回测共用）。

    主路径传 sector_rel + rbsa_weight；降级与回测路径缺失的数据按 0 处理。
    固定系数：赛道相对动量 0.15 / 赛道相对卡玛 0.05 / RBSA 行业权重 0.003，
    为 combo 配方常量（与 regime_combo_weights 的 GA 可调权重区分）。
    """
    return (score_norm * w["model"]
            + rel_strength * w["rs"]
            + sector_rel_momentum * _COMBO_SECTOR_REL_MOMENTUM_W
            + calmar * w["cal"]
            + sector_rel_calmar * _COMBO_SECTOR_REL_CALMAR_W
            + (hurst - 0.5) * 10 * w["hurst"]
            + rbsa_weight * _COMBO_RBSA_W)


def regime_combo_weights(regime: str, cfg: dict) -> dict:
    """根据大盘状态调整因子权重：BULL 偏动量+赫斯特，BEAR 偏卡玛。"""
    w_model = cfg["model_weight"]
    w_rs = cfg["rel_strength_weight"]
    w_cal = cfg["calmar_weight"]
    w_hurst = cfg["hurst_weight"]
    if regime == "BULL":
        w_rs *= 1.3
        w_hurst *= 1.3
        w_cal *= 0.5
    elif regime == "BEAR":
        w_cal *= 1.5
        w_rs *= 0.7
        w_hurst *= 0.5
    return {"model": w_model, "rs": w_rs, "cal": w_cal, "hurst": w_hurst}


def apply_momentum_guard(df: pd.DataFrame, cfg) -> pd.DataFrame:
    """动量护栏过滤：动量不低于门槛的候选保留（推荐排序/回测共用单一判定）。

    cfg 为 RankingConfig 或兼容 dict（阈值经同一入口来源）。
    """
    return df[df["momentum_20d"] >= cfg["momentum_guard_pct"]]


def score_frame(df: pd.DataFrame, model, cfg: dict, idx_mom: float, *,
                default_regime: str = "NEUTRAL",
                rbsa_weight_col: str | None = None,
                sector_rel_momentum_col: str | None = None,
                sector_rel_calmar_col: str | None = None) -> pd.DataFrame:
    """对特征 DataFrame 统一打分：预测 → 相对化 → 归一化 → combo。

    主路径 / 降级路径 / 回测共用。model 为 None 时 score_norm 取 0.5（回测无模型场景）。
    行内已有 regime 列时优先使用，否则回退 default_regime。
    市场状态列（MARKET_COLS）缺失时按 0 填充（防御）：正常调用方须在打分前注入。
    """
    df = df.copy()
    for c in domain.MARKET_COLS:
        if c not in df.columns:
            df[c] = 0.0
    if model is not None:
        X = df[domain.FEATURE_COLS + domain.MARKET_COLS].astype(float)
        df["score"] = model.predict(X)
        df = df[np.isfinite(df["score"])]
        s_min, s_max = df["score"].min(), df["score"].max()
        s_range = s_max - s_min if s_max > s_min else 1.0
        df["score_norm"] = (df["score"] - s_min) / s_range
    else:
        df["score_norm"] = 0.5
    df["rel_strength"] = df["momentum_20d"] - idx_mom
    calmar_clipped = df["calmar"].clip(-5, 5)
    if "regime" in df.columns and len(df) > 0 and pd.notna(df["regime"].iloc[0]):
        regime = df["regime"].iloc[0]
    else:
        regime = default_regime
    w = regime_combo_weights(regime, cfg)
    df["combo"] = combo_score(
        df["score_norm"], df["rel_strength"], calmar_clipped, df["hurst_60d"], w,
        sector_rel_momentum=df[sector_rel_momentum_col] if sector_rel_momentum_col else 0.0,
        sector_rel_calmar=df[sector_rel_calmar_col] if sector_rel_calmar_col else 0.0,
        rbsa_weight=df[rbsa_weight_col] if rbsa_weight_col else 0.0,
    )
    return df


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
                  conn=None) -> dict:
    """计算单只基金特征并返回（内部函数，仅 calc_all_features / 回测调用）。

    ``conn`` 为内部批量 seam：批量路径复用连接，避免逐基金开连接；缺省时自开。
    """
    rows = repo.nav.series(code, conn=conn)
    if len(rows) < 60:
        logger.warning("基金 %s 净值数据不足 (%d 天)，跳过特征计算", code, len(rows))
        return {}
    dates = [r[0] for r in rows]
    navs = np.array([r[1] for r in rows], dtype=float)
    if idx_closes is None:
        idx_rows = repo.get_index_rows(conn=conn)
        idx_volumes = np.array([r[2] for r in idx_rows], dtype=float) if idx_rows else np.array([])
        idx_closes = np.array([r[1] for r in idx_rows], dtype=float) if idx_rows else np.array([])
    feat = compute_fund_features(navs, idx_closes, idx_volumes)
    if feat is None:
        return {}
    features: dict = {"code": code, "date": dates[-1]}
    features.update(feat)
    # 数据质量校验：检测 NaN/Inf/极端值
    for key in ("hurst_60d", "momentum_20d", "calmar", "downside_vol",
                 "capture_up", "capture_down", "bias_60d"):
        v = features.get(key)
        if v is None or (isinstance(v, float) and (np.isnan(v) or np.isinf(v))):
            logger.warning("基金 %s 特征 %s 异常 (%s)，置为 0.0", code, key, v)
            features[key] = 0.0
    if abs(features.get("momentum_20d", 0)) > 30:
        logger.warning("基金 %s 20日动量异常: %.2f%%", code, features["momentum_20d"])
    if abs(features.get("bias_60d", 0)) > 25:
        logger.warning("基金 %s 60日偏离度异常: %.2f%%", code, features["bias_60d"])

    return features


def calc_all_features(batch_commit: int = 500) -> int:
    from app.database import db_conn

    with db_conn() as conn:
        all_codes = repo.get_buyable_codes(conn)
        total = len(all_codes)
        # 预加载全局不变的数据，避免逐基金/逐持仓重复查询（N+1）；
        # 全部走共享连接（conn 口径统一，连接生命周期单一来源）
        industry_map = repo.get_industry_map(conn)
        idx_rows = repo.get_index_rows(conn=conn)
        idx_volumes = np.array([r[2] for r in idx_rows], dtype=float) if idx_rows else np.array([])
        idx_closes = np.array([r[1] for r in idx_rows], dtype=float) if idx_rows else np.array([])
        rbsa_data: dict[str, list[dict]] = {}
        _rbsa_buf: dict[str, list[dict]] = {}
        for code, sc, sn, w in repo.get_latest_holdings_rows(conn):
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
        regime = repo.get_market_regime(conn)
        logger.info("大盘状态机: %s", regime)
        feature_dates = repo.get_feature_dates_map(conn)
        nav_latest = repo.nav.latest_dates(conn)
        holdings_need_rbsa = set()
        for c in repo.get_codes_missing_rbsa(conn):
            if c in rbsa_data:
                holdings_need_rbsa.add(c)
        # 行业映射更新后，强制重算已过期RBSA
        industry_map_date = repo.get_meta(META.INDUSTRY_MAP_UPDATED)
        if industry_map_date:
            for c in repo.get_feature_codes_before(industry_map_date, conn):
                if c in rbsa_data and c not in holdings_need_rbsa:
                    holdings_need_rbsa.add(c)
        skip_codes = {
            c for c in all_codes
            if c in feature_dates and c in nav_latest and feature_dates[c] >= nav_latest[c]
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
            features = calc_features(code, idx_closes, idx_volumes, conn)
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
                repo.save_fund_features(features, conn)
                saved += 1
            if saved % batch_commit == 0:
                conn.commit()
                elapsed = time.monotonic() - start_time
                speed = done / elapsed if elapsed > 0 else 0
                logger.info("特征计算进度: %d/%d, speed=%.1f/s", done, total, speed)
        # 修剪：每只基金仅保留最近 N 行特征快照，防止历史快照无限累积
        repo.trim_fund_features(_FEATURE_RETENTION_ROWS, conn)
        conn.commit()
    elapsed = time.monotonic() - start_time
    logger.info("特征计算完成: %d/%d 只基金入库, 耗时 %.1f 秒", saved, total, elapsed)
    return saved

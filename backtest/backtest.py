"""回测框架：验证推荐排序的预测能力。

用历史数据模拟每日推荐，计算 IC、分位收益差、累计 alpha。
运行：uv run python backtest.py [--start 2024-01-01] [--end 2025-01-01]
"""

import logging
import numpy as np
import pandas as pd

import lightgbm as lgb

from app.features.calculator import (compute_fund_features, score_frame,
                                      apply_momentum_guard,
                                      forward_excess_alpha, sim_trailing_stop,
                                      sim_hard_stop)
from app.model import load as load_model
from app import domain
import app.repo as repo

FEATURE_COLS = repo.FEATURE_COLS
_FORWARD_WINDOW = repo.FORWARD_WINDOW

logger = logging.getLogger("backtest")

_STEP_DAYS = 20
_TOP_N = 5
_BOTTOM_N = 5
_MAX_BT_FUNDS = 2000
# 全量12K基金回测太慢，随机采样2000只。


def _regime_at_date(idx_df: pd.DataFrame, date: pd.Timestamp) -> str:
    row = idx_df.loc[idx_df.index <= date]
    if len(row) == 0:
        return domain.REGIME_NEUTRAL
    last = row.iloc[-1]
    return domain.regime_from_close_ma60(last["close"], last["ma60"])


def _score_funds_at_date(nav_df: pd.DataFrame, idx_df: pd.DataFrame,
                         bt_date: pd.Timestamp, model: lgb.Booster | None,
                         cfg_override: dict | None = None) -> pd.DataFrame:
    """计算某日所有基金的特征并排序。cfg_override 覆盖 ranking 配置（回测对比用）。"""
    idx_close = idx_df["close"]
    idx_vol = idx_df["volume"]
    idx_pos = idx_close.index.get_indexer([bt_date])[0]
    if idx_pos < 0 or idx_pos < 60:
        return pd.DataFrame()
    idx_closes_w = idx_close.iloc[domain.index_window_slice(idx_pos)].to_numpy(dtype=float)
    idx_vols_w = idx_vol.iloc[domain.index_window_slice(idx_pos)].to_numpy(dtype=float)

    regime = _regime_at_date(idx_df, bt_date)
    cfg = repo.get_ranking_cfg()
    if cfg_override:
        cfg = {**cfg.to_dict(), **cfg_override}

    idx_recent = idx_close.iloc[max(0, idx_pos - 20): idx_pos + 1]
    idx_mom = (idx_recent.iloc[-1] / idx_recent.iloc[0] - 1) * 100 if len(idx_recent) >= 21 else 0.0

    records = []
    for code, g in nav_df.groupby("code"):
        g = g.set_index("date")["cum_nav"].sort_index()
        g_end = g.loc[:bt_date]
        if len(g_end) < 60:
            continue
        feat = compute_fund_features(g_end.to_numpy(dtype=float), idx_closes_w, idx_vols_w)
        if feat is None or any(pd.isna(v) for v in feat.values()):
            continue
        feat["code"] = code
        records.append(feat)

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    df = df.dropna(subset=FEATURE_COLS)
    df = apply_momentum_guard(df, cfg)
    if df.empty:
        return df

    return score_frame(df, model, cfg, idx_mom, default_regime=regime)


def _attach_forward_returns(df: pd.DataFrame, nav_df: pd.DataFrame,
                           idx_df: pd.DataFrame, bt_date: pd.Timestamp,
                           stop_mode: str = "none", stop_param: float = 0.0) -> pd.DataFrame:
    """附加20日前向收益（基金绝对收益 + 可选止损模拟）。

    stop_mode: "none"=固定持有；"atr"=追踪止损（stop_param=ATR 倍数）；
    "hard"=硬止损（stop_param=回撤百分比，如 0.10 表示 -10%）。
    """
    idx_close = idx_df["close"]
    idx_pos = idx_close.index.get_indexer([bt_date])[0]
    fwd_pos = idx_pos + _FORWARD_WINDOW
    if fwd_pos >= len(idx_close):
        return df
    idx_fwd_ret = idx_close.iloc[fwd_pos] / idx_close.iloc[idx_pos] - 1.0

    # 预构建 code → Series 映射，避免逐基金 filter
    nav_by_code = {code: g.set_index("date")["cum_nav"].sort_index()
                   for code, g in nav_df.groupby("code")}

    alphas = []
    abs_rets = []
    stop_rets = []
    for _, row in df.iterrows():
        code = row["code"]
        g = nav_by_code.get(code)
        if g is None or bt_date not in g.index:
            alphas.append(np.nan)
            abs_rets.append(np.nan)
            stop_rets.append(np.nan)
            continue
        g_fwd = g.loc[bt_date:]
        if len(g_fwd) < _FORWARD_WINDOW + 1:
            alphas.append(np.nan)
            abs_rets.append(np.nan)
            stop_rets.append(np.nan)
            continue
        nav_at = g.loc[bt_date]
        nav_fwd = g_fwd.iloc[_FORWARD_WINDOW]
        alpha = forward_excess_alpha(nav_at, nav_fwd, idx_fwd_ret)
        alphas.append(alpha if alpha is not None else np.nan)
        # 绝对收益（阶段5：GA fitness / 赚钱口径主标尺）
        abs_ret = nav_fwd / nav_at - 1.0 if nav_at > 0 else np.nan
        abs_rets.append(abs_ret if np.isfinite(abs_ret) else np.nan)
        # 止损模拟（阶段6 续：参数扫描）——逐日净值路径
        daily = g_fwd.iloc[:_FORWARD_WINDOW + 1].tolist()
        if stop_mode == "atr":
            sr = sim_trailing_stop(daily, atr_mult=stop_param)
            stop_rets.append(sr if sr is not None else np.nan)
        elif stop_mode == "hard":
            sr = sim_hard_stop(daily, stop_pct=stop_param)
            stop_rets.append(sr if sr is not None else np.nan)
        else:
            stop_rets.append(abs_ret if np.isfinite(abs_ret) else np.nan)

    df["forward_alpha"] = alphas
    df["forward_abs"] = abs_rets
    df["forward_stop"] = stop_rets
    return df


def run_backtest(start_date: str | None = None, end_date: str | None = None,
                 cfg_override: dict | None = None, fast: bool = False,
                 lookback_days: int = 365,
                 stop_mode: str = "none", stop_param: float = 0.0) -> dict:
    """回测：沿时间轴滑动，计算推荐组合 vs 基准的表现。

    cfg_override：临时覆盖 ranking 配置（guard/权重），用于对比不同参数，不落库。
    fast：快速模式（基金 500 只、步长 40 日），供遗传算法等批量适应度评估降成本。
    lookback_days：未传 start_date 时默认回测最近 N 天（GA 评估用 730 天更稳，
    避免 7 个点小样本过拟合近期 regime）。
    stop_mode/stop_param：止损模拟（"none"/"atr"=ATR倍数/"hard"=回撤百分比），
    供止损参数扫描（阶段6 续）；none 时 forward_stop == forward_abs。
    """
    idx_rows = repo.get_index_series("sh000300", columns=("date", "close", "volume", "ma60"))
    if not idx_rows:
        raise RuntimeError("指数数据缺失")
    idx_df = pd.DataFrame(idx_rows, columns=["date", "close", "volume", "ma60"])
    idx_df["date"] = pd.to_datetime(idx_df["date"])
    idx_df = idx_df.set_index("date").sort_index()

    nav_rows = repo.nav.all_rows()
    nav_df = pd.DataFrame(nav_rows, columns=["code", "date", "cum_nav"])
    nav_df["date"] = pd.to_datetime(nav_df["date"])

    # 随机采样基金，避免全量12K基金逐点特征计算太慢
    sampled_codes = nav_df["code"].unique()
    max_funds = 500 if fast else _MAX_BT_FUNDS
    if len(sampled_codes) > max_funds:
        rng = np.random.default_rng(42)
        sampled_codes = rng.choice(sampled_codes, max_funds, replace=False)
        nav_df = nav_df[nav_df["code"].isin(sampled_codes)]
    logger.info("回测基金样本: %d 只%s", len(sampled_codes), "(fast)" if fast else "")

    all_dates = sorted(idx_df.index.unique())
    if len(all_dates) < 60 + _FORWARD_WINDOW:
        raise RuntimeError("指数数据不足，无法回测")
    min_date = all_dates[60 + _FORWARD_WINDOW]
    max_date = all_dates[-1]
    # 默认只回测最近 12 个月（与训练滚动窗口一致），历史段用 --start/--end 显式指定
    # （避免补全 24 年指数后默认遍历全历史导致每次回测十几分钟）
    if not start_date:
        min_date = max(min_date, all_dates[-1] - pd.Timedelta(days=lookback_days))
    if start_date:
        min_date = max(min_date, pd.Timestamp(start_date))
    if end_date:
        max_date = min(max_date, pd.Timestamp(end_date))

    backtest_dates = [d for d in all_dates if min_date <= d <= max_date][::(_STEP_DAYS * 2 if fast else _STEP_DAYS)]
    logger.info("回测区间: %s ~ %s, 共 %d 个时间点",
                min_date.date(), max_date.date(), len(backtest_dates))

    model = load_model()

    records = []
    for bt_date in backtest_dates:
        scores = _score_funds_at_date(nav_df, idx_df, bt_date, model, cfg_override=cfg_override)
        if len(scores) < _TOP_N + _BOTTOM_N:
            continue
        scores = _attach_forward_returns(scores, nav_df, idx_df, bt_date,
                                         stop_mode=stop_mode, stop_param=stop_param)
        if "forward_alpha" not in scores.columns:
            continue
        scores = scores.dropna(subset=["forward_alpha"])
        if len(scores) < _TOP_N + _BOTTOM_N:
            continue

        top = scores.nlargest(_TOP_N, "combo")
        bottom = scores.nsmallest(_BOTTOM_N, "combo")
        top_alpha = top["forward_alpha"].mean()
        bot_alpha = bottom["forward_alpha"].mean()
        top_abs = top["forward_abs"].mean() if "forward_abs" in top.columns else np.nan
        top_stop = top["forward_stop"].mean() if "forward_stop" in top.columns else np.nan
        spread = top_alpha - bot_alpha
        ic = scores["combo"].corr(scores["forward_alpha"]) if len(scores) >= 5 else 0.0

        records.append({
            "date": bt_date, "top_alpha": top_alpha, "bottom_alpha": bot_alpha,
            "top_abs": top_abs, "top_stop": top_stop, "spread": spread, "ic": ic,
            "regime": _regime_at_date(idx_df, bt_date), "n_funds": len(scores),
        })
        logger.info("%s | regime=%s | top=%.2f%% bot=%.2f%% spread=%.2f%% IC=%.3f n=%d",
                     bt_date.date(), records[-1]["regime"],
                     top_alpha * 100, bot_alpha * 100, spread * 100, ic, len(scores))

    if not records:
        logger.warning("无回测记录")
        return {}

    df = pd.DataFrame(records)
    summary = {
        "periods": len(df),
        "mean_top_alpha_pct": round(df["top_alpha"].mean() * 100, 2),
        "mean_bottom_alpha_pct": round(df["bottom_alpha"].mean() * 100, 2),
        "mean_spread_pct": round(df["spread"].mean() * 100, 2),
        "mean_ic": round(df["ic"].mean(), 4),
        "ic_ir": round(df["ic"].mean() / df["ic"].std(), 4) if df["ic"].std() > 0 else 0,
        "positive_spread_pct": round((df["spread"] > 0).mean() * 100, 1),
        # 阶段5 赚钱口径：Top 组合绝对收益均值 + 赚钱胜率（>1% 决策日占比）
        "mean_top_abs_pct": round(df["top_abs"].mean() * 100, 2),
        "profit_rate_pct": round((df["top_abs"] > domain.PROFIT_THRESHOLD).mean() * 100, 1),
        # 止损版（stop_mode=none 时为固定持有，与上面一致）
        "mean_top_stop_abs_pct": round(float(df["top_stop"].mean()) * 100, 2)
        if df["top_stop"].notna().any() else None,
        "stop_profit_rate_pct": round(float((df["top_stop"] > domain.PROFIT_THRESHOLD).mean()) * 100, 1)
        if df["top_stop"].notna().any() else None,
        "stop_cfg": {"mode": stop_mode, "param": stop_param},
        "bull_periods": int((df["regime"] == "BULL").sum()),
        "bear_periods": int((df["regime"] == "BEAR").sum()),
    }
    logger.info("=== 回测结果 ===")
    for k, v in summary.items():
        logger.info("  %s: %s", k, v)
    return summary


if __name__ == "__main__":
    import sys
    import json
    start = None
    end = None
    cfg_override = None
    for i, arg in enumerate(sys.argv):
        if arg == "--start" and i + 1 < len(sys.argv):
            start = sys.argv[i + 1]
        if arg == "--end" and i + 1 < len(sys.argv):
            end = sys.argv[i + 1]
        if arg == "--cfg" and i + 1 < len(sys.argv):
            cfg_override = json.loads(sys.argv[i + 1])
    run_backtest(start_date=start, end_date=end, cfg_override=cfg_override)
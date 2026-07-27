"""回测框架：验证推荐排序的预测能力。

用历史数据模拟每日推荐，计算 IC、分位收益差、累计 alpha。
运行：uv run python backtest.py [--start 2024-01-01] [--end 2025-01-01]
"""

import logging
import numpy as np
import pandas as pd
from pathlib import Path

import lightgbm as lgb

from data_store import _db_conn
from recommend import (
    _features_from_window, FEATURE_COLS, _FORWARD_WINDOW,
    _load_ranking_cfg, _regime_combo_weights,
)

logger = logging.getLogger("backtest")

_STEP_DAYS = 20
_TOP_N = 5
_BOTTOM_N = 5
_MAX_BT_FUNDS = 2000
"""ponytail: 全量12K基金回测太慢，随机采样2000只。"""


def _regime_at_date(idx_df: pd.DataFrame, date: pd.Timestamp) -> str:
    row = idx_df.loc[idx_df.index <= date]
    if len(row) == 0:
        return "NEUTRAL"
    last = row.iloc[-1]
    if last["ma60"] and last["ma60"] > 0:
        return "BULL" if last["close"] > last["ma60"] else "BEAR"
    return "NEUTRAL"


def _score_funds_at_date(nav_df: pd.DataFrame, idx_df: pd.DataFrame,
                         bt_date: pd.Timestamp, model: lgb.Booster | None) -> pd.DataFrame:
    """计算某日所有基金的特征并排序。"""
    idx_close = idx_df["close"]
    idx_vol = idx_df["volume"]
    idx_pos = idx_close.index.get_indexer([bt_date])[0]
    if idx_pos < 0 or idx_pos < 60:
        return pd.DataFrame()
    idx_closes_w = idx_close.iloc[idx_pos - 59: idx_pos + 1].to_numpy(dtype=float)
    idx_vols_w = idx_vol.iloc[idx_pos - 59: idx_pos + 1].to_numpy(dtype=float)

    regime = _regime_at_date(idx_df, bt_date)
    cfg = _load_ranking_cfg()
    w = _regime_combo_weights(regime, cfg)

    idx_recent = idx_close.iloc[max(0, idx_pos - 20): idx_pos + 1]
    idx_mom = (idx_recent.iloc[-1] / idx_recent.iloc[0] - 1) * 100 if len(idx_recent) >= 21 else 0.0

    records = []
    for code, g in nav_df.groupby("code"):
        g = g.set_index("date")["cum_nav"].sort_index()
        g_end = g.loc[:bt_date]
        if len(g_end) < 60:
            continue
        feat = _features_from_window(g_end.to_numpy(dtype=float), idx_closes_w, idx_vols_w)
        if feat is None or any(pd.isna(v) for v in feat.values()):
            continue
        feat["code"] = code
        records.append(feat)

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    df = df.dropna(subset=FEATURE_COLS)
    df = df[df["momentum_20d"] >= cfg["momentum_guard_pct"]]
    if df.empty:
        return df

    df["rel_strength"] = df["momentum_20d"] - idx_mom
    calmar_clipped = df["calmar"].clip(-5, 5)

    if model is not None:
        X = df[FEATURE_COLS].astype(float)
        df["score"] = model.predict(X)
        df = df[np.isfinite(df["score"])]
        s_min, s_max = df["score"].min(), df["score"].max()
        s_range = s_max - s_min if s_max > s_min else 1.0
        df["score_norm"] = (df["score"] - s_min) / s_range
    else:
        df["score_norm"] = 0.5

    df["combo"] = (
        df["score_norm"] * w["model"]
        + df["rel_strength"] * w["rs"]
        + calmar_clipped * w["cal"]
        + (df["hurst_60d"] - 0.5) * 10 * w["hurst"]
    )
    return df


def _attach_forward_returns(df: pd.DataFrame, nav_df: pd.DataFrame,
                           idx_df: pd.DataFrame, bt_date: pd.Timestamp) -> pd.DataFrame:
    """附加20日前向收益（基金超额 alpha）。"""
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
    for _, row in df.iterrows():
        code = row["code"]
        g = nav_by_code.get(code)
        if g is None or bt_date not in g.index:
            alphas.append(np.nan)
            continue
        g_fwd = g.loc[bt_date:]
        if len(g_fwd) < _FORWARD_WINDOW + 1:
            alphas.append(np.nan)
            continue
        nav_at = g.loc[bt_date]
        nav_fwd = g_fwd.iloc[_FORWARD_WINDOW]
        if not nav_at or nav_at <= 0:
            alphas.append(np.nan)
            continue
        fund_ret = nav_fwd / nav_at - 1.0
        alphas.append(fund_ret - idx_fwd_ret if np.isfinite(fund_ret) else np.nan)

    df["forward_alpha"] = alphas
    return df


def run_backtest(start_date: str | None = None, end_date: str | None = None) -> dict:
    """回测：沿时间轴滑动，计算推荐组合 vs 基准的表现。"""
    with _db_conn() as conn:
        idx_rows = conn.execute(
            "SELECT date, close, volume, ma60 FROM index_daily "
            "WHERE code='sh000300' ORDER BY date ASC"
        ).fetchall()
        if not idx_rows:
            raise RuntimeError("指数数据缺失")
        idx_df = pd.DataFrame(idx_rows, columns=["date", "close", "volume", "ma60"])
        idx_df["date"] = pd.to_datetime(idx_df["date"])
        idx_df = idx_df.set_index("date").sort_index()

        nav_rows = conn.execute(
            "SELECT code, date, cum_nav FROM fund_nav ORDER BY code, date ASC"
        ).fetchall()
    nav_df = pd.DataFrame(nav_rows, columns=["code", "date", "cum_nav"])
    nav_df["date"] = pd.to_datetime(nav_df["date"])

    # 随机采样基金，避免全量12K基金逐点特征计算太慢
    sampled_codes = nav_df["code"].unique()
    if len(sampled_codes) > _MAX_BT_FUNDS:
        rng = np.random.default_rng(42)
        sampled_codes = rng.choice(sampled_codes, _MAX_BT_FUNDS, replace=False)
        nav_df = nav_df[nav_df["code"].isin(sampled_codes)]
    logger.info("回测基金样本: %d 只", len(sampled_codes))

    all_dates = sorted(idx_df.index.unique())
    if len(all_dates) < 60 + _FORWARD_WINDOW:
        raise RuntimeError("指数数据不足，无法回测")
    min_date = all_dates[60 + _FORWARD_WINDOW]
    max_date = all_dates[-1]
    if start_date:
        min_date = max(min_date, pd.Timestamp(start_date))
    if end_date:
        max_date = min(max_date, pd.Timestamp(end_date))

    backtest_dates = [d for d in all_dates if min_date <= d <= max_date][::_STEP_DAYS]
    logger.info("回测区间: %s ~ %s, 共 %d 个时间点",
                min_date.date(), max_date.date(), len(backtest_dates))

    model = None
    model_path = Path("models/lgb_model.txt")
    if model_path.exists():
        try:
            model = lgb.Booster(model_file=str(model_path))
            logger.info("已加载模型: %s", model_path)
        except Exception:
            pass

    records = []
    for bt_date in backtest_dates:
        scores = _score_funds_at_date(nav_df, idx_df, bt_date, model)
        if len(scores) < _TOP_N + _BOTTOM_N:
            continue
        scores = _attach_forward_returns(scores, nav_df, idx_df, bt_date)
        if "forward_alpha" not in scores.columns:
            continue
        scores = scores.dropna(subset=["forward_alpha"])
        if len(scores) < _TOP_N + _BOTTOM_N:
            continue

        top = scores.nlargest(_TOP_N, "combo")
        bottom = scores.nsmallest(_BOTTOM_N, "combo")
        top_alpha = top["forward_alpha"].mean()
        bot_alpha = bottom["forward_alpha"].mean()
        spread = top_alpha - bot_alpha
        ic = scores["combo"].corr(scores["forward_alpha"]) if len(scores) >= 5 else 0.0

        records.append({
            "date": bt_date, "top_alpha": top_alpha, "bottom_alpha": bot_alpha,
            "spread": spread, "ic": ic,
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
        "bull_periods": int((df["regime"] == "BULL").sum()),
        "bear_periods": int((df["regime"] == "BEAR").sum()),
    }
    logger.info("=== 回测结果 ===")
    for k, v in summary.items():
        logger.info("  %s: %s", k, v)
    return summary


if __name__ == "__main__":
    import sys
    import log_utils  # noqa
    start = None
    end = None
    for i, arg in enumerate(sys.argv):
        if arg == "--start" and i + 1 < len(sys.argv):
            start = sys.argv[i + 1]
        if arg == "--end" and i + 1 < len(sys.argv):
            end = sys.argv[i + 1]
    run_backtest(start_date=start, end_date=end)
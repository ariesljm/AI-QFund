"""walk-forward 回测验证模块（无前视偏差）——回测标尺第一版。

对每个回测决策日 bt_date（每 FORWARD_WINDOW 个交易日一次）：
  1. 用 [bt_date-365 天, bt_date] 12 个月滚动窗口重训 LightGBM（时间衰减权重，半衰期 90 天）
  2. 对 bt_date 横截面现算特征 → 动量护栏过滤 → 模型打分 → TopN 组合
  3. 计算 20 日绝对收益 / 相对沪深300 超额
  4. 规则门判定（BEAR / 指数20日动量 / 250日涨幅分位）→ 出手 or 拒绝

汇总口径（对应 grilling 共识标尺）：
  - 全期：所有决策日的 TopN 表现（不看门，现状基准）
  - 出手日：TopN 20日绝对收益均值 / 绝对胜率（主标尺，目标 A）
  - 门拒绝日：假设 TopN 事后绝对收益（验证门是否避开亏钱）
  - 随机基线：同池随机抽 N 只的绝对收益对照

运行：uv run python backtest_walkforward.py [--start 2017-01-01] [--end 2026-08-04]
      [--gate none|rules] [--mom-threshold -3.0] [--pct-threshold 90]
      [--pct-window 250] [--pct-lookback 750] [--topn 5] [--pool 0] [--limit N]
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

from app import domain
from app import repo
from app.engine.quality import profit_stats  # 赚钱口径单一来源（回测汇总与质量度量共用）
from app.features.calculator import (compute_fund_features, score_frame,
                                      market_state_features, sim_ema60_exit)
from app.model import prepare_training_data, train  # 训练采样/训练单一来源（与线上同口径）

logger = logging.getLogger("backtest_walkforward")

FEATURE_COLS = repo.FEATURE_COLS
MARKET_COLS = repo.MARKET_COLS
_FORWARD_WINDOW = repo.FORWARD_WINDOW
_STEP_DAYS = 20  # 决策频率：每 20 交易日一次（与预测窗口对齐，避免重叠窗口伪独立）
_TRAIN_FUNDS = 2000  # 训练采样基金数（与线上 model.prepare 一致）
_DEFAULT_START = "2021-01-01"
_DEFAULT_END = "2026-08-04"


# ========== 1. walk-forward 模型训练 ==========

def _train_window(t_max: pd.Timestamp, fund_codes: list[str],
                  window_days: int = 365) -> lgb.Booster | None:
    """用 [t_max-window_days, t_max] 窗口构建模型——复用生产训练采样（单一来源）。

    调用 model.prepare_training_data(window_end=t_max, fund_codes=fund_codes)：
    采样/特征/目标/时间衰减权重与线上完全同口径（严格无前视，样本不晚于 t_max），
    消除"回测里那份训练逻辑 ≠ 线上那份"的标尺漂移。返回 None 表示样本不足。
    """
    X_train, y_train, w_train, *_ = prepare_training_data(
        window_end=t_max, fund_codes=fund_codes, window_days=window_days)
    if len(X_train) < 500:
        logger.warning("训练样本不足: %d", len(X_train))
        return None
    return train(X_train, y_train, w_train, save_path=None)


# ========== 2. 横截面打分 ==========

def _score_at(bst: lgb.Booster, bt_date: pd.Timestamp, idx_close: pd.Series,
              idx_vol: pd.Series, pool_codes: list[str],
              guard: float, sector_map: dict[str, str] | None = None) -> pd.DataFrame:
    """对 bt_date 横截面打分，返回含 score/combo/alpha/abs_ret 的 DataFrame。

    alpha = 基金20日收益 − 指数20日收益（相对超额）
    abs_ret = 基金20日绝对收益（目标 A 主标尺）
    """
    idx_ret_fwd = idx_close.shift(-_FORWARD_WINDOW) / idx_close - 1.0
    if bt_date not in idx_ret_fwd.index or pd.isna(idx_ret_fwd[bt_date]):
        return pd.DataFrame()
    idx_pos = idx_close.index.get_indexer([bt_date])[0]
    if idx_pos < 60:
        return pd.DataFrame()

    records = []
    for code in pool_codes:
        rows = repo.nav.series(code)
        dates = [pd.Timestamp(r[0]) for r in rows]
        navs = [r[1] for r in rows]
        if bt_date not in dates:
            continue
        pos = dates.index(bt_date)
        if pos < 60 or pos + _FORWARD_WINDOW >= len(dates):
            continue
        abs_ret = navs[pos + _FORWARD_WINDOW] / navs[pos] - 1.0
        feat = compute_fund_features(
            np.array(navs[:pos + 1], dtype=float),
            idx_close.iloc[idx_pos - 59: idx_pos + 1].to_numpy(dtype=float),
            idx_vol.iloc[idx_pos - 59: idx_pos + 1].to_numpy(dtype=float),
        )
        if feat is None or any(pd.isna(v) for v in feat.values()):
            continue
        # R1：注入市场状态列（与训练同口径）
        feat.update(market_state_features(
            idx_close.iloc[idx_pos - 59: idx_pos + 1].to_numpy(dtype=float),
            idx_vol.iloc[idx_pos - 59: idx_pos + 1].to_numpy(dtype=float),
        ))
        feat["code"] = code
        feat["alpha"] = abs_ret - idx_ret_fwd[bt_date]
        feat["abs_ret"] = abs_ret
        records.append(feat)
    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    df = df.dropna(subset=FEATURE_COLS)
    df = df[df["momentum_20d"] >= guard]

    # 贴近线上：按 combo（模型分 + 相对强度 + 卡玛 + Hurst 加权）排序
    idx_recent = idx_close.iloc[max(0, idx_pos - 20): idx_pos + 1]
    idx_mom = (idx_recent.iloc[-1] / idx_recent.iloc[0] - 1) * 100 if len(idx_recent) >= 21 else 0.0
    cfg = repo.get_ranking_cfg()
    regime = _regime_at(idx_close, bt_date)
    df = score_frame(df, bst, cfg, idx_mom, default_regime=regime)
    if sector_map is not None:
        # 赛道归属用最新 RBSA 快照近似（fund_holdings 历史快照不全，无法按时点重算；有前视偏差，解读时打折）
        df["sector"] = df["code"].map(sector_map)
    return df


def _regime_at(idx_close: pd.Series, bt_date: pd.Timestamp) -> str:
    """回测日 regime：收盘 vs 当日 EMA60（读指数表 ma60 列，与线上口径一致）。"""
    rows = repo.get_index_series("sh000300", ("date", "close", "ma60"),
                                 since=bt_date.strftime("%Y-%m-%d"))
    if not rows or rows[0][0] != bt_date.strftime("%Y-%m-%d"):
        return domain.REGIME_NEUTRAL
    _, close, ma60 = rows[0]
    if ma60 is None:
        return domain.REGIME_NEUTRAL
    return domain.regime_from_close_ma60(float(close), float(ma60))


# ========== 3. 规则门（可回测的绝对门 + 防追高护栏） ==========

def _pctile_ret(idx_close: pd.Series, bt_pos: int, pct_window: int,
                pct_lookback: int) -> float | None:
    """当前 pct_window 日涨幅在过去 pct_lookback 个滚动涨幅中的分位（0-100）。

    历史不足时返回 None（门跳过该条件，不误杀）。
    """
    if bt_pos < pct_window + pct_lookback:
        return None
    seg = idx_close.iloc[bt_pos - pct_window - pct_lookback + 1: bt_pos + 1].to_numpy(dtype=float)
    # 当前涨幅 = 最近 pct_window 日（seg[-1] 为当前价，seg[-1-pct_window] 为 pct_window 日前价）
    cur = seg[-1] / seg[-1 - pct_window] - 1.0
    # 历史滚动涨幅：seg[i+pct_window]/seg[i] 为第 i 个 pct_window 日涨幅（共 pct_lookback 个）
    roll = seg[pct_window:] / seg[: -pct_window] - 1.0
    return float((roll < cur).mean() * 100)


def gate_verdict(close: float, ma60: float, mom20: float, pctile: float | None,
                 mom_threshold: float, pct_threshold: float) -> tuple[bool, list[str]]:
    """规则门：任一条件触发 → 不可投。返回 (是否可投, 触发原因列表)。"""
    reasons: list[str] = []
    if ma60 is not None and close < ma60:
        reasons.append("BEAR(收盘<EMA60)")
    if mom20 * 100 < mom_threshold:
        reasons.append(f"指数20日动量{mom20 * 100:.1f}%<{mom_threshold}%")
    if pctile is not None and pctile > pct_threshold:
        reasons.append(f"250日涨幅分位{pctile:.0f}%>{pct_threshold}%")
    return (not reasons, reasons)


# ========== 4. 单决策点处理（worker 进程入口） ==========

def _sector_point(df: pd.DataFrame, bt_date: pd.Timestamp, rng_seed: int,
                  regime: str = "NEUTRAL") -> dict | None:
    """赛道内模式单点汇总：随机选 2 个赛道，对比每赛道量化 Top1 vs 随机 1 只。

    回答「假定 LLM 赛道选得好，量化排序在赛道内是否有区分度」：
    - 随机选赛道（种子按日期派生）＝零假设，隔离赛道质量变量；
    - Top1 vs 随机：比较每 20 日绝对收益/超额 alpha/胜率；
    - ic_sector：赛道内 combo 与 alpha 的秩相关均值。
    """
    if "sector" not in df.columns or df["sector"].isna().all():
        return None
    df = df.dropna(subset=["alpha", "abs_ret", "sector"])
    valid = [s for s, g in df.groupby("sector") if len(g) >= 10]
    if not valid:
        return None
    rng = np.random.default_rng(rng_seed)
    chosen = rng.choice(valid, size=min(2, len(valid)), replace=False)

    top1_abs, rand_abs = [], []
    top1_alpha, rand_alpha = [], []
    top1_win, rand_win = [], []
    top2_abs, top2_alpha, top2_win = [], [], []
    ics = []
    for s in chosen:
        sdf = df[df["sector"] == s].sort_values("combo", ascending=False)
        top1 = sdf.iloc[0]
        top2 = sdf.iloc[1] if len(sdf) >= 2 else None
        rnd = sdf.iloc[rng.integers(0, len(sdf))]
        top1_abs.append(top1["abs_ret"]); rand_abs.append(rnd["abs_ret"])
        top1_alpha.append(top1["alpha"]); rand_alpha.append(rnd["alpha"])
        top1_win.append(top1["abs_ret"] > 0); rand_win.append(rnd["abs_ret"] > 0)
        if top2 is not None:
            top2_abs.append(top2["abs_ret"])
            top2_alpha.append(top2["alpha"])
            top2_win.append(top2["abs_ret"] > 0)
        ics.append(float(sdf["combo"].corr(sdf["alpha"])) if len(sdf) >= 5 else 0.0)

    n2 = len(top2_abs)
    return {
        "date": bt_date.strftime("%Y-%m-%d"),
        "regime": regime,
        "n_sectors": int(len(chosen)),
        "top1_abs_pct": round(float(np.mean(top1_abs)) * 100, 3),
        "rand_abs_pct": round(float(np.mean(rand_abs)) * 100, 3),
        "top1_alpha_pct": round(float(np.mean(top1_alpha)) * 100, 3),
        "rand_alpha_pct": round(float(np.mean(rand_alpha)) * 100, 3),
        "top1_win": int(np.mean(top1_win)),
        "rand_win": int(np.mean(rand_win)),
        "top2_abs_pct": round(float(np.mean(top2_abs)) * 100, 3) if n2 else None,
        "top2_alpha_pct": round(float(np.mean(top2_alpha)) * 100, 3) if n2 else None,
        "top2_win": int(np.mean(top2_win)) if n2 else None,
        "top1_vs_top2_gap": round(float(np.mean(np.array(top1_abs[:n2]) - np.array(top2_abs))) * 100, 3) if n2 else None,
        "ic_sector": round(float(np.mean(ics)), 4),
        "n_funds": int(len(df)),
    }


def _process_point(bt_date_str: str, gate: str, mom_threshold: float,
                   pct_threshold: float, pct_window: int, pct_lookback: int,
                   topn: int, pool_n: int, train_pool: int,
                   mode: str = "market", profit_threshold: float = domain.PROFIT_THRESHOLD * 100) -> dict | None:
    """单个决策点：动态采样 → 重训 → 打分 → 门判定 → 返回记录。

    供 ProcessPoolExecutor 并行调用；worker 内自加载指数与配置，
    避免跨进程传递大对象与全局状态。
    """
    bt_date = pd.Timestamp(bt_date_str)
    idx_rows = repo.get_index_series("sh000300", ("date", "close", "volume", "ma60"))
    if not idx_rows:
        return None
    idx_df = pd.DataFrame(idx_rows, columns=["date", "close", "volume", "ma60"])
    idx_df["date"] = pd.to_datetime(idx_df["date"])
    idx_df = idx_df.set_index("date").sort_index()
    idx_close, idx_vol = idx_df["close"], idx_df["volume"]

    cfg = repo.get_ranking_cfg()
    guard = cfg["momentum_guard_pct"]

    # 训练/打分采样均按回测日动态：只用截止 bt_date 存量基金
    # （避免幸存者偏差 + 早期回测点无历史数据）；查询经 repo 统一读 seam
    fund_codes = repo.sample_fund_codes_before(
        bt_date.strftime("%Y-%m-%d"), 60 + _FORWARD_WINDOW, train_pool)
    pool_codes = repo.sample_fund_codes_before(
        bt_date.strftime("%Y-%m-%d"), 60 + _FORWARD_WINDOW, pool_n)

    bst = _train_window(bt_date, fund_codes)
    if bst is None:
        logger.warning("跳过 %s（训练样本不足）", bt_date.date())
        return None

    if mode == "sector":
        # 赛道归属：最新 RBSA 快照（当前时点，历史点有前视近似）
        sector_map = repo.get_latest_rbsa_sector_map()
        df = _score_at(bst, bt_date, idx_close, idx_vol, pool_codes, guard,
                       sector_map=sector_map)
        if len(df) < 20:
            logger.warning("跳过 %s（横截面不足 %d）", bt_date.date(), len(df))
            return None
        rec = _sector_point(df, bt_date, int(bt_date.strftime("%Y%m%d")), _regime_at(idx_close, bt_date))
        if rec is None:
            logger.warning("跳过 %s（无可投赛道）", bt_date.date())
        return rec

    df = _score_at(bst, bt_date, idx_close, idx_vol, pool_codes, guard)
    if len(df) < max(topn * 2, 30):
        logger.warning("跳过 %s（横截面不足 %d）", bt_date.date(), len(df))
        return None
    df = df.dropna(subset=["alpha", "abs_ret"])

    top = df.nlargest(topn, "combo")
    top_abs = float(top["abs_ret"].mean())
    top_alpha = float(top["alpha"].mean())
    ic = float(df["combo"].corr(df["alpha"])) if len(df) >= 5 else 0.0
    # 单基金明细：阶段0验收基线用（摊平后算赚钱胜率/盈亏比，比TopN均值口径更接近真实推荐体验）
    top_fund_abs = ",".join(f"{x * 100:.2f}" for x in top["abs_ret"].tolist())
    # 退出模拟：EMA60 趋势退出（与生产防线 R1 同判定）——触发即卖出持现金，否则持有到窗口末
    stop_rets = []
    bt_date_str2 = bt_date.strftime("%Y-%m-%d")
    for code in top["code"].tolist():
        navs = repo.nav.series(code, since=bt_date_str2, limit=_FORWARD_WINDOW + 1)
        sr = sim_ema60_exit([r[1] for r in navs]) if len(navs) > 1 else None
        stop_rets.append(f"{sr * 100:.2f}" if sr is not None else "")

    # 随机基线：同池固定种子（按日期派生，并行下可复现）抽 topn 只
    rng = np.random.default_rng(int(bt_date.strftime("%Y%m%d")))
    base = df.iloc[rng.choice(len(df), topn, replace=False)]
    base_abs = float(base["abs_ret"].mean())

    # 规则门
    bt_pos = idx_close.index.get_indexer([bt_date])[0]
    close = float(idx_close.iloc[bt_pos])
    ma60 = float(idx_df["ma60"].iloc[bt_pos]) if pd.notna(idx_df["ma60"].iloc[bt_pos]) else None
    mom20 = close / float(idx_close.iloc[bt_pos - _STEP_DAYS]) - 1.0 if bt_pos >= _STEP_DAYS else 0.0
    pctile = _pctile_ret(idx_close, bt_pos, pct_window, pct_lookback)
    if gate == "rules":
        investable, reasons = gate_verdict(close, ma60, mom20, pctile,
                                           mom_threshold, pct_threshold)
    else:
        investable, reasons = True, []

    return {
        "date": bt_date.strftime("%Y-%m-%d"),
        "regime": _regime_at(idx_close, bt_date),
        "investable": investable,
        "reasons": "|".join(reasons),
        "top_abs_pct": round(top_abs * 100, 3),
        "top_fund_abs_pct": top_fund_abs,
        "stop_fund_abs_pct": ",".join(stop_rets),
        "top_alpha_pct": round(top_alpha * 100, 3),
        "ic": round(ic, 4),
        "baseline_abs_pct": round(base_abs * 100, 3),
        "mom20_pct": round(mom20 * 100, 2),
        "pctile": round(pctile, 1) if pctile is not None else None,
        "n_funds": len(df),
    }


# ========== 5. 回测主循环（并行决策点） ==========

def run_walkforward(start: str, end: str, gate: str, mom_threshold: float,
                    pct_threshold: float, pct_window: int, pct_lookback: int,
                    topn: int, pool: int, limit: int | None,
                    out_stem: str = "backtest_walkforward",
                    workers: int = 4, train_pool: int = 1200,
                    mode: str = "market", profit_threshold: float = domain.PROFIT_THRESHOLD * 100) -> dict:
    idx_rows = repo.get_index_series("sh000300", ("date", "close", "volume", "ma60"))
    if not idx_rows:
        raise RuntimeError("沪深300指数数据缺失")
    idx_df = pd.DataFrame(idx_rows, columns=["date", "close", "volume", "ma60"])
    idx_df["date"] = pd.to_datetime(idx_df["date"])
    idx_df = idx_df.set_index("date").sort_index()

    all_dates = sorted(idx_df.index.unique())
    bt_dates = [d for d in all_dates if pd.Timestamp(start) <= d <= pd.Timestamp(end)][::_STEP_DAYS]
    if limit:
        bt_dates = bt_dates[:limit]
    logger.info("回测区间: %s ~ %s, %d 个决策点, workers=%d, 训练池=%d, 打分池=%d",
                start, end, len(bt_dates), workers, train_pool,
                pool if pool and pool > 0 else 1000)

    pool_n = pool if pool and pool > 0 else 1000
    t0 = time.monotonic()
    records: list[dict] = []
    if workers > 1:
        from concurrent.futures import ProcessPoolExecutor, as_completed
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_process_point, d.strftime("%Y-%m-%d"), gate,
                              mom_threshold, pct_threshold, pct_window, pct_lookback,
                              topn, pool_n, train_pool, mode,
                              profit_threshold): d for d in bt_dates}
            done = 0
            for fut in as_completed(futs):
                rec = fut.result()
                done += 1
                if rec:
                    records.append(rec)
                    if mode == "sector":
                        gap = rec.get("top1_vs_top2_gap")
                        logger.info("[%d/%d] %s regime=%s top1=%.2f%% top2=%s 随机=%.2f%% "
                                    "alpha=%.2f%%/%.2f%% 赛道IC=%.3f n=%d gap=%s",
                                    done, len(bt_dates), rec["date"], rec["regime"],
                                    rec["top1_abs_pct"],
                                    f"{rec['top2_abs_pct']:.2f}%" if rec.get("top2_abs_pct") is not None else "N/A",
                                    rec["rand_abs_pct"],
                                    rec["top1_alpha_pct"], rec["rand_alpha_pct"],
                                    rec["ic_sector"], rec["n_funds"],
                                    f"{gap:.2f}%" if gap is not None else "N/A")
                    else:
                        logger.info("[%d/%d] %s regime=%s 出手=%s top_abs=%.2f%% top_alpha=%.2f%% "
                                    "基线=%.2f%% IC=%.3f n=%d",
                                    done, len(bt_dates), rec["date"], rec["regime"],
                                    rec["investable"], rec["top_abs_pct"], rec["top_alpha_pct"],
                                    rec["baseline_abs_pct"], rec["ic"], rec["n_funds"])
    else:
        for bt_date in bt_dates:
            rec = _process_point(bt_date.strftime("%Y-%m-%d"), gate, mom_threshold,
                                 pct_threshold, pct_window, pct_lookback, topn,
                                 pool_n, train_pool, mode, profit_threshold)
            if rec:
                records.append(rec)
                logger.info("%s 完成", rec["date"])
    records.sort(key=lambda r: r["date"])

    if not records:
        logger.error("无有效回测记录")
        return {}

    out_df = pd.DataFrame(records)
    out_path = Path(f"data/{out_stem}.csv")
    out_df.to_csv(out_path, index=False)
    logger.info("明细已保存: %s", out_path)

    return _summarize(out_df, gate, mom_threshold, pct_threshold, pct_window,
                      pct_lookback, topn, time.monotonic() - t0, out_stem, mode,
                      profit_threshold)


def _summarize(df: pd.DataFrame, gate: str, mom_threshold: float, pct_threshold: float,
               pct_window: int, pct_lookback: int, topn: int, elapsed: float,
               out_stem: str = "backtest_walkforward", mode: str = "market",
               profit_threshold: float = domain.PROFIT_THRESHOLD * 100) -> dict:
    """汇总口径：全期 / 出手日 / 门拒绝日 / 随机基线（market）；赛道内 Top1 vs 随机（sector）。

    profit_threshold：赚钱阈值（%），默认 1.0（覆盖申赎成本后的真赚钱）。
    """

    if mode == "sector":
        n = len(df)
        top2_block = None
        if df["top2_abs_pct"].notna().any():
            sub = df[df["top2_abs_pct"].notna()]
            top2_block = {
                "points": len(sub),
                "abs_pct": round(float(sub["top2_abs_pct"].mean()), 3),
                "win_rate_pct": round(float(sub["top2_win"].mean() * 100), 1),
                "alpha_pct": round(float(sub["top2_alpha_pct"].mean()), 3),
                "top1_vs_top2_gap_pct": round(float(sub["top1_vs_top2_gap"].mean()), 3),
            }
        summary = {
            "mode": "sector",
            "points_total": n,
            "sector_top1": {
                "points": n,
                "abs_pct": round(float(df["top1_abs_pct"].mean()), 3),
                "win_rate_pct": round(float(df["top1_win"].mean() * 100), 1),
                "alpha_pct": round(float(df["top1_alpha_pct"].mean()), 3),
            },
            "sector_random": {
                "points": n,
                "abs_pct": round(float(df["rand_abs_pct"].mean()), 3),
                "win_rate_pct": round(float(df["rand_win"].mean() * 100), 1),
                "alpha_pct": round(float(df["rand_alpha_pct"].mean()), 3),
            },
            "sector_top2": top2_block,
            "top1_beats_random_pct": round(float((df["top1_abs_pct"] > df["rand_abs_pct"]).mean() * 100), 1),
            "ic_sector_mean": round(float(df["ic_sector"].mean()), 4),
            "n_sectors_avg": round(float(df["n_sectors"].mean()), 1),
            "gate_cfg": {"mode": mode},
            "elapsed_sec": round(elapsed, 1),
        }
        print("\n========== walk-forward 赛道内回测汇总 ==========")
        print(f"点数   : {n}")
        print(f"Top1   : 绝对收益均值={summary['sector_top1']['abs_pct']}%  "
              f"胜率={summary['sector_top1']['win_rate_pct']}%  alpha={summary['sector_top1']['alpha_pct']}%")
        print(f"Top2   : 绝对收益均值={top2_block['abs_pct'] if top2_block else 'N/A'}%  "
              f"胜率={top2_block['win_rate_pct'] if top2_block else 'N/A'}%")
        print(f"随机   : 绝对收益均值={summary['sector_random']['abs_pct']}%  "
              f"胜率={summary['sector_random']['win_rate_pct']}%  alpha={summary['sector_random']['alpha_pct']}%")
        print(f"Top1 跑赢随机: {summary['top1_beats_random_pct']}% | 赛道内IC均值: {summary['ic_sector_mean']}")
        if top2_block:
            print(f"Top1 vs Top2 差距: {top2_block['top1_vs_top2_gap_pct']}% (20日绝对收益差, 正=Top1更优)")
        print(f"耗时: {elapsed:.0f}s")
        print("==========================================")
        summary_path = Path(f"data/{out_stem}_summary.json")
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("汇总已保存: %s", summary_path)
        return summary

    def _block(sub: pd.DataFrame) -> dict:
        n = len(sub)
        if n == 0:
            return {"points": 0, "top_abs_pct": None, "win_rate_pct": None,
                    "profit_rate_pct": None, "payoff_ratio": None,
                    "alpha_pct": None, "ic": None}
        rets = [float(v) for v in sub["top_abs_pct"] if v is not None]
        ps = profit_stats(rets, threshold=profit_threshold)  # 赚钱口径单一来源
        return {
            "points": n,
            "top_abs_pct": round(float(np.mean(rets)), 3),
            "win_rate_pct": round(ps["win_rate"] * 100, 1),
            # 赚钱胜率 = 扣费后真赚钱（绝对收益 > 阈值）的决策日占比（TopN 均值口径）
            "profit_rate_pct": round(ps["profit_rate"] * 100, 1),
            "payoff_ratio": round(ps["payoff_ratio"], 2) if ps["payoff_ratio"] else None,
            "alpha_pct": round(float(sub["top_alpha_pct"].mean()), 3),
            "ic": round(float(sub["ic"].mean()), 4),
        }

    all_ = _block(df)
    invested = _block(df[df["investable"]])
    rejected = _block(df[~df["investable"]])

    # 单基金口径：摊平所有 TopN 单基金收益（接近真实"推荐出去的基金"体验）
    fund_rets = []
    if "top_fund_abs_pct" in df.columns:
        for v in df["top_fund_abs_pct"]:
            fund_rets.extend([float(x) for x in str(v).split(",") if x])
    fund = np.array(fund_rets, dtype=float)
    if len(fund) > 0:
        ps = profit_stats(fund, threshold=profit_threshold)  # 赚钱口径单一来源
        per_fund = {
            "n_funds": int(len(fund)),
            "abs_pct": round(float(fund.mean()), 3),
            "win_rate_pct": round(ps["win_rate"] * 100, 1),
            "profit_rate_pct": round(ps["profit_rate"] * 100, 1),
            "payoff_ratio": round(ps["payoff_ratio"], 2) if ps["payoff_ratio"] else None,
        }
    else:
        per_fund = {"n_funds": 0, "abs_pct": None, "win_rate_pct": None,
                    "profit_rate_pct": None, "payoff_ratio": None}

    # EMA60 退出版单基金口径（趋势退出后 vs 固定持有）
    stop_rets = []
    if "stop_fund_abs_pct" in df.columns:
        for v in df["stop_fund_abs_pct"]:
            stop_rets.extend([float(x) for x in str(v).split(",") if x])
    stop_fund = np.array(stop_rets, dtype=float)
    if len(stop_fund) > 0:
        ps = profit_stats(stop_fund, threshold=profit_threshold)  # 赚钱口径单一来源
        per_fund_stop = {
            "n_funds": int(len(stop_fund)),
            "abs_pct": round(float(stop_fund.mean()), 3),
            "win_rate_pct": round(ps["win_rate"] * 100, 1),
            "profit_rate_pct": round(ps["profit_rate"] * 100, 1),
            "payoff_ratio": round(ps["payoff_ratio"], 2) if ps["payoff_ratio"] else None,
        }
    else:
        per_fund_stop = None

    # 最大回撤：TopN 均值等权、每 20 交易日换仓（复利累乘）的累计曲线
    rets = df["top_abs_pct"] / 100.0
    if len(rets) > 0:
        cum = (1.0 + rets).cumprod()
        max_drawdown = round(float((cum / cum.cummax() - 1.0).min() * 100), 2)
    else:
        max_drawdown = 0.0

    summary = {
        "points_total": int(len(df)),
        "all": all_,
        "invested": invested,
        "rejected": rejected,
        "per_fund": per_fund,
        "per_fund_stop": per_fund_stop,
        "max_drawdown_pct": max_drawdown,
        "baseline": {
            "points": int(len(df)),
            "abs_pct": round(float(df["baseline_abs_pct"].mean()), 3),
            "win_rate_pct": round(float((df["baseline_abs_pct"] > 0).mean() * 100), 1),
        },
        "gate_cfg": {
            "gate": gate, "mom_threshold": mom_threshold,
            "pct_threshold": pct_threshold, "pct_window": pct_window,
            "pct_lookback": pct_lookback, "topn": topn,
            "profit_threshold": profit_threshold,
        },
        "elapsed_sec": round(elapsed, 1),
    }

    print("\n========== walk-forward 回测汇总 ==========")
    print(f"全期  : 点数={all_['points']}  Top{topn}绝对收益均值={all_['top_abs_pct']}%  "
          f"名义胜率={all_['win_rate_pct']}%  赚钱胜率(>{profit_threshold}%)={all_['profit_rate_pct']}%  "
          f"盈亏比={all_['payoff_ratio']}  alpha={all_['alpha_pct']}%  IC={all_['ic']}")
    print(f"出手日: 点数={invested['points']}  Top{topn}绝对收益均值={invested['top_abs_pct']}%  "
          f"名义胜率={invested['win_rate_pct']}%  赚钱胜率(>{profit_threshold}%)={invested['profit_rate_pct']}%  "
          f"盈亏比={invested['payoff_ratio']}")
    print(f"拒绝日: 点数={rejected['points']}  Top{topn}事后绝对收益均值={rejected['top_abs_pct']}%  "
          f"赚钱胜率(>{profit_threshold}%)={rejected['profit_rate_pct']}%  "
          f"(若为负 → 门避开了亏钱)")
    print(f"单基金: 只数={per_fund['n_funds']}  绝对收益均值={per_fund['abs_pct']}%  "
          f"名义胜率={per_fund['win_rate_pct']}%  赚钱胜率(>{profit_threshold}%)={per_fund['profit_rate_pct']}%  "
          f"盈亏比={per_fund['payoff_ratio']}")
    if per_fund_stop:
        print(f"止损版: 只数={per_fund_stop['n_funds']}  绝对收益均值={per_fund_stop['abs_pct']}%  "
              f"名义胜率={per_fund_stop['win_rate_pct']}%  赚钱胜率(>{profit_threshold}%)={per_fund_stop['profit_rate_pct']}%  "
              f"盈亏比={per_fund_stop['payoff_ratio']}  (EMA60趋势退出)")
    print(f"回撤  : 累计最大回撤={max_drawdown}%")
    print(f"基线  : 随机{topn}只 绝对收益均值={summary['baseline']['abs_pct']}%  "
          f"胜率={summary['baseline']['win_rate_pct']}%")
    print(f"耗时: {elapsed:.0f}s")
    print("==========================================")

    summary_path = Path(f"data/{out_stem}_summary.json")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("汇总已保存: %s", summary_path)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="walk-forward 回测验证（无前视偏差）")
    parser.add_argument("--start", default=_DEFAULT_START)
    parser.add_argument("--end", default=_DEFAULT_END)
    parser.add_argument("--gate", choices=["none", "rules"], default="rules",
                        help="规则门开关（none=现状基准，rules=绝对门+防追高护栏）")
    parser.add_argument("--mom-threshold", type=float, default=-3.0)
    parser.add_argument("--pct-threshold", type=float, default=90.0)
    parser.add_argument("--pct-window", type=int, default=250)
    parser.add_argument("--pct-lookback", type=int, default=750)
    parser.add_argument("--topn", type=int, default=5)
    parser.add_argument("--pool", type=int, default=600, help="打分池基金数（按回测日动态采样）")
    parser.add_argument("--train-pool", type=int, default=1200, help="训练采样基金数")
    parser.add_argument("--workers", type=int, default=4, help="并行决策点进程数（1=串行）")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 个决策点（快速验证）")
    parser.add_argument("--profit-threshold", type=float, default=1.0,
                        help="赚钱阈值（百分数，默认1.0=覆盖申赎成本）：绝对收益超过该值才算赚钱")
    parser.add_argument("--out", default="backtest_walkforward", help="输出文件前缀（data/ 下）")
    parser.add_argument("--mode", choices=["market", "sector"], default="market",
                        help="market=全市场TopN（现状基线）；sector=赛道内Top1 vs 随机（隔离赛道质量，测量化排序区分度）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run_walkforward(args.start, args.end, args.gate, args.mom_threshold,
                    args.pct_threshold, args.pct_window, args.pct_lookback,
                    args.topn, args.pool, args.limit, args.out,
                    workers=args.workers, train_pool=args.train_pool,
                    mode=args.mode, profit_threshold=args.profit_threshold)


if __name__ == "__main__":
    sys.exit(main())

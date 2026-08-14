"""阶段二验收：严格 walk-forward 退出模拟（无前视）——model_seq vs EMA60 vs 固定持有。

复用 backtest_walkforward 的训练/打分（每 20 日重训模型，仅用 <= 决策日数据，严格无前视）：
  1. 每 20 日决策点：训练模型 → 打分 → TopN 选股（combo）
  2. 对每只入选基金：模拟长持仓（max_days 可变 60/120/250）+ 三种退出策略：
     - fixed    ：持有到窗口末（基线）
     - ema60    ：NAV 连续 2 日 < EMA60 → 卖出（纯价格，无前视）
     - model_seq：窗口内每 20 日用当步模型打分（特征仅用截至打分日数据），
                  连续 2 个打分点转负 → 卖出（模型信号序列退出，无前视）
  3. 对比窗口收益 / 胜率 / 路径最大回撤（卖出后持现金）

验收标准（方案 §6.4）：model_seq 收益 ≥ ema60 且回撤 ≤ ema60。
运行：uv run python backtest_exit_walkforward.py [--start 2023-01-01] [--end 2023-06-30] [--topn 5] [--max-days 120]
"""
import argparse
import logging
import time
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

from app import repo
from app.features.calculator import (compute_fund_features, market_state_features,
                                      ema60_trigger_index)
from backtest.backtest_walkforward import _train_window, _score_at, FEATURE_COLS, MARKET_COLS

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("bt_exit_wf")

_STEP_DAYS = 20
_TRAIN_WINDOW_DAYS = 720   # 训练窗口：决策日前 2 年
_CONFIRM_PTS = 2           # 连续 2 个打分点转负（=40 日确认）
_FEAT_LOOKBACK = 250       # model_seq 打分输入窗口（截断控时）
_TRAIN_POOL = 600          # 训练采样基金数（冒烟用小值）
_SCORE_POOL = 600          # 打分池


def _load_index():
    rows = repo.get_index_series("sh000300", ("date", "close", "volume"))
    df = pd.DataFrame(rows, columns=["date", "close", "volume"])
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    return df["close"], df["volume"]


def _fund_codes(min_navs: int = 400) -> list[str]:
    from app.database import db_conn
    with db_conn() as conn:
        codes = [r[0] for r in conn.execute(
            "SELECT code FROM (SELECT code, COUNT(*) c FROM fund_nav GROUP BY code) WHERE c >= ?", (min_navs,)).fetchall()]
    return codes


def _score_fund_at(bst: lgb.Booster, code: str, d: pd.Timestamp,
                   idx_close: pd.Series, idx_vol: pd.Series) -> float | None:
    """无前视单基金打分（无动量 guard，与 _score_at 的选股口径解耦）：
    特征只用截至 d 的净值/指数窗口。"""
    rows = repo.nav.series(code)
    dates = [pd.Timestamp(r[0]) for r in rows]
    navs = [r[1] for r in rows]
    if d not in dates:
        return None
    pos = dates.index(d)
    if pos < 60:
        return None
    idx_pos = idx_close.index.get_indexer([d])[0]
    if idx_pos < 60:
        return None
    feat = compute_fund_features(
        np.array(navs[max(0, pos - _FEAT_LOOKBACK + 1):pos + 1], dtype=float),
        idx_close.iloc[idx_pos - 59: idx_pos + 1].to_numpy(dtype=float),
        idx_vol.iloc[idx_pos - 59: idx_pos + 1].to_numpy(dtype=float),
    )
    if feat is None:
        return None
    feat.update(market_state_features(
        idx_close.iloc[idx_pos - 59: idx_pos + 1].to_numpy(dtype=float),
        idx_vol.iloc[idx_pos - 59: idx_pos + 1].to_numpy(dtype=float),
    ))
    row = {c: float(feat[c]) for c in FEATURE_COLS if feat.get(c) is not None}
    if len(row) != len(FEATURE_COLS):
        return None
    val = float(bst.predict(pd.DataFrame([row], columns=FEATURE_COLS + MARKET_COLS))[0])
    return val if np.isfinite(val) else None


def _sim_exits(code: str, bt_date: pd.Timestamp, bst: lgb.Booster,
               idx_close: pd.Series, idx_vol: pd.Series,
               max_days: int) -> dict | None:
    """单基金单决策点：三种退出策略的窗口收益与最大回撤。"""
    rows = repo.nav.series(code)
    dates = [pd.Timestamp(r[0]) for r in rows]
    navs = [r[1] for r in rows]
    if bt_date not in dates:
        return None
    bp = dates.index(bt_date)
    if bp + max_days >= len(navs):
        return None
    seg = np.array(navs[bp:bp + max_days + 1], dtype=float)
    entry = seg[0]
    if entry <= 0 or np.any(seg <= 0):
        return None
    out: dict = {}

    def _finish(key: str, seg_ret: float, seg_path: np.ndarray):
        out[f"{key}_ret"] = seg_ret
        out[f"{key}_dd"] = float((1 - seg_path / np.maximum.accumulate(seg_path)).max())
        out[f"{key}_exit"] = 0

    # fixed
    _finish("fixed", seg[-1] / entry - 1.0, seg)

    # ema60：连续 2 日 < EMA60（与生产防线 R1 同判定，单一来源）
    ex_ema = ema60_trigger_index(seg.tolist())
    if ex_ema is not None:
        seg_e = seg.copy(); seg_e[ex_ema:] = seg[ex_ema]
        _finish("ema", seg[ex_ema] / entry - 1.0, seg_e)
        out["ema_exit"] = 1
    else:
        _finish("ema", seg[-1] / entry - 1.0, seg)

    # model_seq：窗口内每 20 日打分（从买入后 1 个步长起），连续 2 点转负 → 卖出
    ex_model = None
    neg_run = 0
    for off in range(_STEP_DAYS, max_days + 1, _STEP_DAYS):
        if bp + off >= len(dates):
            break
        s = _score_fund_at(bst, code, dates[bp + off], idx_close, idx_vol)
        if s is None:
            continue
        neg_run = neg_run + 1 if s < 0 else 0
        if neg_run >= _CONFIRM_PTS:
            ex_model = off
            break
    if ex_model is not None:
        seg_m = seg.copy(); seg_m[ex_model:] = seg[ex_model]
        _finish("model", seg[ex_model] / entry - 1.0, seg_m)
        out["model_exit"] = 1
    else:
        _finish("model", seg[-1] / entry - 1.0, seg)
    return out


def _worker(args: tuple) -> list[dict]:
    """单决策点 worker（进程池）：训练（无前视）→ 打分 → TopN → 退出模拟。"""
    bt_date_str, topn, max_days, train_pool, score_pool, w_days = args
    idx_close, idx_vol = _load_index()
    codes = _fund_codes()
    train_codes = codes[:train_pool] if len(codes) > train_pool else codes
    rng = np.random.default_rng(7)
    score_codes = list(rng.choice(codes, min(score_pool, len(codes)), replace=False))
    bt_date = pd.Timestamp(bt_date_str)
    bst = _train_window(bt_date, train_codes, window_days=w_days)
    if bst is None:
        return []
    df = _score_at(bst, bt_date, idx_close, idx_vol, score_codes, guard=-1.0)
    if df is None or df.empty or len(df) < topn:
        return []
    out = []
    for code in df.nlargest(topn, "combo")["code"].tolist():
        r = _sim_exits(code, bt_date, bst, idx_close, idx_vol, max_days)
        if r is None:
            continue
        r["date"] = bt_date_str
        r["code"] = code
        out.append(r)
    return out


def run(start: str, end: str, topn: int, max_days: int, train_pool: int,
        score_pool: int, limit: int | None, workers: int = 1,
        train_window_days: int = _TRAIN_WINDOW_DAYS) -> None:
    idx_close, _ = _load_index()
    all_dates = sorted(idx_close.index)
    bt_dates = [d for d in all_dates if pd.Timestamp(start) <= d <= pd.Timestamp(end)][::_STEP_DAYS]
    if limit:
        bt_dates = bt_dates[:limit]
    log.info("区间 %s~%s %d 决策点 | topn=%d max_days=%d 训练窗口%d天 workers=%d",
             start, end, len(bt_dates), topn, max_days, train_window_days, workers)

    args_list = [(d.strftime("%Y-%m-%d"), topn, max_days, train_pool, score_pool,
                  train_window_days) for d in bt_dates]
    rows: list[dict] = []
    t0 = time.monotonic()
    if workers > 1:
        from concurrent.futures import ProcessPoolExecutor, as_completed
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_worker, a) for a in args_list]
            done = 0
            for fut in as_completed(futs):
                done += 1
                rows.extend(fut.result())
                if done % 5 == 0:
                    log.info("进度 %d/%d 累计%.0fs", done, len(args_list), time.monotonic() - t0)
    else:
        for k, a in enumerate(args_list):
            rows.extend(_worker(a))
            if (k + 1) % 5 == 0:
                log.info("进度 %d/%d 累计%.0fs", k + 1, len(args_list), time.monotonic() - t0)

    if not rows:
        log.error("无有效样本")
        return
    df = pd.DataFrame(rows)
    out_path = Path(f"data/exit_walkforward_{max_days}d.csv")
    df.to_csv(out_path, index=False)
    print(f"\n=========== 严格 walk-forward 退出模拟（无前视）max_days={max_days} ===========")
    print(f"样本 {len(df)}（{df['date'].nunique()} 决策点 × 基金）| 区间 {start}~{end}")
    print(f"{'策略':<12}{'平均收益':>10}{'胜率':>8}{'中位收益':>10}{'平均最大回撤':>14}{'触发率':>10}")
    for key, label in [("fixed", "固定持有"), ("ema", "EMA60退出"), ("model", "模型序列退出")]:
        rets = df[f"{key}_ret"]; dds = df[f"{key}_dd"]
        trig = df[f"{key}_exit"].mean() * 100
        print(f"{label:<12}{rets.mean() * 100:>9.2f}%{(rets > 0).mean() * 100:>7.1f}%"
              f"{rets.median() * 100:>9.2f}%{dds.mean() * 100:>13.2f}%{trig:>9.1f}%")
    print(f"明细: {out_path} | 耗时 {time.monotonic() - t0:.0f}s")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--end", default="2024-06-30")
    ap.add_argument("--topn", type=int, default=5)
    ap.add_argument("--max-days", type=int, default=120)
    ap.add_argument("--train-pool", type=int, default=_TRAIN_POOL)
    ap.add_argument("--score-pool", type=int, default=_SCORE_POOL)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--train-window-days", type=int, default=_TRAIN_WINDOW_DAYS)
    args = ap.parse_args()
    run(args.start, args.end, args.topn, args.max_days, args.train_pool,
        args.score_pool, args.limit, args.workers, args.train_window_days)

"""样本外切分纪律工具（reco-hardening T05）。

背景：本重构所有阈值都在同一段 2020-2026 历史数据上标定（多重比较过拟合风险）。
本工具强制"挖数段定参 → 验证段确认"纪律：切分逻辑 + 对比报告 + 疑似过拟合标注。

用法（示例）：
    python -m backtest.walk_forward_holdout --start 2024-01-01 --end 2026-08-30
    （内部按时间 80/20 切成挖数段/验证段，两段各自跑回测并对比）
"""

import argparse
import json
from datetime import datetime


def holdout_split(dates: list[str], train_frac: float = 0.8) -> tuple[list[str], list[str]]:
    """按时间升序切分：前 train_frac 为挖数段（定参），后 20% 为验证段（只读不改）。

    返回 (train_dates, valid_dates)，均保持输入顺序（须已按时间升序）。
    """
    if not dates:
        return [], []
    ordered = sorted(dates)
    split = max(1, int(len(ordered) * train_frac))
    return ordered[:split], ordered[split:]


def compare_segments(train: dict, valid: dict, warn_pp: float = 10.0) -> dict:
    """对比两段回测指标，差异超阈值标注疑似过拟合。

    train/valid 为 run_backtest 的 summary（含 profit_rate_pct / mean_top_abs_pct 等）。
    """
    def _diff(key: str) -> float | None:
        a, b = train.get(key), valid.get(key)
        if a is None or b is None:
            return None
        return round(float(b) - float(a), 2)

    win_diff = _diff("profit_rate_pct")
    ret_diff = _diff("mean_top_abs_pct")
    verdict = "OK"
    notes = []
    if win_diff is not None and abs(win_diff) > warn_pp:
        verdict = "SUSPECT_OVERFIT"
        notes.append(f"胜率差 {win_diff:+.1f}pp 超阈值 {warn_pp}pp")
    if ret_diff is not None and abs(ret_diff) > warn_pp / 2:
        verdict = "SUSPECT_OVERFIT"
        notes.append(f"收益差 {ret_diff:+.2f}pp 超阈值 {warn_pp/2}pp")
    return {
        "train": {k: train.get(k) for k in ("periods", "profit_rate_pct", "mean_top_abs_pct", "mean_ic")},
        "valid": {k: valid.get(k) for k in ("periods", "profit_rate_pct", "mean_top_abs_pct", "mean_ic")},
        "diff_pp": {"profit_rate_pct": win_diff, "mean_top_abs_pct": ret_diff},
        "verdict": verdict,
        "notes": notes,
        "warn_pp": warn_pp,
    }


def run_holdout(start: str, end: str, lookback_days: int = 365,
                stop_mode: str = "none", stop_param: float = 0.0,
                fee_buy_pct: float = 0.0, slippage_pct: float = 0.0) -> dict:
    """跑完整挖数段/验证段对比：两段同参数回测 → compare_segments。

    区间从决策日序列（index_daily 交易日）按时间 80/20 切分。
    """
    from app import repo
    idx_rows = repo.get_index_series("sh000300", ("date",))
    if not idx_rows:
        raise RuntimeError("指数数据缺失，无法切分")
    dates = sorted(r[0] for r in idx_rows)
    start_d, end_d = datetime.strptime(start, "%Y-%m-%d"), datetime.strptime(end, "%Y-%m-%d")
    in_range = [d for d in dates if start_d <= datetime.strptime(d, "%Y-%m-%d") <= end_d]
    train_dates, valid_dates = holdout_split(in_range)

    from backtest.backtest import run_backtest
    train = run_backtest(start_date=train_dates[0], end_date=train_dates[-1],
                         lookback_days=lookback_days, stop_mode=stop_mode,
                         stop_param=stop_param, fee_buy_pct=fee_buy_pct,
                         slippage_pct=slippage_pct)
    valid = run_backtest(start_date=valid_dates[0], end_date=valid_dates[-1],
                         lookback_days=lookback_days, stop_mode=stop_mode,
                         stop_param=stop_param, fee_buy_pct=fee_buy_pct,
                         slippage_pct=slippage_pct)
    result = compare_segments(train, valid)
    result["cfg"] = {"start": start, "end": end, "stop_mode": stop_mode,
                     "stop_param": stop_param, "fee_buy_pct": fee_buy_pct,
                     "slippage_pct": slippage_pct}
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--lookback-days", type=int, default=365)
    ap.add_argument("--stop-mode", default="none")
    ap.add_argument("--stop-param", type=float, default=0.0)
    ap.add_argument("--fee-buy-pct", type=float, default=0.0)
    ap.add_argument("--slippage-pct", type=float, default=0.0)
    ap.add_argument("--out", default="data/holdout_report.json")
    args = ap.parse_args()
    result = run_holdout(args.start, args.end, args.lookback_days,
                         args.stop_mode, args.stop_param,
                         args.fee_buy_pct, args.slippage_pct)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print(json.dumps(result, ensure_ascii=False, indent=1))

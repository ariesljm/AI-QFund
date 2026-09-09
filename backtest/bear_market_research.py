"""熊市盈利机制研究（reco-hardening T06，用户决策 D1：熊市也要找到赚钱的基金）。

用回测面板（data/sector_strategy_panel.csv，2020-2026 基金级特征+未来收益）回答：
熊市（regime=BEAR）样本中，哪些特征能选出未来收益为正的基金？

- regime 判定：沪深300 收盘 vs EMA60（与生产 domain regime 同口径）
- 输出：熊市样本画像 + 各特征分位的正收益胜率/平均收益（信号区分度）
- 注意：面板 fwd_20d 为 20 日口径（研究方向性；40 日口径待面板重建后复验）

用法：python -m backtest.bear_market_research
"""

import json

import pandas as pd

from backtest._regime import regime_series as _regime_series_impl

_PANEL = "data/sector_strategy_panel.csv"
_OUT = "data/bear_market_research.json"


def _regime_series(index_df: pd.DataFrame) -> pd.Series:
    """收盘 vs EMA60+EMA250 → regime（⑥ 统一 seam，与生产多周期判定同口径）。"""
    return _regime_series_impl(index_df["close"])


def load_panel_with_regime() -> pd.DataFrame:
    """读取面板 + 标注 regime，返回全部样本行（BULL/BEAR）。"""
    import app.repo as repo

    df = pd.read_csv(_PANEL, parse_dates=["date"])
    idx_rows = repo.get_index_series("sh000300", ("date", "close"))
    idx = pd.DataFrame(idx_rows, columns=["date", "close"])
    idx["date"] = pd.to_datetime(idx["date"])
    idx = idx.set_index("date").sort_index()
    regime = _regime_series(idx)
    df["regime"] = df["date"].map(regime)
    return df


def load_bear_panel() -> pd.DataFrame:
    """读取面板 + 标注 regime，返回熊市样本行。"""
    df = load_panel_with_regime()
    bear = df[df["regime"] == "BEAR"].copy()
    bear = bear.dropna(subset=["fwd_20d", "mom_5d", "mom_20d", "vol_20d", "dd_120d"])
    return bear


def signal_breakdown(bear: pd.DataFrame) -> dict:
    """熊市样本中，各特征高分位 vs 低分位的正收益胜率/平均收益（区分度）。"""
    features = ["mom_5d", "mom_20d", "mom_60d", "vol_20d", "dd_120d", "excess_20d", "mom_accel"]
    out = {}
    for f in features:
        if f not in bear.columns or bear[f].dropna().empty:
            continue
        lo = bear[bear[f] <= bear[f].quantile(0.3)]
        hi = bear[bear[f] >= bear[f].quantile(0.7)]
        out[f] = {
            "lo_win_pct": round((lo["fwd_20d"] > 0).mean() * 100, 1),
            "hi_win_pct": round((hi["fwd_20d"] > 0).mean() * 100, 1),
            "lo_avg_pct": round(lo["fwd_20d"].mean() * 100, 2),
            "hi_avg_pct": round(hi["fwd_20d"].mean() * 100, 2),
            "n": int(len(bear)),
        }
    return out


def sector_profile(bear: pd.DataFrame) -> dict:
    """熊市正收益基金的赛道画像（Top 赛道 + 行业集中度）。"""
    win = bear[bear["fwd_20d"] > 0]
    if win.empty:
        return {"note": "熊市样本无正收益行"}
    top = win["sector"].value_counts().head(8)
    return {
        "top_sectors": {str(k): int(v) for k, v in top.items()},
        "bear_win_rate": round(len(win) / len(bear) * 100, 1),
        "n_bear_rows": int(len(bear)),
    }


def bull_diagnosis(bull: pd.DataFrame) -> dict:
    """牛市适配性诊断：牛市样本中信号区分度 + 追高/低波动/高位行为。

    用户关切：策略（动量门槛/降权/排序）在牛市是否适配——牛市追高是否危险、
    低波动是否仍无意义、动量延续是否更强。
    """
    features = ["mom_5d", "mom_20d", "mom_60d", "vol_20d", "dd_120d", "excess_20d", "mom_accel"]
    out = {"n_bull_rows": int(len(bull)),
           "bull_win_rate_pct": round((bull["fwd_20d"] > 0).mean() * 100, 1)}
    breakdown = {}
    for f in features:
        if f not in bull.columns or bull[f].dropna().empty:
            continue
        lo = bull[bull[f] <= bull[f].quantile(0.3)]
        hi = bull[bull[f] >= bull[f].quantile(0.7)]
        breakdown[f] = {
            "lo_win_pct": round((lo["fwd_20d"] > 0).mean() * 100, 1),
            "hi_win_pct": round((hi["fwd_20d"] > 0).mean() * 100, 1),
            "lo_avg_pct": round(lo["fwd_20d"].mean() * 100, 2),
            "hi_avg_pct": round(hi["fwd_20d"].mean() * 100, 2),
        }
    out["signal_breakdown"] = breakdown
    # 追高组合（5d/20d 双强）在牛市的表现——验证牛市热度降权的必要性
    if {"mom_5d", "mom_20d"} <= set(bull.columns):
        chase = bull[(bull["mom_5d"] >= bull["mom_5d"].quantile(0.5))
                     & (bull["mom_20d"] >= bull["mom_20d"].quantile(0.5))]
        out["chase_combo"] = {
            "n": int(len(chase)),
            "win_pct": round((chase["fwd_20d"] > 0).mean() * 100, 1),
            "avg_pct": round(chase["fwd_20d"].mean() * 100, 2),
        }
    return out


def run() -> dict:
    df = load_panel_with_regime()
    if df.empty:
        return {"note": "无面板样本"}
    bear = df[df["regime"] == "BEAR"].dropna(subset=["fwd_20d"])
    bull = df[df["regime"] == "BULL"].dropna(subset=["fwd_20d"])
    report = {
        "n_bear_rows": int(len(bear)),
        "bear_win_rate_pct": round((bear["fwd_20d"] > 0).mean() * 100, 1),
        "signal_breakdown": signal_breakdown(bear) if not bear.empty else {},
        "sector_profile": sector_profile(bear) if not bear.empty else {},
        "bull_diagnosis": bull_diagnosis(bull) if not bull.empty else {},
        "note": "fwd_20d 为 20 日口径（方向性研究）；40 日口径待面板重建复验",
    }
    with open(_OUT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    return report


if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")
    print(json.dumps(run(), ensure_ascii=False, indent=1))

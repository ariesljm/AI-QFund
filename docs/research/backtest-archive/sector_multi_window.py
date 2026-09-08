"""多窗口信号验证（持有周期重定向前置证据）：
用户口径：推荐基金未来 N 日绝对收益 >0（N 属于 1-3 个月 ≈ 20/40/60 交易日）。
验证此前基于 fwd_20d 的关键结论在 40/60 日窗口下是否依然成立：
  1. 20/60 日动量是否仍是反转信号（买在高位的风险）
  2. 5 日动量/当日热度是否仍有延续性
  3. 低波动是否仍是跨窗口稳健的正信号
  4. 赛道内排序（组合分代理）在更长窗口是否有效
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

_OUT = Path("data/sector_multi_window_research.json")


def add_fwd_windows(df: pd.DataFrame) -> pd.DataFrame:
    """给基金级面板补充 fwd_40d / fwd_60d（绝对收益），并聚合到赛道级。"""
    panel = df.copy()
    # 需要重新从净值取未来 40/60 日收益：标记每只基金决策日的位置
    # 简化：解散裁面板改为直接从裸净值重建赛道聚合（研究脚本已有基建）
    return panel


def build_sector_panel_multi(pivot: pd.DataFrame) -> pd.DataFrame:
    """赛道级多窗口面板：（date, sector, n, mom_5d, mom_20d, mom_60d, ret_1d,
    fwd_20d, fwd_40d, fwd_60d, vol_20d, dd_120d）。"""
    nav = pivot.astype(float)
    mom_60 = nav / nav.shift(60) - 1.0
    mom_20 = nav / nav.shift(20) - 1.0
    mom_5 = nav / nav.shift(5) - 1.0
    ret_1 = nav / nav.shift(1) - 1.0
    fwd_20 = nav.shift(-20) / nav - 1.0
    fwd_40 = nav.shift(-40) / nav - 1.0
    fwd_60 = nav.shift(-60) / nav - 1.0
    vol_20 = nav.pct_change().rolling(20).std() * np.sqrt(252)
    peak_120 = nav.rolling(120, min_periods=30).max()
    dd_120 = nav / peak_120 - 1.0

    labels = _small_labels()
    trading_dates = [d for d in sorted(nav.index) if pd.Timestamp(d) >= pd.Timestamp("2020-10-01")]
    lab = labels.sort_values("report_date")
    by_code = {c: (lab_g["report_date"].to_numpy(), lab_g["industry_1"].to_numpy())
               for c, lab_g in lab.groupby("code")}

    rows = []
    for t in trading_dates[::20]:
        if t not in nav.index:
            continue
        ok = (nav.loc[t].notna() & mom_20.loc[t].notna() & mom_5.loc[t].notna()
              & mom_60.loc[t].notna() & ret_1.loc[t].notna()
              & fwd_60.loc[t].notna())
        funds = nav.columns[ok]
        if len(funds) < 20:
            continue
        sector = []
        for c in funds:
            rd, inds = by_code.get(c, (None, None))
            if rd is None or len(rd) == 0:
                sector.append(None)
                continue
            pos = np.searchsorted(rd, t, side="right") - 1
            sector.append(inds[pos] if pos >= 0 else None)
        df = pd.DataFrame({
            "code": funds, "sector": sector,
            "mom_5d": mom_5.loc[t, funds].to_numpy(),
            "mom_20d": mom_20.loc[t, funds].to_numpy(),
            "mom_60d": mom_60.loc[t, funds].to_numpy(),
            "ret_1d": ret_1.loc[t, funds].to_numpy(),
            "vol_20d": vol_20.loc[t, funds].fillna(0).to_numpy(),
            "dd_120d": dd_120.loc[t, funds].fillna(0).to_numpy(),
            "fwd_20d": fwd_20.loc[t, funds].to_numpy(),
            "fwd_40d": fwd_40.loc[t, funds].to_numpy(),
            "fwd_60d": fwd_60.loc[t, funds].to_numpy(),
            "date": t,
        }).dropna(subset=["sector"])
        rows.append(df)
    if not rows:
        return pd.DataFrame()
    full = pd.concat(rows, ignore_index=True)
    agg = (full.groupby(["date", "sector"])
           .agg(n=("code", "count"),
                mom_5d=("mom_5d", "mean"), mom_20d=("mom_20d", "mean"),
                mom_60d=("mom_60d", "mean"), ret_1d=("ret_1d", "mean"),
                vol_20d=("vol_20d", "mean"), dd_120d=("dd_120d", "mean"),
                fwd_20d=("fwd_20d", "mean"),
                fwd_40d=("fwd_40d", "mean"),
                fwd_60d=("fwd_60d", "mean"))
           .reset_index())
    return agg[agg["n"] >= 5]


def _small_labels() -> pd.DataFrame:
    from backtest.sector_signals import _load_holdings_labels
    return _load_holdings_labels()


def _hot_cold(df: pd.DataFrame, feat: str, target: str) -> dict:
    tmp = df.copy()
    tmp["pct"] = tmp.groupby("date")[feat].transform(lambda x: x.rank(pct=True))
    hot = tmp[tmp["pct"] >= 0.9]
    cold = tmp[tmp["pct"] <= 0.1]
    if len(hot) < 5 or len(cold) < 5:
        return {}
    return {"hot_pct": round(float(hot[target].mean() * 100), 2),
            "cold_pct": round(float(cold[target].mean() * 100), 2),
            "spread": round(float((hot[target].mean() - cold[target].mean()) * 100), 2),
            "hot_win": round(float((hot[target] > 0).mean() * 100), 1),
            "n": int(len(hot))}


def main() -> None:
    from backtest.sector_signals import _load_nav_pivot
    print("加载面板…")
    pivot = _load_nav_pivot()
    agg = build_sector_panel_multi(pivot)
    print(f"赛道级面板: {len(agg)} 行, {agg['date'].nunique()} 决策日")
    if agg.empty:
        return

    out = {"n_rows": int(len(agg)), "n_dates": int(agg["date"].nunique())}
    feats = [("mom_5d", "5日动量"), ("mom_20d", "20日动量"), ("mom_60d", "60日动量"),
             ("ret_1d", "当日热度"), ("vol_20d", "低波动(取反日衡量)",),
             ]
    for tgt, lab in [("fwd_20d", "未来20日"), ("fwd_40d", "未来40日"), ("fwd_60d", "未来60日")]:
        out[lab] = {"all_fwd": round(float(agg[tgt].mean() * 100), 2),
                    "all_win": round(float((agg[tgt] > 0).mean() * 100), 1)}
        for f, flab in feats:
            out[lab][flab] = _hot_cold(agg, f, tgt)
    # 赛道内排序损耗（多窗口）
    for tgt, lab in [("fwd_20d", "未来20日"), ("fwd_40d", "未来40日"), ("fwd_60d", "未来60日")]:
        sim = (agg.groupby("date")["mom_20d"].rank(pct=True)
               + agg.groupby("date")["ret_1d"].rank(pct=True)) / 2
        top = agg[sim >= 0.7]
        bot = agg[sim <= 0.3]
        out["landing_" + lab] = {
            "top70": round(float(top[tgt].mean() * 100), 2),
            "bot30": round(float(bot[tgt].mean() * 100), 2),
            "diff": round(float((top[tgt].mean() - bot[tgt].mean()) * 100), 2),
        }

    _OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

"""赛道策略通用研究（重构 T2/T3 证据）：找出牛熊通用的有效赛道信号。

回答三个问题：
  Q1 哪些信号（动量/热度/波动/回撤/超额）在整个 2019-2026 有稳定的截面预测力？
  Q2 这些信号在 BULL / BEAR 下是否都有效（跨 regime 鲁棒）？还是需要条件切换？
  Q3 「赛道信号 → 基金落地」：赛道内排名靠前的基金是否真的比靠后的赚（落地损耗）？
     （面板同时保留基金级记录，可检验 LLM 终选/排序环节的损耗）

面板构建完全复用生产同源数据（fund_holdings×stock_industry_map 季报标签 + fund_nav），
但与 backtest.sector_signals 的不同：保留 code 级记录，并追加 vol_20d / drawdown_120d /
excess_20d / 拥挤度（mom 加速）特征。面板落盘 CSV 缓存，分析可反复读。

运行：uv run python -m backtest.sector_strategy_research [--start 2019-01-01] [--end 2026-09-03] [--rebuild]
"""

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from app import repo
from app.engine.quality import spearman
from backtest.sector_signals import _load_holdings_labels, _load_nav_pivot, _load_trading_dates

_PANEL_CACHE = Path("data/sector_strategy_panel.csv")
_OUT_PATH = Path("data/sector_strategy_research.json")


def build_panel_full(start: str, end: str) -> pd.DataFrame:
    """基金级面板（保留 code）：动量/热度/波动/回撤/超额/拥挤度 → 未来20日收益。"""
    pivot = _load_nav_pivot().astype(float)
    labels = _load_holdings_labels()
    trading_dates = [d for d in _load_trading_dates(pivot) if start <= d <= end]

    nav = pivot
    mom_60 = nav / nav.shift(60) - 1.0
    mom_20 = nav / nav.shift(20) - 1.0
    mom_5 = nav / nav.shift(5) - 1.0
    ret_1 = nav / nav.shift(1) - 1.0
    fwd_20 = nav.shift(-20) / nav - 1.0
    vol_20 = nav.pct_change().rolling(20).std() * np.sqrt(252)
    peak_120 = nav.rolling(120, min_periods=30).max()
    dd_120 = nav / peak_120 - 1.0
    idx_close = pd.Series(
        {r[0]: r[1] for r in repo.get_index_series("sh000300", ("date", "close"))})
    idx_close.index = pd.to_datetime(idx_close.index)
    idx_ret_20 = idx_close / idx_close.shift(20) - 1.0

    bd = int(20 / 1)  # 决策日步长
    lab = labels.sort_values("report_date")
    by_code = {c: (lab_g["report_date"].to_numpy(), lab_g["industry_1"].to_numpy())
               for c, lab_g in lab.groupby("code")}

    rows = []
    for t in trading_dates[::bd]:
        if t not in nav.index:
            continue
        ok = (nav.loc[t].notna() & mom_20.loc[t].notna() & mom_5.loc[t].notna()
              & mom_60.loc[t].notna() & ret_1.loc[t].notna() & fwd_20.loc[t].notna())
        funds = nav.columns[ok]
        if len(funds) == 0:
            continue
        t_pd = pd.Timestamp(t)
        sector = []
        for c in funds:
            rd, inds = by_code.get(c, (None, None))
            if rd is None or len(rd) == 0:
                sector.append(None)
                continue
            pos = np.searchsorted(rd, t, side="right") - 1
            sector.append(inds[pos] if pos >= 0 else None)
        idx20 = float(idx_ret_20.get(t_pd, np.nan)) if t_pd in idx_ret_20.index else np.nan
        df = pd.DataFrame({
            "code": funds, "sector": sector,
            "mom_5d": mom_5.loc[t, funds].to_numpy(),
            "mom_20d": mom_20.loc[t, funds].to_numpy(),
            "mom_60d": mom_60.loc[t, funds].to_numpy(),
            "ret_1d": ret_1.loc[t, funds].to_numpy(),
            "vol_20d": vol_20.loc[t, funds].fillna(0).to_numpy(),
            "dd_120d": dd_120.loc[t, funds].fillna(0).to_numpy(),
            "excess_20d": mom_20.loc[t, funds].to_numpy() - idx20,
            # 拥挤度：5日动量 vs 20日动量的加速比（正值=加速赶顶）
            "mom_accel": mom_5.loc[t, funds].to_numpy() - mom_20.loc[t, funds].to_numpy() / 4,
            "fwd_20d": fwd_20.loc[t, funds].to_numpy(),
            "date": t,
        })
        rows.append(df.dropna(subset=["sector"]))
    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, ignore_index=True)


def _section_ic(df: pd.DataFrame, feat: str) -> dict:
    ics = []
    for _, g in df.groupby("date"):
        ic = spearman(g[feat].to_numpy(), g["fwd_20d"].to_numpy())
        if ic is not None:
            ics.append(ic)
    if not ics:
        return {}
    a = np.array(ics)
    return {"ic": round(float(a.mean()), 4),
            "ir": round(float(a.mean() / a.std()), 3) if a.std() > 1e-12 else None,
            "pos_pct": round(float((a > 0).mean()), 3),
            "n_dates": int(len(a))}


def _hot_cold(df: pd.DataFrame, feat: str) -> dict:
    tmp = df.copy()
    tmp["pct"] = tmp.groupby("date")[feat].transform(lambda x: x.rank(pct=True))
    hot = tmp[tmp["pct"] >= 0.9]
    cold = tmp[tmp["pct"] <= 0.1]
    if len(hot) < 5 or len(cold) < 5:
        return {}
    return {"hot_pct": round(float(hot["fwd_20d"].mean() * 100), 2),
            "cold_pct": round(float(cold["fwd_20d"].mean() * 100), 2),
            "spread": round(float((hot["fwd_20d"].mean() - cold["fwd_20d"].mean()) * 100), 2),
            "hot_win": round(float((hot["fwd_20d"] > 0.01).mean() * 100), 1),
            "n": int(len(hot))}


def _landing_loss(df: pd.DataFrame) -> dict:
    """落地损耗：同一赛道内，combo 模拟分前 30% 基金 vs 后 30% 的未来收益差。

    用 mom_20d+ret_1d 的等权秩作为"赛道内排序"代理（生产 combo 的简化）：
    若排序靠前的基金未来收益不高于靠后，说明基金落地点是损耗环节。
    """
    tmp = df.copy()
    tmp["sim_rank"] = (tmp.groupby("date")["mom_20d"].rank(pct=True)
                       + tmp.groupby("date")["ret_1d"].rank(pct=True)) / 2
    top = tmp[tmp["sim_rank"] >= 0.7]
    bot = tmp[tmp["sim_rank"] <= 0.3]
    if len(top) < 20 or len(bot) < 20:
        return {}
    return {"top70_pct": round(float(top["fwd_20d"].mean() * 100), 2),
            "bot30_pct": round(float(bot["fwd_20d"].mean() * 100), 2),
            "loss_pct": round(float((bot["fwd_20d"].mean() - top["fwd_20d"].mean()) * 100), 2),
            "n": int(len(top))}


def run(start: str, end: str, rebuild: bool) -> dict:
    if rebuild or not _PANEL_CACHE.exists():
        print("构建基金级面板…")
        panel = build_panel_full(start, end)
        panel.to_csv(_PANEL_CACHE, index=False)
    else:
        panel = pd.read_csv(_PANEL_CACHE)
    panel["dt"] = pd.to_datetime(panel["date"])
    idx = repo.get_index_series("sh000300", ("date", "close"))
    closes = pd.Series({r[0]: r[1] for r in idx})
    closes.index = pd.to_datetime(closes.index)
    ma60 = closes.rolling(60).mean()
    regime_map = (closes > ma60).map({True: "BULL", False: "BEAR"}).to_dict()
    panel["regime"] = panel["dt"].map(regime_map).fillna("BEAR")
    panel["year"] = panel["dt"].dt.year

    out: dict = {"n_fund_rows": int(len(panel)),
                 "n_dates": int(panel["date"].nunique()),
                 "range": f"{panel['date'].min()} ~ {panel['date'].max()}"}
    feats = [("mom_5d", "5日动量"), ("mom_20d", "20日动量"), ("mom_60d", "60日动量"),
             ("ret_1d", "当日热度"), ("vol_20d", "20波动率"), ("dd_120d", "120日回撤"),
             ("excess_20d", "相对指数超额"), ("mom_accel", "动量加速(拥挤)")]

    # Q1 全区间
    out["all"] = {}
    for f, lab in feats:
        out["all"][lab] = {"ic": _section_ic(panel, f), "hotcold": _hot_cold(panel, f)}

    # Q2 按 regime
    out["by_regime"] = {}
    for reg in ("BULL", "BEAR"):
        g = panel[panel["regime"] == reg]
        out["by_regime"][reg] = {"n_dates": int(g["date"].nunique()),
                                 "all_fwd": round(float(g["fwd_20d"].mean() * 100), 2),
                                 "all_win": round(float((g["fwd_20d"] > 0.01).mean() * 100), 1)}
        for f, lab in feats[:4]:  # 重点看动量/热度
            out["by_regime"][reg][lab] = {"ic": _section_ic(g, f),
                                          "hotcold": _hot_cold(g, f)}

    # 分年度稳定性（热度 + 20日动量）
    out["yearly"] = {}
    for y, g in panel.groupby("year"):
        out["yearly"][str(y)] = {
            "n": int(len(g)),
            "hot_ret1": _hot_cold(g, "ret_1d"),
            "hot_mom5": _hot_cold(g, "mom_5d"),
            "mom20_ic": _section_ic(g, "mom_20d"),
            "all_fwd": round(float(g["fwd_20d"].mean() * 100), 2),
        }

    # Q3 落地损耗（全区间 + regime）
    out["landing_loss"] = {"all": _landing_loss(panel)}
    for reg in ("BULL", "BEAR"):
        out["landing_loss"][reg] = _landing_loss(panel[panel["regime"] == reg])

    _OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2019-01-01")
    parser.add_argument("--end", default="2026-09-03")
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args()
    result = run(args.start, args.end, args.rebuild)
    with open("data/sector_strategy_report.txt", "w", encoding="utf-8") as f:
        f.write(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print("报告已写入 data/sector_strategy_report.txt 与 .json")


if __name__ == "__main__":
    main()

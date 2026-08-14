"""赛道信号离线回测（D6b）：反推历史赛道收益，验证「趋势/动量」与「单日热度」对未来 20 日赛道收益的预测力。

目的：为改造方案（量化定池：5/20 日板块动量 + 过热规避）提供证据——
  1. 多日动量（5/20/60 日）是否有预测力（有 → 趋势口径成立；没有 → 需另找信号）
  2. 「当日热度」赛道（单日涨幅最大，资金流入排行的近似代理）是否均值回归（是 → 追当日热度不赚钱）
  3. 动量护栏 -15% 崩盘过滤是否合理（崩盘赛道未来是否继续弱）
  4. 反推过热阈值（动量加速/单日骤增的截断点）

方法（赛道标签与生产 calc_rbsa 完全同源）：
  fund_holdings × stock_industry_map → 每基金每报告期第一行业（季报级低频标签）
  fund_nav → 每基金在决策日的 5/20/60 日动量、单日涨幅、未来 20 日收益
  按 (赛道, 决策日) 等权聚合（≥5 只基金）→ 面板检验截面 IC 与分位收益

决策日每 20 交易日一次（与 FORWARD_DAYS 对齐，避免重叠窗口伪独立）。

运行：uv run python -m backtest.sector_signals [--start 2019-01-01] [--end 2026-07-31]
"""

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from app import repo
from app.database import db_conn
from app.engine.quality import spearman  # 秩相关单一来源（与生产质量度量同口径）
from app.utils.log import get_logger

logger = get_logger("backtest.sector_signals")

_MIN_FUNDS_PER_SECTOR = 5   # 赛道样本下限：不足该基金数不计入面板
_DECISION_STEP = 20         # 决策频率：每 20 交易日一次（与 FORWARD_DAYS 对齐）
_OUT_PATH = Path("data/sector_signal_backtest.json")


def _load_holdings_labels() -> pd.DataFrame:
    """从季报持仓重建每基金每报告期的第一行业标签（与 calc_rbsa 同源）。

    返回 DataFrame[code, report_date, industry_1]，行业名来自 stock_industry_map。
    """
    industry_map = repo.get_industry_map()
    with db_conn() as conn:
        holdings = pd.read_sql_query(
            "SELECT code, report_date, stock_code, weight FROM fund_holdings",
            conn)
    # 无行业映射的持仓忽略（与 calc_rbsa 的 '其他' 处理一致，但过滤掉避免污染赛道面板）
    holdings["industry"] = holdings["stock_code"].map(industry_map)
    holdings = holdings.dropna(subset=["industry"])
    # 按报告期聚合行业权重 → 取第一行业（与 calc_rbsa 相同的前 3 大逻辑）
    grp = (holdings.groupby(["code", "report_date", "industry"])["weight"]
           .sum().reset_index())
    grp = grp.sort_values("weight", ascending=False)
    top1 = grp.groupby(["code", "report_date"], as_index=False).first()
    return top1.rename(columns={"industry": "industry_1"})[["code", "report_date", "industry_1"]]


def _load_nav_pivot() -> pd.DataFrame:
    """基金净值透视：行=交易日，列=基金代码，值为累计净值。"""
    with db_conn() as conn:
        nav = pd.read_sql_query(
            "SELECT code, date, cum_nav FROM fund_nav WHERE cum_nav IS NOT NULL", conn)
    pivot = nav.pivot_table(index="date", columns="code", values="cum_nav",
                            aggfunc="last")
    pivot = pivot.sort_index()
    return pivot


def _load_trading_dates(pivot: pd.DataFrame) -> list[str]:
    """交易日历：优先沪深300 指数日线，缺失时回退基金净值日期。"""
    rows = repo.get_index_rows(code="sh000300")
    if rows:
        dates = sorted({r[0] for r in rows})
        # 与净值轴对齐（指数可能早于基金数据）
        dset = set(pivot.index)
        return [d for d in dates if d in dset]
    return list(pivot.index)


def _decision_dates(trading_dates: list[str]) -> list[str]:
    """决策日：每 20 交易日取一次（对齐 FORWARD_DAYS，避免重叠窗口伪独立）。"""
    return trading_dates[::_DECISION_STEP]


def build_panel(pivot: pd.DataFrame, labels: pd.DataFrame,
                trading_dates: list[str]) -> pd.DataFrame:
    """构建 (赛道, 决策日) 面板：动量/热度/未来收益的等权均值。

    对每个决策日 t：基金需在 t-60 至 t+20 均有净值（60 日历史 + 20 日未来）。
    赛道标签取「报告期 ≤ t 的最新季报」第一行业（季报级低频，与生产同口径）。
    """
    nav = pivot.astype(float)
    # 相对收益：与 t 的比值 - 1（各列独立，天然处理基金净值稀疏）
    mom_60 = nav / nav.shift(60) - 1.0
    mom_20 = nav / nav.shift(20) - 1.0
    mom_5 = nav / nav.shift(5) - 1.0
    ret_1 = nav / nav.shift(1) - 1.0
    fwd_20 = nav.shift(-20) / nav - 1.0

    # 基金 → 最新报告期标签的映射（按 code 分组，决策日 searchsorted）
    lab = labels.sort_values("report_date")
    by_code = {c: (lab_g["report_date"].to_numpy(),
                   lab_g["industry_1"].to_numpy())
               for c, lab_g in lab.groupby("code")}
    fund_dates = np.array(trading_dates)

    rows = []
    for t in _decision_dates(trading_dates):
        if t not in nav.index:
            continue
        t_pos = nav.index.get_loc(t)
        row = nav.loc[t]
        ok = (row.notna()
              & mom_20.loc[t].notna()
              & mom_5.loc[t].notna()
              & mom_60.loc[t].notna()
              & ret_1.loc[t].notna()
              & fwd_20.loc[t].notna())
        funds = row.index[ok]
        if len(funds) == 0:
            continue
        # 每只基金的赛道标签
        sector = []
        for c in funds:
            rd, inds = by_code.get(c, (None, None))
            if rd is None:
                sector.append(None)
                continue
            pos = np.searchsorted(rd, t, side="right") - 1
            sector.append(inds[pos] if pos >= 0 else None)
        df = pd.DataFrame({
            "code": funds,
            "sector": sector,
            "mom_5d": mom_5.loc[t, funds].to_numpy(),
            "mom_20d": mom_20.loc[t, funds].to_numpy(),
            "mom_60d": mom_60.loc[t, funds].to_numpy(),
            "ret_1d": ret_1.loc[t, funds].to_numpy(),
            "fwd_20d": fwd_20.loc[t, funds].to_numpy(),
        })
        df["date"] = t
        rows.append(df)
    panel = pd.concat(rows, ignore_index=True)
    panel = panel.dropna(subset=["sector"])
    # 等权聚合（≥_MIN_FUNDS_PER_SECTOR）
    agg = (panel.groupby(["date", "sector"])
           .agg(n=("code", "count"),
                mom_5d=("mom_5d", "mean"),
                mom_20d=("mom_20d", "mean"),
                mom_60d=("mom_60d", "mean"),
                ret_1d=("ret_1d", "mean"),
                fwd_20d=("fwd_20d", "mean"))
           .reset_index())
    agg = agg[agg["n"] >= _MIN_FUNDS_PER_SECTOR]
    return agg


def _cross_sectional_ic(panel: pd.DataFrame, feat: str,
                        target: str = "fwd_20d") -> dict:
    """截面 IC：每决策日按 feat 排序 vs 目标收益的秩相关，再平均。

    返回 {ic_mean, ic_ir(ic均值/ic标准差), ic_positive_pct, n_dates, n_rows}。
    """
    ics = []
    for _, g in panel.groupby("date"):
        ic = spearman(g[feat].to_numpy(), g[target].to_numpy())
        if ic is not None:
            ics.append(ic)
    if not ics:
        return {"ic_mean": None, "ic_ir": None, "ic_positive_pct": None,
                "n_dates": 0, "n_rows": len(panel)}
    arr = np.array(ics)
    return {
        "ic_mean": float(arr.mean()),
        "ic_ir": float(arr.mean() / arr.std()) if arr.std() > 1e-12 else None,
        "ic_positive_pct": float((arr > 0).mean()),
        "n_dates": int(len(arr)),
        "n_rows": int(len(panel)),
    }


def _decile_analysis(panel: pd.DataFrame, feat: str,
                     target: str = "fwd_20d") -> dict:
    """分位分析：每决策日按 feat 分 10 档，输出高/低档的未来收益（百分数）。

    高档(decile 10) vs 低档(decile 1)：检验「买最热/最高动量」与「买最低动量」的未来表现。
    """
    tmp = panel.copy()
    tmp["decile"] = tmp.groupby("date")[feat].transform(
        lambda x: pd.qcut(x.rank(method="first"), 10, labels=False) + 1)
    stats = (tmp.groupby("decile")[target]
             .agg(["mean", "median", "count"]).reset_index())
    d1 = stats[stats["decile"] == 1]
    d10 = stats[stats["decile"] == 10]
    if d1.empty or d10.empty:
        return {"error": "分位样本不足"}
    return {
        "d1_mean_pct": round(float(d1["mean"].iloc[0]) * 100, 2),
        "d10_mean_pct": round(float(d10["mean"].iloc[0]) * 100, 2),
        "spread_pct": round(float((d10["mean"].iloc[0] - d1["mean"].iloc[0]) * 100), 2),
        "d10_win_pct": round(float((tmp[tmp["decile"] == 10][target] > 0.01).mean() * 100), 1),
        "d1_win_pct": round(float((tmp[tmp["decile"] == 1][target] > 0.01).mean() * 100), 1),
        "n_rows": int(len(tmp)),
    }


def _overheat_analysis(panel: pd.DataFrame) -> dict:
    """过热检验：单日热度(涨幅top)与动量加速(top)赛道，未来 20 日是否跑输均值。"""
    out = {}
    for feat, label in [("ret_1d", "单日涨幅(当日热度)"),
                        ("mom_5d", "5日动量(短热)")]:
        tmp = panel.copy()
        tmp["pct"] = tmp.groupby("date")[feat].transform(
            lambda x: x.rank(pct=True))
        hot = tmp[tmp["pct"] >= 0.9]
        cold = tmp[tmp["pct"] <= 0.1]
        if len(hot) < 10 or len(cold) < 10:
            out[label] = {"样本不足": True}
            continue
        out[label] = {
            "hot_top10pct_fwd_pct": round(float(hot["fwd_20d"].mean() * 100), 2),
            "cold_bottom10pct_fwd_pct": round(float(cold["fwd_20d"].mean() * 100), 2),
            "all_mean_fwd_pct": round(float(tmp["fwd_20d"].mean() * 100), 2),
            "hot_win_pct": round(float((hot["fwd_20d"] > 0.01).mean() * 100), 1),
            "n_hot": int(len(hot)),
        }
    return out


def _guard_check(panel: pd.DataFrame) -> dict:
    """动量护栏检验：mom_20d < -15%（当前 guard）的赛道未来 20 日表现。"""
    crash = panel[panel["mom_20d"] < -0.15]
    rest = panel[panel["mom_20d"] >= -0.15]
    return {
        "crash_fwd_pct": round(float(crash["fwd_20d"].mean() * 100), 2) if len(crash) else None,
        "crash_win_pct": round(float((crash["fwd_20d"] > 0.01).mean() * 100), 1) if len(crash) else None,
        "rest_fwd_pct": round(float(rest["fwd_20d"].mean() * 100), 2) if len(rest) else None,
        "n_crash": int(len(crash)),
        "n_rest": int(len(rest)),
    }


def _chase_vs_launch(panel: pd.DataFrame) -> dict:
    """追高 vs 启动四象限：短中期动量双高（追高）是否比刚启动更差。

    象限切分用每决策日 mom_5d / mom_20d 的截面中位数：
      追高: mom_5d 高 且 mom_20d 高（已涨很多还在加速）
      启动: mom_5d 高 且 mom_20d 低（刚开始涨）
    检验「买在追高象限」是否均值回归（未来跑输）。
    """
    tmp = panel.copy()
    tmp["m5_hi"] = tmp.groupby("date")["mom_5d"].transform(lambda x: x >= x.median())
    tmp["m20_hi"] = tmp.groupby("date")["mom_20d"].transform(lambda x: x >= x.median())
    quads = {
        "追高(5d高+20d高)": (tmp["m5_hi"] & tmp["m20_hi"]),
        "启动(5d高+20d低)": (tmp["m5_hi"] & ~tmp["m20_hi"]),
        "弱化(5d低+20d高)": (~tmp["m5_hi"] & tmp["m20_hi"]),
        "双低(5d低+20d低)": (~tmp["m5_hi"] & ~tmp["m20_hi"]),
    }
    out = {}
    for name, mask in quads.items():
        g = tmp[mask]
        out[name] = {
            "fwd_pct": round(float(g["fwd_20d"].mean() * 100), 2) if len(g) else None,
            "win_pct": round(float((g["fwd_20d"] > 0.01).mean() * 100), 1) if len(g) else None,
            "n": int(len(g)),
        }
    return out


def _regime_breakdown(panel: pd.DataFrame) -> dict:
    """市场状态分解：BULL/BEAR 下热度的预测力是否不同。

    用沪深300 收盘 vs MA60 判定各决策日的 regime（与生产 get_market_regime 同口径）。
    """
    idx = repo.get_index_series("sh000300", ("date", "close"))
    closes = pd.Series({d: c for d, c in idx})
    closes.index = pd.to_datetime(closes.index)
    ma60 = closes.rolling(60).mean()
    regime_of_date = (closes > ma60).map({True: "BULL", False: "BEAR"})
    tmp = panel.copy()
    tmp["date"] = pd.to_datetime(tmp["date"])
    tmp["regime"] = tmp["date"].map(regime_of_date)
    tmp["pct"] = tmp.groupby("date")["ret_1d"].transform(lambda x: x.rank(pct=True))
    out = {}
    for reg in ("BULL", "BEAR"):
        g = tmp[tmp["regime"] == reg]
        if len(g) < 10:
            continue
        hot = g[g["pct"] >= 0.9]
        cold = g[g["pct"] <= 0.1]
        out[reg] = {
            "hot_fwd_pct": round(float(hot["fwd_20d"].mean() * 100), 2) if len(hot) else None,
            "all_fwd_pct": round(float(g["fwd_20d"].mean() * 100), 2),
            "n": int(len(g)),
            "n_dates": int(g["date"].nunique()),
        }
    return out


def run(start: str, end: str) -> dict:
    logger.info("加载持仓标签…")
    labels = _load_holdings_labels()
    logger.info("持仓标签 %d 条（基金×报告期）", len(labels))
    logger.info("加载净值透视…")
    pivot = _load_nav_pivot()
    logger.info("净值透视 %d 日 × %d 基金", pivot.shape[0], pivot.shape[1])
    trading_dates = [d for d in _load_trading_dates(pivot) if start <= d <= end]
    logger.info("交易日历 %d 日（窗口 %s ~ %s）", len(trading_dates), start, end)

    logger.info("构建面板…")
    panel = build_panel(pivot, labels, trading_dates)
    logger.info("面板 %d 行（%d 个决策日 × 赛道）", len(panel), panel["date"].nunique())
    if panel.empty:
        return {"error": "面板为空（数据不足）"}

    result = {"n_dates": int(panel["date"].nunique()),
              "n_sector_dates": int(len(panel))}
    for feat, label in [("mom_5d", "mom_5d"),
                        ("mom_20d", "mom_20d"),
                        ("mom_60d", "mom_60d"),
                        ("ret_1d", "ret_1d(当日热度)")]:
        ic = _cross_sectional_ic(panel, feat)
        result[f"ic_{feat}"] = ic
        result[f"decile_{feat}"] = _decile_analysis(panel, feat)
    result["overheat"] = _overheat_analysis(panel)
    result["guard_check"] = _guard_check(panel)
    result["chase_vs_launch"] = _chase_vs_launch(panel)
    result["regime_breakdown"] = _regime_breakdown(panel)

    _OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    logger.info("结果写入 %s", _OUT_PATH)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="赛道信号离线回测")
    parser.add_argument("--start", default="2019-01-01")
    parser.add_argument("--end", default="2026-07-31")
    args = parser.parse_args()
    result = run(args.start, args.end)

    print("\n=== 截面 IC（秩相关，>0 表示有排序预测力）===")
    for k, v in result.items():
        if k.startswith("ic_"):
            print(f"{k:>14}: ic={v.get('ic_mean')}, ir={v.get('ic_ir')}, "
                  f"正占比={v.get('ic_positive_pct')}, 决策日={v.get('n_dates')}")
    print("\n=== 十分位收益（未来20日绝对收益 %，>1% 为赚钱口径）===")
    for k, v in result.items():
        if k.startswith("decile_"):
            print(f"{k:>14}: D1={v.get('d1_mean_pct')}%  D10={v.get('d10_mean_pct')}%  "
                  f"价差={v.get('spread_pct')}%  D10胜率={v.get('d10_win_pct')}%  "
                  f"D1胜率={v.get('d1_win_pct')}%")
    print("\n=== 过热检验 ===")
    for k, v in result.get("overheat", {}).items():
        print(f"{k}: {v}")
    print("\n=== 追高 vs 启动四象限（未来20日）===")
    for k, v in result.get("chase_vs_launch", {}).items():
        print(f"{k}: {v}")
    print("\n=== 市场状态分解（热度赛道未来20日）===")
    for k, v in result.get("regime_breakdown", {}).items():
        print(f"{k}: {v}")
    print("\n=== 动量护栏检验（guard=-15%）===")
    print(result.get("guard_check"))


if __name__ == "__main__":
    main()

"""回测 regime 判定单一口径（架构深化 ⑥ 统一）。

生产判定：domain.regime_from_multi_timeframe（EMA60+EMA250 双确认，矛盾归 NEUTRAL，
见 domain.regime_from_multi_timeframe / repo.base.get_market_regime）。
回测兼容：无 ema250 数据/不足预热时回退单周期 EMA60（旧行为，兼容早期区间与旧测试）。

各回测脚本（backtest / backtest_walkforward / backtest_exit_walkforward /
sector_signals / bear_market_research）统一经本模块判定，禁止各自实现。
"""

import pandas as pd

from app import domain

__all__ = ["regime_at_date", "regime_series", "ema250_of"]


def ema250_of(close: pd.Series) -> pd.Series:
    """EMA250 序列（与生产 repo/base get_market_regime 的纯 Python 实现同口径的向量版）。"""
    return close.ewm(span=250, adjust=False).mean()


def regime_at_date(idx_df: pd.DataFrame, date: pd.Timestamp) -> str:
    """回测日 regime：优先多周期共振（close vs EMA60 vs EMA250）；
    无 EMA250 列/数据不足时回退单周期（旧行为，兼容旧测试与早期区间）。
    """
    row = idx_df.loc[idx_df.index <= date]
    if len(row) == 0:
        return domain.REGIME_NEUTRAL
    last = row.iloc[-1]
    ema250 = last.get("ema250") if "ema250" in idx_df.columns else None
    if ema250 is not None and pd.notna(ema250):
        return domain.regime_from_multi_timeframe(last["close"], last["ema60"], float(ema250))
    return domain.regime_from_close_ema60(last["close"], last["ema60"])


def regime_series(close: pd.Series, ema60: pd.Series | None = None,
                  ema250: pd.Series | None = None) -> pd.Series:
    """逐日 regime 向量（BULL/BEAR/NEUTRAL），与 regime_at_date 同口径。

    ema60 缺省时按 close 自算；ema250 提供且非 NaN 时走多周期共振（矛盾归 NEUTRAL），
    否则回退单周期 EMA60——与生产/回测日判定一致。
    """
    e60 = ema60 if ema60 is not None else close.ewm(span=60, adjust=False).mean()
    e250 = ema250 if ema250 is not None else ema250_of(close)
    out = close.map(lambda _: domain.REGIME_NEUTRAL).astype(object)
    # 多周期：EMA60 与 EMA250 均可信处用双确认；否则回退单周期 EMA60
    multi_ok = e250.notna()
    out[multi_ok] = [domain.regime_from_multi_timeframe(c, e60[i], float(e250[i]))
                     for i, c in close[multi_ok].items()]
    single = ~multi_ok
    out[single] = [domain.regime_from_close_ema60(c, e60[i])
                   for i, c in close[single].items()]
    return out

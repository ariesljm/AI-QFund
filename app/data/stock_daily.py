"""个股日线（搜狐 hisHq，票 07 数据源；自 sector_history 解耦，供 22 删除前置）。

**来源**：搜狐 `q.stock.sohu.com/hisHq`（2026-09 实测稳定 200，沪深/创业/科创
全市场可用；原腾讯 `web.ifzq.gtimg.cn` 被 WAF 概率拦截）。
**复权口径**：搜狐返回**前复权**收盘价——`domain.adjusted_returns` 据此算
复权日收益（`cur/prev−1` 天然含分红/拆股调整）。
**行格式**：`[日期, 开盘, 收盘, 涨跌额, 涨跌幅%, 最低, 最高, 量, 额, 换手, 量比]`，
收盘取 **index 2**（index 1 是开盘价，误取会含隔夜跳空噪声）；响应 GBK 编码。
**窗口**：按自然日起止区间拉取，`days × 1.5` 覆盖（600 交易日 ≈ 900 自然日）。
"""

from app.data.fetchers import fetch
from app.utils.log import get_logger

logger = get_logger("data.stock_daily")

_SOHU_KLINE = "https://q.stock.sohu.com/hisHq"


def tx_symbol(code: str) -> str:
    """6 位 A 股代码 → 搜狐符号（cn_ 前缀，沪深/创业/科创通用）。"""
    return f"cn_{code}"


def _sohu_start_days(days: int) -> int:
    """600 个交易日约需 900 自然日（搜狐按自然日区间拉取）。"""
    return int(days * 1.5)


def stock_daily(code: str, days: int = 600) -> dict[str, float]:
    """搜狐历史日线，返回 {date: 前复权收盘价}；无数据返回空 dict。

    响应 GBK；收盘取 index 2（前复权口径，见模块 docstring）。
    """
    from datetime import datetime, timedelta
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=_sohu_start_days(days))).strftime("%Y%m%d")
    r = fetch(_SOHU_KLINE, params={
        "code": tx_symbol(code), "start": start, "end": end,
        "stat": "1", "order": "D", "period": "d",
    })
    try:
        import json
        data = json.loads(r.content.decode("gbk", errors="replace"))
    except Exception:
        return {}
    hq = ((data or [{}])[0] or {}).get("hq") or []
    out: dict[str, float] = {}
    for row in hq:
        if len(row) >= 3 and row[0] and row[2] not in (None, ""):
            try:
                out[str(row[0])] = float(row[2])
            except (TypeError, ValueError):
                continue
    return out

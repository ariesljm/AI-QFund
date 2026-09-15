"""个股估值日频回填：东财 RPT_VALUEANALYSIS_DET（票 05）。

数据源（2026-09 实测）：datacenter-web.eastmoney.com/api/data/v1/get
reportName=RPT_VALUEANALYSIS_DET，日频返回 PE_TTM / PB_MRQ / TOTAL_MARKET_CAP /
CLOSE_PRICE / PEG_CAR，一次按页拉全（茅台 2112 条，无年度限制）。

估值表 PRIMARY KEY (stock_code, date)，save 用 INSERT OR REPLACE → 幂等；
backfill 逐只提交 → 断点续传（中断后已完成的股票不重复拉取）。
"""

from app.data.fetchers import fetch
from app.data.store import save_stock_valuation
from app.utils.log import get_logger

logger = get_logger("data.valuation")

_VALUATION_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_PAGE_SIZE = 500
# 只取需要的列，减小响应体（全列会带 ~30 个字段）
_COLUMNS = "SECURITY_CODE,TRADE_DATE,PE_TTM,PB_MRQ,TOTAL_MARKET_CAP"


def fetch_stock_valuation(code: str) -> list[tuple[str, float | None, float | None, float | None]]:
    """单只股票全历史日频估值，返回 [(date, pe_ttm, pb_mrq, market_cap)]（升序）。

    无数据（停牌/退市/代码不存在）返回空列表。失败不吞异常（由调用方重试/记缺口）。
    """
    rows: list[tuple[str, float | None, float | None, float | None]] = []
    page = 1
    while True:
        r = fetch(_VALUATION_URL, params={
            "reportName": "RPT_VALUEANALYSIS_DET",
            "columns": _COLUMNS,
            "filter": f'(SECURITY_CODE="{code}")',
            "pageNumber": str(page),
            "pageSize": str(_PAGE_SIZE),
            "sortColumns": "TRADE_DATE",
            "sortTypes": "1",
            "source": "HSF10",
            "client": "PC",
        })
        result = (r.json().get("result") or {}) if r else {}
        batch = result.get("data") or []
        if not batch:
            break
        for it in batch:
            d = str(it.get("TRADE_DATE") or "")[:10]
            if not d:
                continue
            rows.append((d, it.get("PE_TTM"), it.get("PB_MRQ"), it.get("TOTAL_MARKET_CAP")))
        total = result.get("count") or 0
        if page * _PAGE_SIZE >= total:
            break
        page += 1
    # 接口 sortTypes=1 已升序；防御性再排一次（跨页稳定）
    rows.sort(key=lambda x: x[0])
    return rows


def backfill_valuations(codes: list[str]) -> dict[str, int]:
    """对给定股票集合回填估值日频。返回 {"done": N, "empty": N, "failed": N}。

    幂等（INSERT OR REPLACE）+ 断点续传（逐只 save 提交）；不抛异常，
    单只失败记数继续，缺失清单由返回的 failed 计数 + 日志定位。
    """
    done = empty = failed = 0
    for code in codes:
        try:
            rows = fetch_stock_valuation(code)
        except Exception as e:
            failed += 1
            logger.warning("估值回填失败 %s: %s", code, str(e)[:120])
            continue
        if not rows:
            empty += 1
            continue
        save_stock_valuation(code, rows)
        done += 1
    return {"done": done, "empty": empty, "failed": failed}

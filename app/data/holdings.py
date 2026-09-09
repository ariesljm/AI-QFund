"""数据基座持仓数据 module（架构深化 ⑨ 拆分）：持仓 HTML 解析 + 批量下载。

从 foundation.py 拆出：持仓抓取/解析是自洽概念，独立可测（test_holdings_encoding）。
"""

import asyncio
import re
import time
from datetime import datetime

import httpx

from app.data.fetchers import fetch, fetch_async
from app.data.ingest import filter_cooldown_targets, run_batched_fetch
from app.data.store import save_holdings_batch
from app.database import db_conn
from app.repo.base import get_buyable_codes, get_holdings_report_dates
from app.utils.log import get_logger

logger = get_logger("data.holdings")

_API_HOLDINGS_URL = "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"

_HOLDINGS_NO_UPDATE_COOLDOWN_DAYS = 30


_HOLDING_DATE_RE = re.compile(r"([\d]{4}-[\d]{2}-[\d]{2})</font></label>")
_HOLDING_ROW_RE = re.compile(
    r"<td>\d+</td>"
    r"<td><a[^>]*>(\d+)</a></td>"
    r"<td class='tol'><a[^>]*>([^<]+)</a></td>"
    r".*?<td class='tor'>([\d.]+)%</td>",
    re.DOTALL,
)




def _parse_holdings_html(text: str) -> tuple[str | None, list[dict]]:
    date_m = _HOLDING_DATE_RE.search(text)
    report_date = date_m.group(1) if date_m else None
    holdings = []
    for m in _HOLDING_ROW_RE.finditer(text):
        stock_code, stock_name, weight_str = m.group(1), m.group(2), m.group(3)
        try:
            weight = float(weight_str)
        except ValueError:
            weight = 0.0
        holdings.append({
            "stock_code": stock_code,
            "stock_name": stock_name,
            "weight": weight,
        })
    return report_date, holdings


_HOLDINGS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://fundf10.eastmoney.com/",
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
}


async def _async_fetch_holdings_one(
    session: "httpx.AsyncClient",
    code: str,
    holdings_url: str,
    semaphore: asyncio.Semaphore,
) -> tuple[str, str | None, list[dict], bool]:
    params = {"type": "jjcc", "code": code, "topline": "10", "year": "", "month": ""}
    async with semaphore:
        try:
            resp = await fetch_async(
                session, holdings_url, params=params, timeout=15,
                headers=_HOLDINGS_HEADERS,
            )
            raw = resp.content
            # 东财持仓接口 charset 已由 gbk 改为 utf-8（Content-Type 声明）：以响应声明
            # 编码为准，回退 gbk 兑底。旧代码取 getattr(resp, "charset")（httpx 无此属性，
            # 恒为 None → 永远 gbk）导致 utf-8 内容被 gbk 解码成乱码股票名。
            charset = getattr(resp, "encoding", None) or "gbk"
            try:
                text = raw.decode(charset)
            except (UnicodeDecodeError, LookupError):
                text = raw.decode("gbk", errors="replace")
            report_date, holdings = _parse_holdings_html(text)
            return code, report_date, holdings, False
        except Exception as e:
            logger.debug("基金 %s 持仓异步拉取失败: %s", code, str(e)[:120], exc_info=True)
            return code, None, [], True


async def async_download_all_holdings(
    concurrency: int = 6,
    batch_size: int = 200,
) -> int:
    holdings_url = _API_HOLDINGS_URL

    all_codes = get_buyable_codes()

    today = datetime.now()
    m, d = today.month, today.day
    # 季报披露窗口：公募季报须于季度结束后 15 个工作日内披露，实际多数在
    # 每季度末月（1/4/7/10月）下旬初公布完毕。取 22 日为切换点：
    # 早于窗口时目标停在上一季（避免拉到半套数据），过窗后推进到最新完整季度。
    if m == 1 and d <= 22:
        latest_quarter = f"{today.year - 1}-09-30"
    elif m < 4 or (m == 4 and d <= 21):
        latest_quarter = f"{today.year - 1}-12-31"
    elif m < 7 or (m == 7 and d <= 21):
        latest_quarter = f"{today.year}-03-31"
    elif m < 10 or (m == 10 and d <= 21):
        latest_quarter = f"{today.year}-06-30"
    else:
        latest_quarter = f"{today.year}-09-30"

    local_latest = get_holdings_report_dates()
    all_codes = [
        c for c in all_codes
        if local_latest.get(c) is None or local_latest[c] < latest_quarter
    ]
    logger.info(
        "持仓增量模式：最新季报 %s, 已是最新 %d 只跳过, 待下载 %d 只",
        latest_quarter, len(local_latest) - len(all_codes), len(all_codes),
    )

    all_codes = filter_cooldown_targets("holdings", all_codes, "持仓",
                                        stage_cooldown_days={"no_update": _HOLDINGS_NO_UPDATE_COOLDOWN_DAYS})

    with db_conn() as conn:
        semaphore = asyncio.Semaphore(concurrency)
        limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
        total_rows = 0
        funds_with_holdings = 0
        start_time = time.monotonic()

        async with httpx.AsyncClient(headers=_HOLDINGS_HEADERS, limits=limits, trust_env=False) as session:
            async def _fetch_holdings(session_, code) -> tuple[str, tuple[str | None, list[dict]], bool]:
                code_, report_date, holdings, failed = await _async_fetch_holdings_one(
                    session_, code, holdings_url, semaphore)
                return code_, (report_date, holdings), failed

            def _save_holdings_batch(conn_, results) -> dict:
                nonlocal total_rows, funds_with_holdings
                batch_rows = 0
                success: set[str] = set()
                failed_codes: list[str] = []
                no_update: list[str] = []
                for code, (report_date, holdings), failed in results:
                    if failed:
                        failed_codes.append(code)
                        continue
                    success.add(code)
                    if holdings and report_date and report_date != local_latest.get(code):
                        save_holdings_batch(conn_, [
                            (code, report_date, h["stock_code"], h["stock_name"], h["weight"])
                            for h in holdings
                        ])
                        batch_rows += len(holdings)
                        funds_with_holdings += 1
                        local_latest[code] = report_date
                    elif not holdings:
                        # 接口确认无持仓披露（ETF联接/商品基金等）：计入失败累计，
                        # 满 3 个周期进 30 天长冷却，不再每周反复请求
                        no_update.append(code)
                conn_.commit()
                total_rows += batch_rows
                return {"new_count": batch_rows, "success": success,
                        "no_update": no_update, "failed": failed_codes}

            def _backfill_holdings(code) -> None:
                nonlocal total_rows, funds_with_holdings
                resp = fetch(
                    holdings_url,
                    params={"type": "jjcc", "code": code, "topline": "10", "year": "", "month": ""},
                    headers=_HOLDINGS_HEADERS, timeout=10,
                )
                report_date, holdings = _parse_holdings_html(resp.text)
                if holdings and report_date and report_date != local_latest.get(code):
                    save_holdings_batch(conn, [
                        (code, report_date, h["stock_code"], h["stock_name"], h["weight"])
                        for h in holdings
                    ])
                    total_rows += len(holdings)
                    funds_with_holdings += 1
                    local_latest[code] = report_date

            await run_batched_fetch(
                session, fetch_type="holdings", label="持仓",
                targets=all_codes, batch_size=batch_size, conn=conn,
                fetch_one=_fetch_holdings, handle_batch=_save_holdings_batch,
                backfill_one=_backfill_holdings, primary_note="持仓拉取失败",
                no_update_note="接口无持仓披露", no_update_guard=False,
            )
            conn.commit()

    elapsed = time.monotonic() - start_time
    logger.info(
        "持仓下载完成: %d 只有持仓, 共 %d 条, 耗时 %.1f 秒",
        funds_with_holdings, total_rows, elapsed,
    )
    return total_rows


# ── 行业映射 ──


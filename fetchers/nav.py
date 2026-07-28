"""异步净值获取器。"""

import asyncio
import time
from datetime import datetime

from data_store import _db_conn, _save_nav_batch, _backfill_guard
from fetch import fetch_async

HAS_AIOHTTP = True
try:
    import aiohttp
except ImportError:
    HAS_AIOHTTP = False

_API_LSJZ_URL = "https://api.fund.eastmoney.com/f10/lsjz"
_API_PINGZHONGDATA_URL = "http://fund.eastmoney.com/pingzhongdata/{code}.js"

_LSJZ_HEADERS = {
    "Referer": "https://fund.eastmoney.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}

from log_utils import get_logger
logger = get_logger("fetchers.nav")


def _parse_lsjz_list(lsjz_list: list[dict]) -> list[dict]:
    result = []
    for item in lsjz_list:
        try:
            date = item.get("FSDATE") or ""
            if not date:
                continue
            unit_nav = float(item.get("NAV") or 0)
            cum_nav = float(item.get("ACCUMULATEDNAV") or 0)
            if unit_nav <= 0:
                continue
            equity_return = float(item.get("ADJUSTEDNAV") or 0)
            dividend_str = item.get("DIVIDEND") or ""
            dividend = float(dividend_str) if dividend_str and dividend_str != "-" else None
            result.append({
                "date": date[:10],
                "unit_nav": unit_nav,
                "cum_nav": cum_nav,
                "equity_return": equity_return,
                "dividend": dividend,
            })
        except (ValueError, TypeError):
            continue
    return result


async def _async_fetch_lsjz(
    session: "aiohttp.ClientSession",
    code: str,
    lsjz_url: str,
    start_date: str,
    end_date: str,
    semaphore: asyncio.Semaphore,
) -> tuple[str, list[dict], bool]:
    all_navs: list[dict] = []
    first_failed = False
    page = 1
    page_size = 60

    async with semaphore:
        while True:
            params = {
                "fundCode": code,
                "pageIndex": page,
                "pageSize": page_size,
            }
            if start_date:
                params["startDate"] = start_date
                params["endDate"] = end_date

            try:
                resp = await fetch_async(
                    session, lsjz_url, params=params, timeout=15,
                    headers=_LSJZ_HEADERS,
                )
                data = await resp.json(content_type=None)
                lsjz_list = (data.get("Data") or {}).get("LSJZList") or []
                total = data.get("TotalCount") or 0
            except Exception as e:
                logger.debug("基金 %s lsjz 拉取失败(第%d页): %s", code, page, str(e)[:120], exc_info=True)
                if page == 1:
                    first_failed = True
                break

            all_navs.extend(_parse_lsjz_list(lsjz_list))

            if start_date:
                break
            if page * page_size >= total or not lsjz_list:
                break
            page += 1

    return code, all_navs, first_failed


async def async_update_nav_incremental(
    concurrency: int = 20,
    batch_size: int = 200,
) -> int:
    if not HAS_AIOHTTP:
        raise RuntimeError("需要安装 aiohttp: uv add aiohttp")

    lsjz_url = _API_LSJZ_URL
    end_date = datetime.now().strftime("%Y-%m-%d")

    with _db_conn() as conn:
        all_codes = [
            r[0] for r in conn.execute("SELECT code FROM fund_basic WHERE is_buyable = 1").fetchall()
        ]
        local_max = dict(
            conn.execute("SELECT code, MAX(date) FROM fund_nav GROUP BY code").fetchall()
        )
        global_latest = conn.execute("SELECT MAX(date) FROM fund_nav").fetchone()[0]

    tasks_meta: list[tuple[str, str]] = []
    incr_cnt = 0
    full_cnt = 0
    for code in all_codes:
        lm = local_max.get(code)
        if lm == global_latest:
            continue
        if lm:
            tasks_meta.append((code, lm))
            incr_cnt += 1
        else:
            tasks_meta.append((code, ""))
            full_cnt += 1

    logger.info(
        "真·增量：跳过已最新 %d 只，增量 %d 只，全量兜底 %d 只(无本地数据)",
        len(all_codes) - len(tasks_meta), incr_cnt, full_cnt,
    )

    if not tasks_meta:
        logger.info("无需增量更新")
        return 0

    semaphore = asyncio.Semaphore(concurrency)
    connector = aiohttp.TCPConnector(limit=concurrency, ttl_dns_cache=300, enable_cleanup_closed=True)
    total_new = 0
    total_done = 0
    all_failed: set[str] = set()
    start_time = time.monotonic()

    async with aiohttp.ClientSession(headers=_LSJZ_HEADERS, connector=connector) as session:
        for i in range(0, len(tasks_meta), batch_size):
            batch = tasks_meta[i : i + batch_size]
            coros = [
                _async_fetch_lsjz(session, code, lsjz_url, start, end_date, semaphore)
                for code, start in batch
            ]
            results = await asyncio.gather(*coros)

            batch_new = 0
            with _db_conn() as conn:
                for code, navs, failed in results:
                    batch_new += _save_nav_batch(conn, code, navs)
                    total_done += 1
                    if failed:
                        all_failed.add(code)
            total_new += batch_new

            elapsed = time.monotonic() - start_time
            speed = total_done / elapsed if elapsed > 0 else 0
            logger.info(
                "进度 %d/%d (+%d), 速度 %.1f/s",
                total_done, len(tasks_meta), batch_new, speed,
            )

    if _backfill_guard(all_failed, len(tasks_meta), "净值增量"):
            import requests
            for code in all_failed:
                try:
                    resp = requests.get(
                        lsjz_url,
                        params={"fundCode": code, "pageIndex": 1, "pageSize": 60,
                                "startDate": "", "endDate": end_date},
                        headers=_LSJZ_HEADERS, timeout=10,
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        lsjz_list = (data.get("Data") or {}).get("LSJZList") or []
                        navs = _parse_lsjz_list(lsjz_list)
                        if navs:
                            with _db_conn() as conn:
                                total_new += _save_nav_batch(conn, code, navs)
                except Exception as e:
                    logger.debug("补查基金 %s 净值增量失败: %s", code, str(e)[:120], exc_info=True)

    elapsed = time.monotonic() - start_time
    logger.info("增量更新完成: %d 条净值, 耗时 %.1f 秒", total_new, elapsed)
    return total_new


async def async_download_all_nav(
    concurrency: int = 20,
    batch_size: int = 200,
    force_full: bool = False,
) -> int:
    if not HAS_AIOHTTP:
        raise RuntimeError("需要安装 aiohttp: uv add aiohttp")

    url_template = _API_PINGZHONGDATA_URL

    with _db_conn() as conn:
        cur = conn.execute("SELECT code FROM fund_basic WHERE is_buyable = 1")
        all_codes = [r[0] for r in cur.fetchall()]

        if not force_full:
            row = conn.execute("SELECT MAX(date) FROM fund_nav").fetchone()
            global_latest = row[0] if row and row[0] else None
            if global_latest:
                local_max = dict(
                    conn.execute(
                        "SELECT code, MAX(date) FROM fund_nav GROUP BY code"
                    ).fetchall()
                )
                skipped = [c for c in all_codes if local_max.get(c) == global_latest]
                all_codes = [c for c in all_codes if local_max.get(c) != global_latest]
                logger.info(
                    "增量模式：跳过 %d 只已最新(%s)，待下载 %d 只",
                    len(skipped), global_latest, len(all_codes),
                )

    logger.info("待下载基金: %d 只, 并发数: %d", len(all_codes), concurrency)

    if not all_codes:
        logger.info("所有基金均已最新，无需下载")
        return 0

    total_new = 0
    total_done = 0
    all_failed: set[str] = set()
    start_time = time.monotonic()

    import requests as sync_requests

    async def _fetch_one(code: str) -> tuple[str, list[dict], bool]:
        nonlocal total_new, total_done
        import re
        try:
            url = url_template.replace("{code}", code)
            resp = await fetch_async(
                None, url, timeout=15,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                },
            )
            text = await resp.text()
            text = text.strip()
            # 提取 Data_netWorthTrend
            m = re.search(r"Data_netWorthTrend\s*=\s*(\[.+?\]);", text, re.DOTALL)
            if not m:
                return code, [], True
            trend = json.loads(m.group(1))
            navs = _parse_pingzhong_navs(trend)
            return code, navs, False
        except Exception as e:
            logger.debug("基金 %s 全量净值拉取失败: %s", code, str(e)[:120], exc_info=True)
            return code, [], True

    semaphore = asyncio.Semaphore(concurrency)
    connector = aiohttp.TCPConnector(limit=concurrency, ttl_dns_cache=300, enable_cleanup_closed=True)

    async with aiohttp.ClientSession(connector=connector) as session:
        for i in range(0, len(all_codes), batch_size):
            batch = all_codes[i : i + batch_size]
            coros = [_fetch_one(code) for code in batch]
            results = await asyncio.gather(*coros)

            batch_new = 0
            with _db_conn() as conn:
                for code, navs, failed in results:
                    batch_new += _save_nav_batch(conn, code, navs)
                    total_done += 1
                    if failed:
                        all_failed.add(code)
            total_new += batch_new

            elapsed = time.monotonic() - start_time
            speed = total_done / elapsed if elapsed > 0 else 0
            logger.info(
                "全量净值进度 %d/%d (+%d), 速度 %.1f/s",
                total_done, len(all_codes), batch_new, speed,
            )

    if _backfill_guard(all_failed, len(all_codes), "全量净值"):
            for code in all_failed:
                try:
                    url = url_template.replace("{code}", code)
                    resp = sync_requests.get(url, timeout=15)
                    text = resp.text.strip()
                    import re
                    m = re.search(r"Data_netWorthTrend\s*=\s*(\[.+?\]);", text, re.DOTALL)
                    if m:
                        import json
                        trend = json.loads(m.group(1))
                        navs = _parse_pingzhong_navs(trend)
                        if navs:
                            with _db_conn() as conn:
                                total_new += _save_nav_batch(conn, code, navs)
                except Exception as e:
                    logger.debug("补查基金 %s 全量净值失败: %s", code, str(e)[:120], exc_info=True)

    elapsed = time.monotonic() - start_time
    logger.info("全量净值更新完成: %d 条, 耗时 %.1f 秒", total_new, elapsed)
    return total_new


def _parse_pingzhong_navs(trend: list[dict]) -> list[dict]:
    import json as _json
    navs = []
    for item in trend:
        try:
            ts = item.get("x", 0)
            if not ts:
                continue
            date = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")
            unit_nav = float(item.get("y", 0))
            equity_return = float(item.get("equityReturn", 0))
            if unit_nav <= 0:
                continue
            navs.append({
                "date": date,
                "unit_nav": unit_nav,
                "cum_nav": unit_nav,
                "equity_return": equity_return,
                "dividend": None,
            })
        except (ValueError, TypeError):
            continue
    return navs
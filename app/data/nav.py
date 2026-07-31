"""净值数据抓取：全量下载 & 增量更新。"""

import asyncio
import json
import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

from app.database import db_conn
from app.data.fetchers import fetch, fetch_async
from app.data.store import save_nav_batch, backfill_guard
from app.utils.log import get_logger

try:
    import aiohttp
except ImportError:
    aiohttp = None

logger = get_logger("nav")


def parse_pingzhong_navs(trend: list) -> list[dict]:
    navs = []
    for item in trend:
        date_ts = item.get("x")
        if date_ts is None:
            continue
        if isinstance(date_ts, (int, float)):
            d = datetime(1970, 1, 1) + timedelta(milliseconds=date_ts)
            date_str = d.strftime("%Y-%m-%d")
        elif isinstance(date_ts, str):
            date_str = date_ts
        else:
            continue
        cum_nav = item.get("y")
        unit_nav = item.get("y2", cum_nav)
        equityReturn = item.get("equityReturn")
        dividend = item.get("dividend")
        if cum_nav is None:
            continue
        nav = {"date": date_str, "cum_nav": float(cum_nav), "unit_nav": float(unit_nav)}
        if equityReturn is not None:
            nav["equityReturn"] = float(equityReturn)
        if dividend is not None:
            nav["dividend"] = float(dividend)
        navs.append(nav)
    return navs


async def async_update_nav_incremental(concurrency: int = 5) -> int:
    with db_conn() as conn:
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
        if global_latest is not None and lm == global_latest:
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
        return 0

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://fund.eastmoney.com/",
    }

    async def _async_fetch_lsjz(code: str, start_date: str) -> tuple[str, list[dict], bool]:
        nonlocal total_new
        lsjz_url = (
            "https://api.fund.eastmoney.com/f10/lsjz?"
            f"callback=jQuery&fundCode={code}&pageIndex=1&pageSize=9999"
        )
        if start_date:
            lsjz_url += f"&startDate={start_date}"
        try:
            resp = await fetch_async(
                session, lsjz_url, timeout=15, headers=headers,
            )
            text = await resp.text()
            m = re.search(r"jQuery\((.+)\)$", text.strip())
            if not m:
                return code, [], True
            data = json.loads(m.group(1))
            records = data.get("Data", {}).get("LSJZList", [])
            navs = []
            for r2 in records:
                unit_nav_str = r2.get("DWJZ", "")
                cum_nav_str = r2.get("LJJZ", "")
                if not unit_nav_str or not cum_nav_str:
                    continue
                navs.append({
                    "date": r2["FSRQ"],
                    "unit_nav": float(unit_nav_str),
                    "cum_nav": float(cum_nav_str),
                })
            logger.debug("增量 %s: %d 条净值", code, len(navs))
            return code, navs, False
        except Exception as e:
            logger.debug("基金 %s 增量净值拉取失败: %s", code, str(e)[:120], exc_info=True)
            return code, [], True

    semaphore = asyncio.Semaphore(concurrency)
    connector = aiohttp.TCPConnector(limit=concurrency, ttl_dns_cache=300, enable_cleanup_closed=True)

    async with aiohttp.ClientSession(connector=connector) as session:
        batch_size = 100
        total_new = 0
        all_failed: list[str] = []
        for i in range(0, len(tasks_meta), batch_size):
            batch = tasks_meta[i: i + batch_size]
            coros = [_async_fetch_lsjz(code, start) for code, start in batch]
            results = await asyncio.gather(*coros)

            batch_new = 0
            with db_conn() as conn:
                for code, navs, failed in results:
                    if failed:
                        all_failed.append(code)
                    else:
                        n = save_nav_batch(conn, code, navs)
                        batch_new += n
                        total_new += n
            if batch_new:
                logger.info("增量净值批次写入 %d 条", batch_new)

        if all_failed:
            if backfill_guard(all_failed, len(tasks_meta), "增量净值"):
                for code in all_failed:
                    try:
                        lsjz_url = (
                            "https://api.fund.eastmoney.com/f10/lsjz?"
                            f"callback=jQuery&fundCode={code}&pageIndex=1&pageSize=9999"
                        )
                        resp = fetch(
                            lsjz_url,
                            timeout=15, headers=headers,
                        )
                        text = resp.text
                        m = re.search(r"jQuery\((.+)\)$", text.strip())
                        if m:
                            data = json.loads(m.group(1))
                            records = data.get("Data", {}).get("LSJZList", [])
                            navs = []
                            for r2 in records:
                                unit_nav_str = r2.get("DWJZ", "")
                                cum_nav_str = r2.get("LJJZ", "")
                                if not unit_nav_str or not cum_nav_str:
                                    continue
                                navs.append({
                                    "date": r2["FSRQ"],
                                    "unit_nav": float(unit_nav_str),
                                    "cum_nav": float(cum_nav_str),
                                })
                            if navs:
                                with db_conn() as conn:
                                    save_nav_batch(conn, code, navs)
                    except Exception as e2:
                        logger.debug("补查 %s 失败: %s", code, str(e2)[:80])

    return total_new


async def async_download_all_nav(concurrency: int = 50) -> int:
    with db_conn() as conn:
        all_codes = [
            r[0] for r in conn.execute("SELECT code FROM fund_basic WHERE is_buyable = 1").fetchall()
        ]

    if not all_codes:
        return 0

    logger.info("全量净值下载: %d 只基金, 并发 %d", len(all_codes), concurrency)

    url_template = "https://fundgz.1234567.com.cn/js/{code}.js"
    batch_size = 200
    total_new = 0
    total_done = 0
    start_time = time.monotonic()

    async def _fetch_one(code: str) -> tuple[str, list[dict], bool]:
        nonlocal total_new, total_done
        try:
            url = url_template.replace("{code}", code)
            resp = await fetch_async(
                session, url, timeout=15,
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
            m = re.search(r"Data_netWorthTrend\s*=\s*(\[.+?\]);", text, re.DOTALL)
            if not m:
                return code, [], True
            trend = json.loads(m.group(1))
            navs = parse_pingzhong_navs(trend)
            return code, navs, False
        except Exception as e:
            logger.warning("基金 %s 全量净值拉取失败: %s", code, str(e)[:120], exc_info=True)
            return code, [], True

    semaphore = asyncio.Semaphore(concurrency)
    connector = aiohttp.TCPConnector(limit=concurrency, ttl_dns_cache=300, enable_cleanup_closed=True)

    async with aiohttp.ClientSession(connector=connector) as session:
        for i in range(0, len(all_codes), batch_size):
            batch = all_codes[i: i + batch_size]
            coros = [_fetch_one(code) for code in batch]
            results = await asyncio.gather(*coros)

            batch_new = 0
            with db_conn() as conn:
                for code, navs, failed in results:
                    if failed:
                        continue
                    n = save_nav_batch(conn, code, navs)
                    batch_new += n
                    total_new += n
            total_done += len(batch)
            elapsed = time.monotonic() - start_time
            speed = total_done / elapsed if elapsed > 0 else 0
            logger.info(
                "特征计算进度: %d/%d, speed=%.1f/s",
                total_done, len(all_codes), speed,
            )

        elapsed = time.monotonic() - start_time
        logger.info(
            "全量净值更新完成: %d 条, 耗时 %.1f 秒", total_new, elapsed,
        )

    return total_new


def fetch_fund_nav(code: str, settings: dict | None = None) -> list[dict]:
    """从 pingzhongdata 拉取单只基金历史净值（累计净值）。

    返回格式：[{"date": "2024-01-02", "cum_nav": 1.2345}, ...]
    """
    url = "http://fund.eastmoney.com/pingzhongdata/{code}.js".format(code=code)
    resp = fetch(url)
    text = resp.text

    # 提取 ACWorthTrend（累计净值序列）
    m = re.search(r"ACWorthTrend\s*=\s*(\[.*?\]);", text, re.DOTALL)
    if not m:
        logger.warning("基金 %s 未找到 ACWorthTrend", code)
        return []

    series = json.loads(m.group(1))
    nav_list = []
    for item in series:
        if len(item) < 2:
            continue
        ts_ms, cum_nav = item[0], item[1]
        if cum_nav is None:
            continue
        date_str = datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d")
        nav_list.append({"date": date_str, "cum_nav": cum_nav})

    return nav_list


def fetch_fund_nav_incremental(code: str, conn: sqlite3.Connection, settings: dict | None = None) -> int:
    """增量拉取单只基金净值，仅补充缺失数据。返回新增条数。"""
    cur = conn.execute("SELECT MAX(date) FROM fund_nav WHERE code = ?", (code,))
    row = cur.fetchone()
    local_max = row[0] if row and row[0] else None

    all_nav = fetch_fund_nav(code, settings)
    if not all_nav:
        return 0

    # 过滤：仅保留本地缺失的日期
    if local_max:
        new_nav = [n for n in all_nav if n["date"] > local_max]
    else:
        new_nav = all_nav

    if not new_nav:
        return 0

    conn.executemany(
        "INSERT OR IGNORE INTO fund_nav (code, date, cum_nav) VALUES (?, ?, ?)",
        [(code, n["date"], n["cum_nav"]) for n in new_nav],
    )
    return len(new_nav)

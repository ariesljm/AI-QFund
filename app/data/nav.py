"""净值数据抓取：全量下载 & 增量更新。"""

import asyncio
import json
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.database import db_conn
from app.data.fetchers import fetch, fetch_async
from app.data.store import save_nav_batch, backfill_guard, NAV_RETENTION_DAYS
from app.utils.log import get_logger

try:
    import aiohttp
except ImportError:
    aiohttp = None

logger = get_logger("nav")

_CHINA_TZ = timezone(timedelta(hours=8))
"""东八区时区：pingzhongdata 时间戳为中国时间 0 点，避免容器 UTC 下日期偏移一天。"""


_LSJZ_PAGE_SIZE = 100
"""lsjz 接口单页最大返回条数（实测固定 20 条，pageSize>=500 会直接返回空）。"""
_LSJZ_MAX_PAGES = 400


def _parse_lsjz_page(text: str) -> tuple[list[dict], int]:
    """解析单页 lsjz 响应 → (navs, total_count)，兼容 jQuery 包裹与裸 JSON。"""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return [], 0
    try:
        data = json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        return [], 0
    records = (data.get("Data") or {}).get("LSJZList") or []
    total = int(data.get("TotalCount") or 0)
    navs = []
    for r2 in records:
        cum_nav_str = r2.get("LJJZ", "")
        if not cum_nav_str:
            continue
        navs.append({
            "date": r2["FSRQ"],
            "cum_nav": float(cum_nav_str),
        })
    return navs, total


async def _lsjz_fetch_all(session, code: str, start_date: str,
                          headers: dict, timeout: float = 15) -> list[dict]:
    """分页拉取 lsjz 历史净值（pageSize 太大接口会返回空，必须逐页翻取）。

    全量时返回完整历史序列；带 start_date 时仅返回其后的净值。
    """
    all_navs = []
    page_index = 1
    while page_index <= _LSJZ_MAX_PAGES:
        url = (
            "https://api.fund.eastmoney.com/f10/lsjz?"
            f"callback=jQuery&fundCode={code}&pageIndex={page_index}"
            f"&pageSize={_LSJZ_PAGE_SIZE}"
        )
        if start_date:
            url += f"&startDate={start_date}"
        resp = await fetch_async(session, url, timeout=timeout, headers=headers)
        text = await resp.text()
        navs, total = _parse_lsjz_page(text)
        all_navs.extend(navs)
        if not navs or len(all_navs) >= total:
            break
        page_index += 1
    return all_navs


def _parse_pingzhong_acworth(text: str) -> list[dict]:
    """解析 pingzhongdata 的 ACWorthTrend（累计净值序列）→ [{"date", "cum_nav"}, ...]。"""
    m = re.search(r"ACWorthTrend\s*=\s*(\[.*?\]);", text, re.DOTALL)
    if not m:
        return []
    try:
        series = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return []
    navs = []
    for item in series:
        if len(item) < 2:
            continue
        ts_ms, cum_nav = item[0], item[1]
        if cum_nav is None:
            continue
        date_str = datetime.fromtimestamp(ts_ms / 1000, tz=_CHINA_TZ).strftime("%Y-%m-%d")
        navs.append({"date": date_str, "cum_nav": float(cum_nav)})
    return navs


async def _pingzhong_fetch_all_async(session, code: str,
                                     headers: dict, timeout: float = 20) -> list[dict]:
    """异步抓取 pingzhongdata 完整历史净值（累计净值），单请求返回全量序列。

    用于全量首次下载，以及增量更新中无本地数据（全量兜底）的基金——
    lsjz 单页最多 20 条，全量逐页翻取需要数百次请求，pingzhongdata 一次即可。
    """
    url = f"https://fund.eastmoney.com/pingzhongdata/{code}.js"
    resp = await fetch_async(session, url, timeout=timeout, headers=headers)
    text = await resp.text()
    return _parse_pingzhong_acworth(text)


async def _probe_lsjz_latest(session, headers: dict) -> str | None:
    """探测 lsjz 接口当前可返回的最新净值日期（用活跃基金 000001 的首页）。"""
    try:
        url = (
            "https://api.fund.eastmoney.com/f10/lsjz?"
            f"callback=jQuery&fundCode=000001&pageIndex=1&pageSize={_LSJZ_PAGE_SIZE}"
        )
        resp = await fetch_async(session, url, timeout=15, headers=headers)
        text = await resp.text()
        navs, _ = _parse_lsjz_page(text)
        return navs[0]["date"] if navs else None
    except Exception:
        return None


async def async_update_nav_incremental(concurrency: int = 5) -> int:
    with db_conn() as conn:
        all_codes = [
            r[0] for r in conn.execute("SELECT code FROM fund_basic WHERE is_buyable = 1").fetchall()
        ]
        local_max = dict(
            conn.execute("SELECT code, MAX(date) FROM fund_nav GROUP BY code").fetchall()
        )
        global_latest = conn.execute("SELECT MAX(date) FROM fund_nav").fetchone()[0]
        before_global_max = global_latest

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
        try:
            if start_date:
                navs = await _lsjz_fetch_all(session, code, start_date, headers)
            else:
                # 无本地数据（新基金/H类份额等）：lsjz 全量需逐页数百次请求，改用 pingzhongdata 一次拿全
                navs = (await _pingzhong_fetch_all_async(session, code, headers))[-NAV_RETENTION_DAYS:]
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
                        navs = fetch_fund_nav(code)
                        if navs:
                            with db_conn() as conn:
                                save_nav_batch(conn, code, navs)
                    except Exception as e2:
                        logger.debug("补查 %s 失败: %s", code, str(e2)[:80])

        ok_cnt = len(tasks_meta) - len(all_failed)
        logger.info("净值增量更新完成: 新增 %d 条, 成功 %d/%d 只, 失败 %d 只",
                    total_new, ok_cnt, len(tasks_meta), len(all_failed))
        if total_new == 0 and ok_cnt > 100:
            # 探测接口最新净值日期：若比本地最新还新却没写入，才是真异常；
            # 周末/停更基金导致的 0 条属正常，不应告警。
            probe_date = await _probe_lsjz_latest(session, headers)
            if probe_date and probe_date > before_global_max:
                logger.error(
                    "净值增量更新写入 0 条但接口已可返回 %s（本地最新 %s）——"
                    "疑似净值接口失效或请求被拒，请检查后重跑，否则特征/推荐将基于陈旧净值",
                    probe_date, before_global_max,
                )
            else:
                logger.info("净值增量更新 0 条: 接口最新 %s == 本地最新 %s，无新增数据（正常）",
                            probe_date, before_global_max)

    return total_new


async def async_download_all_nav(concurrency: int = 50) -> int:
    with db_conn() as conn:
        all_codes = [
            r[0] for r in conn.execute("SELECT code FROM fund_basic WHERE is_buyable = 1").fetchall()
        ]

    if not all_codes:
        return 0

    logger.info("全量净值下载: %d 只基金, 并发 %d", len(all_codes), concurrency)

    # 注意：不能用 fundgz.1234567.com.cn/js/{code}.js（实时估值接口，无历史序列）。
    # lsjz 历史净值接口单页硬上限 20 条且大 pageSize 返回空，全量逐页翻取太慢，
    # 全量首次下载改用 pingzhongdata（单请求返回完整 ACWorthTrend 历史序列）；
    # 增量日常更新仍走 lsjz（startDate 后通常只有 1-2 页，见 _lsjz_fetch_all）。
    batch_size = 200
    total_new = 0
    total_done = 0
    start_time = time.monotonic()

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://fund.eastmoney.com/",
    }

    async def _fetch_one(code: str) -> tuple[str, list[dict], bool]:
        try:
            navs = await _pingzhong_fetch_all_async(session, code, headers)
            return code, navs[-NAV_RETENTION_DAYS:], False
        except Exception as e:
            logger.warning("基金 %s 全量净值拉取失败: %s", code, str(e)[:120], exc_info=True)
            return code, [], True

    semaphore = asyncio.Semaphore(concurrency)
    connector = aiohttp.TCPConnector(limit=concurrency, ttl_dns_cache=300, enable_cleanup_closed=True)

    async with aiohttp.ClientSession(connector=connector) as session:
        all_failed: list[str] = []
        for i in range(0, len(all_codes), batch_size):
            batch = all_codes[i: i + batch_size]
            coros = [_fetch_one(code) for code in batch]
            results = await asyncio.gather(*coros)

            batch_new = 0
            with db_conn() as conn:
                for code, navs, failed in results:
                    if failed:
                        all_failed.append(code)
                        continue
                    n = save_nav_batch(conn, code, navs)
                    batch_new += n
                    total_new += n
            total_done += len(batch)
            elapsed = time.monotonic() - start_time
            speed = total_done / elapsed if elapsed > 0 else 0
            logger.info(
                "全量净值下载进度: %d/%d, speed=%.1f/s",
                total_done, len(all_codes), speed,
            )

        if all_failed:
            if backfill_guard(all_failed, len(all_codes), "全量净值"):
                for code in all_failed:
                    try:
                        navs = fetch_fund_nav(code)
                        if navs:
                            with db_conn() as conn:
                                save_nav_batch(conn, code, navs)
                    except Exception as e2:
                        logger.debug("全量补查 %s 失败: %s", code, str(e2)[:80])

        elapsed = time.monotonic() - start_time
        logger.info(
            "全量净值更新完成: 新增 %d 条, 失败 %d/%d 只, 耗时 %.1f 秒",
            total_new, len(all_failed), len(all_codes), elapsed,
        )
        if total_new == 0 and len(all_codes) > 100:
            logger.error(
                "全量净值下载写入 0 条但任务数 %d——疑似净值接口失效，请检查",
                len(all_codes),
            )

    return total_new


def prune_nav_history(retention: int = NAV_RETENTION_DAYS) -> int:
    """删除每只基金超出保留窗口的旧净值（存量一次性清理），返回删除行数。

    新写入路径由 save_nav_batch 自动修剪，本函数用于清理历史遗留的存量数据。
    """
    with db_conn() as conn:
        cur = conn.execute(
            "DELETE FROM fund_nav WHERE rowid IN ("
            "  SELECT rowid FROM ("
            "    SELECT rowid, ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) rk"
            "    FROM fund_nav) WHERE rk > ?)",
            (retention,),
        )
        return cur.rowcount


def fetch_fund_nav(code: str, settings: dict | None = None) -> list[dict]:
    """从 pingzhongdata 拉取单只基金历史净值（累计净值）。

    返回格式：[{"date": "2024-01-02", "cum_nav": 1.2345}, ...]
    """
    url = "https://fund.eastmoney.com/pingzhongdata/{code}.js".format(code=code)
    resp = fetch(url)
    nav_list = _parse_pingzhong_acworth(resp.text)
    if not nav_list:
        # 已终止的后端份额等基金在天天基金已无净值页（404），属常态，降为 debug 避免刷屏
        logger.debug("基金 %s 未找到 ACWorthTrend", code)
    return nav_list


def fetch_fund_nav_incremental(code: str, conn: sqlite3.Connection, settings: dict | None = None) -> int:
    """增量拉取单只基金净值（走 save_nav_batch 统一过滤+修剪），返回新增条数。"""
    navs = fetch_fund_nav(code, settings)
    if not navs:
        return 0
    return save_nav_batch(conn, code, navs)

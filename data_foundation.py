"""Phase 1 数据基座：基金列表、净值、指数、特征计算。

运行方式：
    uv run python data_foundation.py          # 全流程
    uv run python data_foundation.py --step 1  # 仅执行某步骤
"""

import asyncio
import json
import logging
import re
import sqlite3
import sys
import time
from ast import literal_eval
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import requests

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DB_PATH = Path("data/qfund.db")
SETTINGS_PATH = Path("config/settings.toml")


# ========== 工具函数 ==========

def _load_settings():
    import tomllib
    with open(SETTINGS_PATH, "rb") as f:
        return tomllib.load(f)


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    """读取 meta 表中的值；表不存在时返回 None。"""
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    """写入 meta 表中的键值。"""
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
    conn.commit()


def _fetch(url: str, params: dict | None = None, timeout: float = 15) -> requests.Response:
    """发起 GET 请求，绕过系统代理。"""
    s = requests.Session()
    s.trust_env = False
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://fund.eastmoney.com/data/fundranking.html",
    }
    resp = s.get(url, params=params, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp


# ========== 1.1 基金列表获取与过滤 ==========

# 需要剔除的基金类型关键词
_EXCLUDE_KEYWORDS = ["货币", "债券", "封闭", "偏债", "QDII", "FOF", "理财"]


def fetch_fund_list(settings: dict | None = None) -> list[dict]:
    """获取并过滤基金列表，剔除不可投类型。

    返回格式：[{"code": "000001", "name": "华夏成长", "type": "混合型", "is_buyable": 1}, ...]
    """
    if settings is None:
        settings = _load_settings()
    api = settings["api"]
    url = api["fund_list_url"]

    all_funds: list[dict] = []
    # 按类型拉取：gp(股票), hh(混合), zs(指数), qdii, fof
    # 只取股票型、混合型、指数型
    type_map = {"gp": "股票型", "hh": "混合型", "zs": "指数型"}

    for ft, type_label in type_map.items():
        page = 1
        while True:
            params = {
                "op": "ph",
                "dt": "kf",
                "ft": ft,
                "rs": "",
                "gs": "0",
                "sc": "6yzf",
                "st": "desc",
                "sd": "2020-01-01",
                "ed": datetime.now().strftime("%Y-%m-%d"),
                "qdii": "",
                "tabSubtype": ",,,,,",
                "pi": page,
                "pn": 200,
                "dx": "1",
            }
            resp = _fetch(url, params)
            text = resp.text

            # 解析 JS 赋值：var rankData = {datas:["...","..."],...};
            m = re.search(r"var rankData\s*=\s*(\{.*\});", text, re.DOTALL)
            if not m:
                logger.warning("类型 %s 第 %d 页解析失败", ft, page)
                break

            obj_str = re.sub(r"([A-Za-z_]\w*)\s*:", r'"\1":', m.group(1))
            obj = literal_eval(obj_str)
            rows = obj.get("datas") or []
            if not rows:
                break

            for row_str in rows:
                fields = row_str.split("|")
                if len(fields) < 4:
                    continue
                code = fields[0].strip()
                name = fields[1].strip()
                # 过滤不可投类型
                if any(kw in name for kw in _EXCLUDE_KEYWORDS):
                    continue
                all_funds.append({
                    "code": code,
                    "name": name,
                    "type": type_label,
                    "is_buyable": 1,
                })

            all_pages = obj.get("allPages", 1)
            if page >= all_pages:
                break
            page += 1
            time.sleep(0.3)  # 避免请求过快

        logger.info("类型 %s 拉取完成，累计 %d 条", ft, len(all_funds))

    return all_funds


def save_fund_list(funds: list[dict]) -> int:
    """将基金列表写入 fund_basic 表，返回写入条数。"""
    conn = _get_db()
    count = 0
    for f in funds:
        conn.execute(
            "INSERT OR REPLACE INTO fund_basic (code, name, type, is_buyable) VALUES (?, ?, ?, ?)",
            (f["code"], f["name"], f["type"], f["is_buyable"]),
        )
        count += 1
    conn.commit()
    conn.close()
    return count


_LIST_UPDATE_INTERVAL_DAYS = 7


def update_fund_list_weekly(settings: dict | None = None, force: bool = False) -> int:
    """按周更新基金列表：距上次更新不足 7 天则跳过。

    返回本次实际写入的基金条数；跳过时返回 -1。
    """
    conn = _get_db()
    last = _meta_get(conn, "fund_list_last_update")
    if last and not force:
        last_dt = datetime.strptime(last, "%Y-%m-%d")
        age_days = (datetime.now() - last_dt).days
        if age_days < _LIST_UPDATE_INTERVAL_DAYS:
            logger.info("基金列表 %d 天前更新过（<%d 天），跳过",
                        age_days, _LIST_UPDATE_INTERVAL_DAYS)
            conn.close()
            return -1
    conn.close()

    funds = fetch_fund_list(settings)
    n = save_fund_list(funds)
    conn = _get_db()
    _meta_set(conn, "fund_list_last_update", datetime.now().strftime("%Y-%m-%d"))
    conn.close()
    logger.info("基金列表更新完成，写入 %d 条", n)
    return n


# ========== 1.2 净值增量更新 ==========

def fetch_fund_nav(code: str, settings: dict | None = None) -> list[dict]:
    """从 pingzhongdata 拉取单只基金历史净值（累计净值）。

    返回格式：[{"date": "2024-01-02", "cum_nav": 1.2345}, ...]
    """
    if settings is None:
        settings = _load_settings()
    api = settings["api"]
    url = api["pingzhongdata_url"].format(code=code)
    resp = _fetch(url)
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
    # 查询本地最新日期
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


# ========== 1.2b 异步并发净值下载 ==========

_HEADERS_ASYNC = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://fund.eastmoney.com/data/fundranking.html",
}


def _parse_nav_response(text: str, code: str) -> list[dict]:
    """从 pingzhongdata 响应文本中解析净值序列。"""
    m = re.search(r"ACWorthTrend\s*=\s*(\[.*?\]);", text, re.DOTALL)
    if not m:
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


async def _async_fetch_one(
    session: "aiohttp.ClientSession",
    code: str,
    url_template: str,
    semaphore: asyncio.Semaphore,
) -> tuple[str, list[dict]]:
    """异步拉取单只基金净值。"""
    url = url_template.format(code=code)
    async with semaphore:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                text = await resp.text()
                navs = _parse_nav_response(text, code)
                return code, navs
        except Exception as e:
            logger.debug("基金 %s 异步拉取失败: %s", code, e)
            return code, []


async def _async_batch_fetch(
    codes: list[str],
    url_template: str,
    concurrency: int = 20,
) -> dict[str, list[dict]]:
    """并发拉取一批基金净值，返回 {code: [nav_list]}。"""
    semaphore = asyncio.Semaphore(concurrency)
    headers = _HEADERS_ASYNC.copy()
    connector = aiohttp.TCPConnector(limit=concurrency, force_close=True)

    async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
        tasks = [_async_fetch_one(session, c, url_template, semaphore) for c in codes]
        results = await asyncio.gather(*tasks)

    return {code: navs for code, navs in results}


def _save_nav_batch(conn: sqlite3.Connection, code: str, navs: list[dict]) -> int:
    """将一批净值写入数据库（增量）。"""
    if not navs:
        return 0
    cur = conn.execute("SELECT MAX(date) FROM fund_nav WHERE code = ?", (code,))
    row = cur.fetchone()
    local_max = row[0] if row and row[0] else None
    if local_max:
        navs = [n for n in navs if n["date"] > local_max]
    if not navs:
        return 0
    conn.executemany(
        "INSERT OR IGNORE INTO fund_nav (code, date, cum_nav) VALUES (?, ?, ?)",
        [(code, n["date"], n["cum_nav"]) for n in navs],
    )
    # 分红入库（仅现金分红，lsjz FHSP 字段解析所得）
    dividends = [
        (code, n["date"], n["dividend"])
        for n in navs
        if n.get("dividend") is not None
    ]
    if dividends:
        conn.executemany(
            "INSERT OR IGNORE INTO fund_dividend (code, date, dividend_per_unit) "
            "VALUES (?, ?, ?)",
            dividends,
        )
    return len(navs)


# ========== 1.2c 真·增量：基于 lsjz 分页接口 ==========

_LSJZ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://fundf10.eastmoney.com/",
}


_DIVIDEND_RE = re.compile(r"每份派现金\s*([\d.]+)\s*元")


def _parse_fhsp_dividend(fhsp: str | None) -> float | None:
    """从 lsjz 的 FHSP 字段提取每份派现金额（元）。

    FHSP 形如 "每份派现金0.0500元"（现金分红）或 "每份基金份额折算..."（拆分），
    仅提取现金分红金额，其余返回 None。
    """
    if not fhsp:
        return None
    m = _DIVIDEND_RE.search(fhsp)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _parse_lsjz_list(lsjz_list: list[dict]) -> list[dict]:
    """解析 lsjz 接口返回的净值列表为标准格式（含分红）。"""
    nav_list = []
    for x in lsjz_list:
        cum = x.get("LJJZ")
        date = x.get("FSRQ")
        if not date or cum in (None, ""):
            continue
        try:
            cum_nav = float(cum)
        except (ValueError, TypeError):
            continue
        nav_list.append({
            "date": date,
            "cum_nav": cum_nav,
            "dividend": _parse_fhsp_dividend(x.get("FHSP")),
        })
    return nav_list


async def _async_fetch_lsjz(
    session: "aiohttp.ClientSession",
    code: str,
    lsjz_url: str,
    start_date: str,
    end_date: str,
    semaphore: asyncio.Semaphore,
) -> tuple[str, list[dict]]:
    """异步拉取单只基金指定日期范围的净值（lsjz 接口）。

    - start_date 非空：增量模式，仅取该日期之后的新数据，单页即可。
    - start_date 为空：兜底全量模式，自动翻页拉完整历史（新基金/漏拉基金）。
    """
    page_size = 60
    all_navs: list[dict] = []
    page = 1
    async with semaphore:
        while True:
            params = {
                "fundCode": code,
                "pageIndex": page,
                "pageSize": page_size,
                "startDate": start_date,
                "endDate": end_date,
            }
            try:
                async with session.get(
                    lsjz_url, params=params, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    data = await resp.json(content_type=None)
                    lsjz_list = (data.get("Data") or {}).get("LSJZList") or []
                    total = data.get("TotalCount") or 0
            except Exception as e:
                logger.debug("基金 %s lsjz 拉取失败(第%d页): %s", code, page, e)
                break

            all_navs.extend(_parse_lsjz_list(lsjz_list))

            # 增量模式（start_date 非空）：单页即够，不翻页
            if start_date:
                break
            # 全量模式：翻到取完为止
            if page * page_size >= total or not lsjz_list:
                break
            page += 1

    return code, all_navs


async def async_update_nav_incremental(
    concurrency: int = 20,
    batch_size: int = 200,
) -> int:
    """真·增量更新：每只基金仅拉取本地最新日期之后的净值。

    基于 lsjz 分页接口的日期范围过滤，每天运行仅下载缺失的 1-2 天，
    相比 pingzhongdata 全量重拉，网络传输量降低两个数量级。

    Returns:
        总新增条数
    """
    if not HAS_AIOHTTP:
        raise RuntimeError("需要安装 aiohttp: uv add aiohttp")

    settings = _load_settings()
    lsjz_url = settings["api"]["lsjz_url"]
    end_date = datetime.now().strftime("%Y-%m-%d")

    conn = _get_db()
    all_codes = [
        r[0] for r in conn.execute("SELECT code FROM fund_basic WHERE is_buyable = 1").fetchall()
    ]
    local_max = dict(
        conn.execute("SELECT code, MAX(date) FROM fund_nav GROUP BY code").fetchall()
    )

    # 以全局最新日期作为最新交易日基准，跳过已最新的基金
    global_latest = conn.execute("SELECT MAX(date) FROM fund_nav").fetchone()[0]

    # 构造待更新任务：(code, start_date)
    # - 有本地数据 → start_date=本地最新，走单页增量
    # - 无本地数据 → start_date=""，走翻页全量兜底（新基金/首次漏拉）
    tasks_meta: list[tuple[str, str]] = []
    incr_cnt = 0
    full_cnt = 0
    for code in all_codes:
        lm = local_max.get(code)
        if lm == global_latest:
            continue  # 已最新，跳过
        if lm:
            tasks_meta.append((code, lm))  # 增量
            incr_cnt += 1
        else:
            tasks_meta.append((code, ""))  # 全量兜底
            full_cnt += 1

    logger.info(
        "真·增量：跳过已最新 %d 只，增量 %d 只，全量兜底 %d 只(无本地数据)",
        len(all_codes) - len(tasks_meta), incr_cnt, full_cnt,
    )

    if not tasks_meta:
        logger.info("无需增量更新")
        conn.close()
        return 0

    semaphore = asyncio.Semaphore(concurrency)
    connector = aiohttp.TCPConnector(limit=concurrency, force_close=True)
    total_new = 0
    total_done = 0
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
            for code, navs in results:
                batch_new += _save_nav_batch(conn, code, navs)
                total_done += 1
            conn.commit()
            total_new += batch_new

            elapsed = time.monotonic() - start_time
            speed = total_done / elapsed if elapsed > 0 else 0
            logger.info(
                "进度 %d/%d (+%d), 速度 %.1f/s",
                total_done, len(tasks_meta), batch_new, speed,
            )

    conn.close()
    elapsed = time.monotonic() - start_time
    logger.info("增量更新完成: %d 条净值, 耗时 %.1f 秒", total_new, elapsed)
    return total_new


async def async_download_all_nav(
    concurrency: int = 20,
    batch_size: int = 200,
    force_full: bool = False,
) -> int:
    """异步并发全量下载所有基金净值。

    增量对齐策略：pingzhongdata 只能返回完整历史，无法请求增量。
    因此优化点在于「跳过已最新的基金」——若某基金本地最新日期已达到
    全局最新交易日，则完全跳过下载（不发起网络请求）。

    Args:
        concurrency: 并发数（建议 10-30）
        batch_size: 每批请求数（控制内存）
        force_full: 强制全量下载，忽略跳过逻辑（首次或数据修复时用）

    Returns:
        总新增条数
    """
    if not HAS_AIOHTTP:
        raise RuntimeError("需要安装 aiohttp: uv add aiohttp")

    settings = _load_settings()
    url_template = settings["api"]["pingzhongdata_url"]

    conn = _get_db()
    cur = conn.execute("SELECT code FROM fund_basic WHERE is_buyable = 1")
    all_codes = [r[0] for r in cur.fetchall()]

    # 增量对齐：跳过本地已是最新交易日的基金
    if not force_full:
        # 以全局最新净值日期作为「最新交易日」基准
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
        conn.close()
        return 0

    total_new = 0
    total_done = 0
    start_time = time.monotonic()

    # 分批下载
    for i in range(0, len(all_codes), batch_size):
        batch = all_codes[i : i + batch_size]
        results = await _async_batch_fetch(batch, url_template, concurrency)

        batch_new = 0
        for code, navs in results.items():
            n = _save_nav_batch(conn, code, navs)
            batch_new += n
            total_done += 1

        conn.commit()
        total_new += batch_new

        elapsed = time.monotonic() - start_time
        speed = total_done / elapsed if elapsed > 0 else 0
        eta = (len(all_codes) - total_done) / speed if speed > 0 else 0
        logger.info(
            "进度 %d/%d (+%d), 速度 %.1f/s, ETA %.0fs",
            total_done, len(all_codes), batch_new, speed, eta,
        )

    conn.close()
    elapsed = time.monotonic() - start_time
    logger.info("全量下载完成: %d 条净值, 耗时 %.1f 秒", total_new, elapsed)
    return total_new


# ========== 1.3 宏观指数获取 ==========

def fetch_index_daily(settings: dict | None = None, datalen: int = 250) -> list[dict]:
    """获取沪深300指数日线数据（新浪 K 线接口）。

    datalen: 拉取的交易日条数。冷启动默认 250 条（约 1 年），
    足够 EMA60 收敛；增量场景由调用方传入较小值。
    """
    if settings is None:
        settings = _load_settings()
    api = settings["api"]
    url = api["index_url"]
    params = {
        "symbol": api["hs300_symbol"],
        "scale": 240,  # 日线
        "ma": 60,
        "datalen": datalen,
    }
    resp = _fetch(url, params)
    klines = resp.json()

    result = []
    for k in klines:
        result.append({
            "date": k["day"],
            "open": float(k["open"]),
            "high": float(k["high"]),
            "low": float(k["low"]),
            "close": float(k["close"]),
            "volume": float(k["volume"]),
        })
    return result


_EMA_PERIOD = 60
_EMA_K = 2 / (_EMA_PERIOD + 1)


def save_index_daily(index_code: str, data: list[dict]) -> int:
    """将指数日线数据写入 index_daily 表，计算 EMA60（字段沿用 ma60 列）。

    EMA 是递推序列：EMA_today = close*k + EMA_yesterday*(1-k)。
    - 增量场景：若本地已有 EMA 值，以本地最后一条为种子直接续算，
      只写入本地缺失的新交易日。
    - 冷启动场景：本地无 EMA 时，前 60 条用 SMA 作种子，之后递推。
    """
    conn = _get_db()

    # 读取本地已有的最新一条（作为增量递推种子）
    row = conn.execute(
        "SELECT date, ma60 FROM index_daily WHERE code = ? ORDER BY date DESC LIMIT 1",
        (index_code,),
    ).fetchone()
    local_max_date = row[0] if row else None
    prev_ema = row[1] if row and row[1] is not None else None

    # 仅保留本地缺失的日期，按时间升序
    data_sorted = sorted(data, key=lambda d: d["date"])
    if local_max_date:
        new_data = [d for d in data_sorted if d["date"] > local_max_date]
    else:
        new_data = data_sorted

    if not new_data:
        conn.close()
        return 0

    # 冷启动收盘价缓存：本地已有的收盘价 + 逐步追加的新收盘价，
    # 用于在 prev_ema 尚未建立时累计到 60 条求 SMA 种子
    seed_closes = [
        r[0] for r in conn.execute(
            "SELECT close FROM index_daily WHERE code = ? ORDER BY date ASC",
            (index_code,),
        ).fetchall()
    ]

    written = 0
    for d in new_data:
        close = d["close"]
        if prev_ema is not None:
            # 已有种子，直接递推
            ema = close * _EMA_K + prev_ema * (1 - _EMA_K)
        else:
            # 冷启动：累计到满 60 条时用 SMA 作首个 EMA，之前记 NULL
            seed_closes.append(close)
            if len(seed_closes) >= _EMA_PERIOD:
                ema = sum(seed_closes[-_EMA_PERIOD:]) / _EMA_PERIOD
            else:
                ema = None

        conn.execute(
            "INSERT OR REPLACE INTO index_daily (code, date, open, high, low, close, volume, ma60) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (index_code, d["date"], d["open"], d["high"], d["low"], d["close"], d["volume"], ema),
        )
        if ema is not None:
            prev_ema = ema
        written += 1

    conn.commit()
    conn.close()
    return written


# ========== 1.4 大盘状态机 ==========

def calc_regime(conn: sqlite3.Connection, index_code: str = "sh000300") -> str:
    """返回 "BULL" 或 "BEAR"，基于最新收盘价与 EMA60 的关系（ma60 列存 EMA 值）。"""
    cur = conn.execute(
        "SELECT close, ma60 FROM index_daily WHERE code = ? ORDER BY date DESC LIMIT 1",
        (index_code,),
    )
    row = cur.fetchone()
    if not row or row[1] is None:
        logger.warning("无法计算 regime，EMA60 数据不足")
        return "NEUTRAL"

    close, ema60 = row
    return "BULL" if close > ema60 else "BEAR"


# ========== 1.5 重仓股数据获取 ==========

# f10 持仓 HTML 解析：报告期 + 每行 [代码, 名称, 占净值比例]
_HOLDING_DATE_RE = re.compile(r"截止至：<font[^>]*>([\d-]+)</font>")
_HOLDING_ROW_RE = re.compile(
    r"<td>\d+</td>"                                      # 序号
    r"<td><a[^>]*>(\d+)</a></td>"                        # 股票代码
    r"<td class='tol'><a[^>]*>([^<]+)</a></td>"          # 股票名称
    r".*?<td class='tor'>([\d.]+)%</td>",               # 占净值比例
    re.DOTALL,
)


def _parse_holdings_html(text: str) -> tuple[str | None, list[dict]]:
    """解析 f10 jjcc 接口返回的 HTML，返回 (报告期, 持仓列表)。

    HTML 可能含多个季度块，仅取第一个（最新季报）的报告期与明细。
    """
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


def fetch_holdings(code: str, settings: dict | None = None) -> tuple[str | None, list[dict]]:
    """从天天基金 f10 拉取单只基金最新季报重仓股。

    返回 (报告期, [{"stock_code","stock_name","weight"}, ...])。
    """
    if settings is None:
        settings = _load_settings()
    api = settings["api"]
    params = {"type": "jjcc", "code": code, "topline": "10", "year": "", "month": ""}
    resp = _fetch(api["holdings_url"], params)
    return _parse_holdings_html(resp.text)


def save_holdings(code: str, holdings: list[dict], report_date: str) -> int:
    """将持仓数据写入 fund_holdings 表。"""
    conn = _get_db()
    conn.executemany(
        "INSERT OR REPLACE INTO fund_holdings (code, report_date, stock_code, stock_name, weight) "
        "VALUES (?, ?, ?, ?, ?)",
        [(code, report_date, h["stock_code"], h["stock_name"], h["weight"]) for h in holdings],
    )
    conn.commit()
    conn.close()
    return len(holdings)


_HOLDINGS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://fundf10.eastmoney.com/",
}


async def _async_fetch_holdings_one(
    session: "aiohttp.ClientSession",
    code: str,
    holdings_url: str,
    semaphore: asyncio.Semaphore,
) -> tuple[str, str | None, list[dict]]:
    """异步拉取单只基金最新季报持仓（f10 jjcc 接口）。"""
    params = {"type": "jjcc", "code": code, "topline": "10", "year": "", "month": ""}
    async with semaphore:
        try:
            async with session.get(
                holdings_url, params=params, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                text = await resp.text()
                report_date, holdings = _parse_holdings_html(text)
                return code, report_date, holdings
        except Exception as e:
            logger.debug("基金 %s 持仓异步拉取失败: %s", code, e)
            return code, None, []


async def async_download_all_holdings(
    concurrency: int = 20,
    batch_size: int = 200,
) -> int:
    """异步并发全量下载所有可投基金最新季报重仓股（f10 jjcc 接口）。

    report_date 取自接口返回的季报截止日期（如 2026-03-31），
    保证持仓快照可按季报期唯一标识。

    Returns:
        总写入持仓条数
    """
    if not HAS_AIOHTTP:
        raise RuntimeError("需要安装 aiohttp: uv add aiohttp")

    settings = _load_settings()
    holdings_url = settings["api"]["holdings_url"]

    conn = _get_db()
    all_codes = [
        r[0] for r in conn.execute(
            "SELECT code FROM fund_basic WHERE is_buyable = 1"
        ).fetchall()
    ]
    logger.info("待下载持仓基金: %d 只, 并发数: %d", len(all_codes), concurrency)

    semaphore = asyncio.Semaphore(concurrency)
    connector = aiohttp.TCPConnector(limit=concurrency, force_close=True)
    total_rows = 0
    total_done = 0
    funds_with_holdings = 0
    start_time = time.monotonic()

    async with aiohttp.ClientSession(headers=_HOLDINGS_HEADERS, connector=connector) as session:
        for i in range(0, len(all_codes), batch_size):
            batch = all_codes[i : i + batch_size]
            coros = [
                _async_fetch_holdings_one(session, c, holdings_url, semaphore)
                for c in batch
            ]
            results = await asyncio.gather(*coros)

            batch_rows = 0
            for code, report_date, holdings in results:
                if holdings and report_date:
                    conn.executemany(
                        "INSERT OR REPLACE INTO fund_holdings "
                        "(code, report_date, stock_code, stock_name, weight) "
                        "VALUES (?, ?, ?, ?, ?)",
                        [(code, report_date, h["stock_code"], h["stock_name"], h["weight"])
                         for h in holdings],
                    )
                    batch_rows += len(holdings)
                    funds_with_holdings += 1
                total_done += 1
            conn.commit()
            total_rows += batch_rows

            elapsed = time.monotonic() - start_time
            speed = total_done / elapsed if elapsed > 0 else 0
            eta = (len(all_codes) - total_done) / speed if speed > 0 else 0
            logger.info(
                "进度 %d/%d (+%d 条), 速度 %.1f/s, ETA %.0fs",
                total_done, len(all_codes), batch_rows, speed, eta,
            )

    conn.close()
    elapsed = time.monotonic() - start_time
    logger.info(
        "持仓下载完成: %d 只有持仓, 共 %d 条, 耗时 %.1f 秒",
        funds_with_holdings, total_rows, elapsed,
    )
    return total_rows


# ========== 1.6 RBSA 行业暴露（简化版）==========

# 申万一级行业映射（简化：用股票代码前缀粗略分类）
# 实际生产中应接入行业分类 API，此处用占位逻辑
_INDUSTRY_MAP = {
    "600": "金融", "601": "金融", "603": "制造",
    "000": "制造", "001": "制造", "002": "中小板",
    "300": "创业板", "688": "科创板",
}


def calc_rbsa(holdings: list[dict]) -> list[dict]:
    """计算 RBSA 行业暴露度（简化版：按股票代码前缀分类）。

    返回按权重降序排列的行业列表。
    """
    industry_weights: dict[str, float] = {}
    for h in holdings:
        stock_code = h["stock_code"]
        prefix = stock_code[:3]
        industry = _INDUSTRY_MAP.get(prefix, "其他")
        industry_weights[industry] = industry_weights.get(industry, 0) + h["weight"]

    # 按权重降序排列
    sorted_industries = sorted(industry_weights.items(), key=lambda x: x[1], reverse=True)
    return [{"industry": ind, "weight": w} for ind, w in sorted_industries[:3]]


# ========== 1.7 特征计算 ==========

def calc_hurst(series: np.ndarray, max_lag: int = 20) -> float:
    """计算 Hurst 指数（R/S 分析法）。"""
    if len(series) < max_lag + 10:
        return 0.5  # 默认随机游走

    lags = range(2, max_lag + 1)
    rs_values = []
    for lag in lags:
        # 分块计算 R/S
        n_blocks = len(series) // lag
        if n_blocks == 0:
            continue
        rs_list = []
        for i in range(n_blocks):
            block = series[i * lag : (i + 1) * lag]
            mean_block = np.mean(block)
            deviations = np.cumsum(block - mean_block)
            r = np.max(deviations) - np.min(deviations)
            s = np.std(block, ddof=1) if np.std(block, ddof=1) > 0 else 1e-10
            rs_list.append(r / s)
        if rs_list:
            rs_values.append((np.log(lag), np.log(np.mean(rs_list))))

    if len(rs_values) < 2:
        return 0.5

    # 线性回归求斜率（Hurst 指数）
    x = np.array([v[0] for v in rs_values])
    y = np.array([v[1] for v in rs_values])
    slope = np.polyfit(x, y, 1)[0]
    return float(np.clip(slope, 0, 1))


def calc_features(code: str, conn: sqlite3.Connection) -> dict:
    """全特征计算入口，返回单只基金当日特征字典。"""
    # 获取累计净值序列
    cur = conn.execute(
        "SELECT date, cum_nav FROM fund_nav WHERE code = ? ORDER BY date ASC",
        (code,),
    )
    rows = cur.fetchall()
    if len(rows) < 60:
        logger.warning("基金 %s 净值数据不足 (%d 天)，跳过特征计算", code, len(rows))
        return {}

    dates = [r[0] for r in rows]
    navs = np.array([r[1] for r in rows], dtype=float)

    # 日收益率（净值可能含 0 值导致除零，随后由 isfinite 过滤，局部抑制警告）
    with np.errstate(divide="ignore", invalid="ignore"):
        returns = np.diff(navs) / navs[:-1]
    returns = returns[np.isfinite(returns)]

    features: dict = {"code": code, "date": dates[-1]}

    # Hurst 指数（60日窗口）
    window = min(60, len(returns))
    features["hurst_60d"] = calc_hurst(returns[-window:])

    # 绝对动量（20日）
    if len(navs) >= 20:
        features["momentum_20d"] = float((navs[-1] / navs[-20] - 1) * 100)
    else:
        features["momentum_20d"] = 0.0

    # 卡玛比率（年化收益 / 最大回撤）
    if len(navs) >= 60:
        cum_returns = navs[-60:] / navs[-60]
        peak = np.maximum.accumulate(cum_returns)
        drawdown = (cum_returns - peak) / peak
        max_dd = float(np.min(drawdown))
        ann_return = float((navs[-1] / navs[-60] - 1) * 252 / 60)
        features["calmar"] = ann_return / abs(max_dd) if abs(max_dd) > 1e-10 else 0.0
    else:
        features["calmar"] = 0.0

    # 下行波动率（20日）
    if len(returns) >= 20:
        neg_returns = returns[-20:][returns[-20:] < 0]
        features["downside_vol"] = float(np.std(neg_returns) * np.sqrt(252)) if len(neg_returns) > 0 else 0.0
    else:
        features["downside_vol"] = 0.0

    # 向上/向下捕获率（相对沪深300）
    # 获取沪深300数据
    cur_idx = conn.execute(
        "SELECT date, close FROM index_daily WHERE code = 'sh000300' ORDER BY date ASC",
    )
    idx_rows = cur_idx.fetchall()
    if len(idx_rows) >= 60 and len(returns) >= 60:
        idx_dates = [r[0] for r in idx_rows]
        idx_closes = np.array([r[1] for r in idx_rows], dtype=float)
        idx_returns = np.diff(idx_closes) / idx_closes[:-1]
        idx_returns = idx_returns[np.isfinite(idx_returns)]

        # 对齐最近60日
        min_len = min(60, len(returns), len(idx_returns))
        fund_ret = returns[-min_len:]
        idx_ret = idx_returns[-min_len:]

        up_mask = idx_ret > 0
        down_mask = idx_ret < 0

        if np.sum(up_mask) > 0:
            features["capture_up"] = float(np.mean(fund_ret[up_mask]) / np.mean(idx_ret[up_mask]))
        else:
            features["capture_up"] = 1.0

        if np.sum(down_mask) > 0:
            features["capture_down"] = float(np.mean(fund_ret[down_mask]) / np.mean(idx_ret[down_mask]))
        else:
            features["capture_down"] = 1.0
    else:
        features["capture_up"] = 1.0
        features["capture_down"] = 1.0

    # 乖离率 BIAS（60日）
    if len(navs) >= 60:
        ma60 = np.mean(navs[-60:])
        features["bias_60d"] = float((navs[-1] - ma60) / ma60 * 100)
    else:
        features["bias_60d"] = 0.0

    return features


def save_features(features: dict) -> None:
    """将特征写入 fund_features 表。"""
    conn = _get_db()
    conn.execute(
        "INSERT OR REPLACE INTO fund_features "
        "(code, date, hurst_60d, momentum_20d, calmar, downside_vol, "
        "capture_up, capture_down, bias_60d) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            features["code"],
            features["date"],
            features.get("hurst_60d"),
            features.get("momentum_20d"),
            features.get("calmar"),
            features.get("downside_vol"),
            features.get("capture_up"),
            features.get("capture_down"),
            features.get("bias_60d"),
        ),
    )
    conn.commit()
    conn.close()


def calc_all_features(regime: str | None = None, batch_commit: int = 500) -> int:
    """对所有可投基金批量计算特征并入库（纯本地计算，不联网）。

    净值不足 60 天的基金按 calc_features 的门槛自动跳过。
    regime: 可选，写入 fund_features.regime 字段（当日大盘状态快照）。

    Returns:
        实际入库的基金数
    """
    conn = _get_db()
    if regime is None:
        regime = calc_regime(conn)

    all_codes = [
        r[0] for r in conn.execute(
            "SELECT code FROM fund_basic WHERE is_buyable = 1"
        ).fetchall()
    ]
    total = len(all_codes)
    logger.info("待计算特征基金: %d 只, 当前 regime=%s", total, regime)

    done = 0
    saved = 0
    start_time = time.monotonic()
    for code in all_codes:
        features = calc_features(code, conn)
        done += 1
        if features:
            conn.execute(
                "INSERT OR REPLACE INTO fund_features "
                "(code, date, regime, hurst_60d, momentum_20d, calmar, downside_vol, "
                "capture_up, capture_down, bias_60d) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    features["code"], features["date"], regime,
                    features.get("hurst_60d"), features.get("momentum_20d"),
                    features.get("calmar"), features.get("downside_vol"),
                    features.get("capture_up"), features.get("capture_down"),
                    features.get("bias_60d"),
                ),
            )
            saved += 1
        if done % batch_commit == 0:
            conn.commit()
            elapsed = time.monotonic() - start_time
            speed = done / elapsed if elapsed > 0 else 0
            logger.info("进度 %d/%d, 已入库 %d, 速度 %.0f/s", done, total, saved, speed)

    conn.commit()
    conn.close()
    elapsed = time.monotonic() - start_time
    logger.info("特征计算完成: 入库 %d 只(跳过 %d 只数据不足), 耗时 %.1f 秒",
                saved, total - saved, elapsed)
    return saved


# ========== 主流程 ==========

def run_pipeline(steps: list[int] | None = None):
    """执行数据基座全流程。steps=None 表示全部执行。"""
    all_steps = {1, 2, 3, 4, 5, 6, 7}
    steps = steps or all_steps

    settings = _load_settings()
    conn = _get_db()

    # Step 1: 基金列表（每周更新，不足 7 天自动跳过）
    if 1 in steps:
        logger.info("=== Step 1: 基金列表获取与过滤 ===")
        update_fund_list_weekly(settings)

    # Step 2: 净值增量更新
    if 2 in steps:
        logger.info("=== Step 2: 净值增量更新 ===")
        cur = conn.execute("SELECT code FROM fund_basic WHERE is_buyable = 1")
        codes = [r[0] for r in cur.fetchall()]
        total_new = 0
        for i, code in enumerate(codes[:10]):  # 先测前10只
            new_count = fetch_fund_nav_incremental(code, conn, settings)
            total_new += new_count
            if (i + 1) % 5 == 0:
                logger.info("进度 %d/%d，新增 %d 条", i + 1, len(codes), total_new)
            time.sleep(0.5)
        conn.commit()
        logger.info("净值增量更新完成，共新增 %d 条", total_new)

    # Step 3: 宏观指数（EMA60 增量）
    if 3 in steps:
        logger.info("=== Step 3: 宏观指数获取 ===")
        # 本地有数据则只拉最近少量做增量续算；无数据则冷启动拉 250 条
        has_index = conn.execute(
            "SELECT 1 FROM index_daily WHERE code = 'sh000300' LIMIT 1"
        ).fetchone()
        datalen = 10 if has_index else 250
        index_data = fetch_index_daily(settings, datalen=datalen)
        n = save_index_daily("sh000300", index_data)
        logger.info("沪深300日线新增 %d 条", n)

    # Step 4: 大盘状态机
    if 4 in steps:
        logger.info("=== Step 4: 大盘状态机 ===")
        regime = calc_regime(conn)
        logger.info("当前大盘状态: %s", regime)

    # Step 5: 重仓股（全量并发下载）
    if 5 in steps:
        logger.info("=== Step 5: 重仓股数据获取 ===")
        asyncio.run(async_download_all_holdings())

    # Step 6: RBSA 行业暴露
    if 6 in steps:
        logger.info("=== Step 6: RBSA 行业暴露 ===")
        cur = conn.execute("SELECT code FROM fund_holdings GROUP BY code LIMIT 3")
        for (code,) in cur.fetchall():
            # 仅取该基金最新季报（MAX report_date），避免混用多季度持仓
            cur_h = conn.execute(
                "SELECT stock_code, stock_name, weight FROM fund_holdings "
                "WHERE code = ? AND report_date = "
                "(SELECT MAX(report_date) FROM fund_holdings WHERE code = ?)",
                (code, code),
            )
            holdings = [{"stock_code": r[0], "stock_name": r[1], "weight": r[2]} for r in cur_h.fetchall()]
            rbsa = calc_rbsa(holdings)
            logger.info("基金 %s 行业暴露: %s", code, rbsa)

    # Step 7: 特征计算（全量本地计算入库）
    if 7 in steps:
        logger.info("=== Step 7: 特征计算 ===")
        regime = calc_regime(conn)
        calc_all_features(regime=regime)

    conn.close()
    logger.info("数据基座流程完成")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--step":
        step_num = int(sys.argv[2])
        run_pipeline(steps=[step_num])
    elif len(sys.argv) > 1 and sys.argv[1] == "--async-nav":
        concurrency = int(sys.argv[2]) if len(sys.argv) > 2 else 20
        force = "--force-full" in sys.argv
        asyncio.run(async_download_all_nav(concurrency=concurrency, force_full=force))
    elif len(sys.argv) > 1 and sys.argv[1] == "--update-nav":
        concurrency = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 20
        asyncio.run(async_update_nav_incremental(concurrency=concurrency))
    elif len(sys.argv) > 1 and sys.argv[1] == "--holdings":
        concurrency = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 20
        asyncio.run(async_download_all_holdings(concurrency=concurrency))
    elif len(sys.argv) > 1 and sys.argv[1] == "--features":
        calc_all_features()
    else:
        run_pipeline()

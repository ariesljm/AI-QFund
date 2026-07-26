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

import features as _features
from data_store import _db_conn, _get_db, _load_settings, _meta_get, _meta_set, _save_nav_batch, save_fund_list, save_index_daily, save_holdings
from fetch import fetch_async
from fetch import fetch as _push2_fetch

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

logger = logging.getLogger(__name__)


def _fetch(url: str, params: dict | None = None, timeout: float = 15) -> requests.Response:
    """发起 GET 请求，绕过系统代理。"""
    from fetch import fetch
    return fetch(url, params=params, timeout=timeout)


# ========== 数据源 URL 常量 ==========

# 基金全量列表（天天基金排行接口，按类型分页拉取，返回 代码|名称|类型|...）
_API_FUND_LIST_URL = "https://fundapi.eastmoney.com/fundtradenew.aspx"

# 单只基金历史净值（东方财富 pingzhongdata，{code} 替换为基金代码，仅首次全量用）
_API_PINGZHONGDATA_URL = "http://fund.eastmoney.com/pingzhongdata/{code}.js"

# 单只基金历史净值分页接口（支持日期范围，用于真·增量更新）
_API_LSJZ_URL = "https://api.fund.eastmoney.com/f10/lsjz"

# 单只基金季报持仓明细（天天基金 f10，返回 HTML，含股票代码/名称/占净值比例）
_API_HOLDINGS_URL = "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"

# 宽基指数日线（新浪财经 K 线接口，返回标准 JSON）
_API_INDEX_URL = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"

# 沪深300 在新浪接口中的标的代号（sh=上海，000300=沪深300）
_API_HS300_SYMBOL = "sh000300"


# ========== 1.1 基金列表获取与过滤 ==========

# 需要剔除的基金类型关键词（名称维度兜底，数据源名称可能不准）
_EXCLUDE_KEYWORDS = ["货币", "债券", "封闭", "偏债", "QDII", "FOF", "理财", "定开", "定期开放", "持有"]

# 交易所上市基金（ETF/LOF/封基）代码前缀：只能通过证券账户场内交易，
# 不属于可场外申购的开放式基金，直接按代码剔除（不依赖名称，避免名称错配漏剔）
_EXCLUDE_CODE_PREFIXES = ("15", "16", "18", "50", "51", "55", "56", "58", "59")


def fetch_fund_list(settings: dict | None = None) -> list[dict]:
    """获取并过滤基金列表，剔除不可投类型。

    返回格式：[{"code": "000001", "name": "华夏成长", "type": "混合型", "is_buyable": 1}, ...]
    """
    url = _API_FUND_LIST_URL

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
                # 先按代码前缀剔除场内上市基金（确定可靠，不依赖名称）
                if code.startswith(_EXCLUDE_CODE_PREFIXES):
                    continue
                # 再按名称关键词过滤不可投类型（名称维度兜底）
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


_LIST_UPDATE_INTERVAL_DAYS = 7


def update_fund_list_weekly(settings: dict | None = None, force: bool = False) -> int:
    """按周更新基金列表：距上次更新不足 7 天则跳过。

    返回本次实际写入的基金条数；跳过时返回 -1。
    """
    with _db_conn() as conn:
        last = _meta_get(conn, "fund_list_last_update")
        if last and not force:
            last_dt = datetime.strptime(last, "%Y-%m-%d")
            age_days = (datetime.now() - last_dt).days
            if age_days < _LIST_UPDATE_INTERVAL_DAYS:
                logger.info("基金列表 %d 天前更新过（<%d 天），跳过",
                            age_days, _LIST_UPDATE_INTERVAL_DAYS)
                return -1

    funds = fetch_fund_list(settings)
    n = save_fund_list(funds)
    with _db_conn() as conn:
        _meta_set(conn, "fund_list_last_update", datetime.now().strftime("%Y-%m-%d"))
    logger.info("基金列表更新完成，写入 %d 条", n)
    return n


# ========== 1.2 净值增量更新 ==========

def fetch_fund_nav(code: str, settings: dict | None = None) -> list[dict]:
    """从 pingzhongdata 拉取单只基金历史净值（累计净值）。

    返回格式：[{"date": "2024-01-02", "cum_nav": 1.2345}, ...]
    """
    url = _API_PINGZHONGDATA_URL.format(code=code)
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
            resp = await fetch_async(session, url, timeout=15)
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
    # ponytail: 复用 TCP/TLS 连接（keep-alive），关闭 force_close 显著提速
    connector = aiohttp.TCPConnector(limit=concurrency, ttl_dns_cache=300, enable_cleanup_closed=True)

    async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
        tasks = [_async_fetch_one(session, c, url_template, semaphore) for c in codes]
        results = await asyncio.gather(*tasks)

    return {code: navs for code, navs in results}


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
                resp = await fetch_async(
                    session, lsjz_url, params=params, timeout=15,
                    headers=_LSJZ_HEADERS,
                )
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

    lsjz_url = _API_LSJZ_URL
    end_date = datetime.now().strftime("%Y-%m-%d")

    with _db_conn() as conn:
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
        return 0

    semaphore = asyncio.Semaphore(concurrency)
    # ponytail: 复用连接提速
    connector = aiohttp.TCPConnector(limit=concurrency, ttl_dns_cache=300, enable_cleanup_closed=True)
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
            with _db_conn() as conn:
                for code, navs in results:
                    batch_new += _save_nav_batch(conn, code, navs)
                    total_done += 1
            total_new += batch_new

            elapsed = time.monotonic() - start_time
            speed = total_done / elapsed if elapsed > 0 else 0
            logger.info(
                "进度 %d/%d (+%d), 速度 %.1f/s",
                total_done, len(tasks_meta), batch_new, speed,
            )

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

    url_template = _API_PINGZHONGDATA_URL

    with _db_conn() as conn:
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
        return 0

    total_new = 0
    total_done = 0
    start_time = time.monotonic()

    # 分批下载
    for i in range(0, len(all_codes), batch_size):
        batch = all_codes[i : i + batch_size]
        results = await _async_batch_fetch(batch, url_template, concurrency)

        batch_new = 0
        with _db_conn() as conn:
            for code, navs in results.items():
                n = _save_nav_batch(conn, code, navs)
                batch_new += n
                total_done += 1
        total_new += batch_new

        elapsed = time.monotonic() - start_time
        speed = total_done / elapsed if elapsed > 0 else 0
        eta = (len(all_codes) - total_done) / speed if speed > 0 else 0
        logger.info(
            "进度 %d/%d (+%d), 速度 %.1f/s, ETA %.0fs",
            total_done, len(all_codes), batch_new, speed, eta,
        )

    elapsed = time.monotonic() - start_time
    logger.info("全量下载完成: %d 条净值, 耗时 %.1f 秒", total_new, elapsed)
    return total_new


# ========== 1.3 宏观指数获取 ==========

def fetch_index_daily(settings: dict | None = None, datalen: int = 250) -> list[dict]:
    """获取沪深300指数日线数据（新浪 K 线接口）。

    datalen: 拉取的交易日条数。冷启动默认 250 条（约 1 年），
    足够 EMA60 收敛；增量场景由调用方传入较小值。
    """
    url = _API_INDEX_URL
    params = {
        "symbol": _API_HS300_SYMBOL,
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


_API_ETF510300_SYMBOL = "sh510300"  # 沪深300ETF，用于资金流斜率


def fetch_etf_daily(datalen: int = 10) -> list[dict]:
    """获取沪深300ETF（510300）日线数据，复用新浪 K 线接口。

    用于计算 etf_flow_slope_5d（市场资金流斜率代理）。
    datalen 默认 10，足够 5 日回归 + 余量。
    """
    url = _API_INDEX_URL
    params = {
        "symbol": _API_ETF510300_SYMBOL,
        "scale": 240,
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




# ========== 1.5 重仓股数据获取 ==========

# f10 持仓 HTML 解析：报告期 + 每行 [代码, 名称, 占净值比例]
_HOLDING_DATE_RE = re.compile(r"([\d]{4}-[\d]{2}-[\d]{2})</font></label>")
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
    params = {"type": "jjcc", "code": code, "topline": "10", "year": "", "month": ""}
    resp = _fetch(_API_HOLDINGS_URL, params)
    return _parse_holdings_html(resp.text)



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
            resp = await fetch_async(
                session, holdings_url, params=params, timeout=15,
                headers=_HOLDINGS_HEADERS,
            )
            raw = await resp.read()
            # 优先从响应头获取编码，东财传统为 GBK
            charset = resp.charset or "gbk"
            try:
                text = raw.decode(charset)
            except (UnicodeDecodeError, LookupError):
                text = raw.decode("gbk", errors="replace")
            report_date, holdings = _parse_holdings_html(text)
            return code, report_date, holdings
        except Exception as e:
            logger.debug("基金 %s 持仓异步拉取失败: %s", code, e)
            return code, None, []


async def async_download_all_holdings(
    concurrency: int = 20,
    batch_size: int = 200,
) -> int:
    """异步并发下载所有可投基金最新季报重仓股（f10 jjcc 接口）。

    根据当前日期推算最新季报截止日，仅拉取本地缺数据或数据过期的基金。
    """
    if not HAS_AIOHTTP:
        raise RuntimeError("需要安装 aiohttp: uv add aiohttp")

    holdings_url = _API_HOLDINGS_URL

    with _db_conn() as conn:
        all_codes = [
            r[0] for r in conn.execute(
                "SELECT code FROM fund_basic WHERE is_buyable = 1"
            ).fetchall()
        ]

        # 推算最新季报截止日
        today = datetime.now()
        m = today.month
        if m <= 3:
            latest_quarter = f"{today.year - 1}-12-31"
        elif m <= 6:
            latest_quarter = f"{today.year}-03-31"
        elif m <= 9:
            latest_quarter = f"{today.year}-06-30"
        else:
            latest_quarter = f"{today.year}-09-30"

        # 本地各基金最新 report_date
        local_latest = dict(
            conn.execute(
                "SELECT code, MAX(report_date) FROM fund_holdings GROUP BY code"
            ).fetchall()
        )
        # 只拉取本地没有持仓、或持仓期早于最新季报截止日的基金
        all_codes = [
            c for c in all_codes
            if local_latest.get(c) is None or local_latest[c] < latest_quarter
        ]
        logger.info(
            "持仓增量模式：最新季报 %s, 已是最新 %d 只跳过, 待下载 %d 只",
            latest_quarter, len(local_latest) - len(all_codes), len(all_codes),
        )

        semaphore = asyncio.Semaphore(concurrency)
        connector = aiohttp.TCPConnector(limit=concurrency, ttl_dns_cache=300, enable_cleanup_closed=True)
        total_rows = 0
        total_done = 0
        funds_with_holdings = 0
        start_time = time.monotonic()

        async with aiohttp.ClientSession(headers=_HOLDINGS_HEADERS, connector=connector, trust_env=False) as session:
            for i in range(0, len(all_codes), batch_size):
                batch = all_codes[i : i + batch_size]
                coros = [
                    _async_fetch_holdings_one(session, c, holdings_url, semaphore)
                    for c in batch
                ]
                results = await asyncio.gather(*coros)

                batch_rows = 0
                for code, report_date, holdings in results:
                    if holdings and report_date and report_date != local_latest.get(code):
                        conn.executemany(
                            "INSERT OR REPLACE INTO fund_holdings "
                            "(code, report_date, stock_code, stock_name, weight) "
                            "VALUES (?, ?, ?, ?, ?)",
                            [(code, report_date, h["stock_code"], h["stock_name"], h["weight"])
                             for h in holdings],
                        )
                        batch_rows += len(holdings)
                        funds_with_holdings += 1
                        local_latest[code] = report_date
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
    elapsed = time.monotonic() - start_time
    logger.info(
        "持仓下载完成: %d 只有持仓, 共 %d 条, 耗时 %.1f 秒",
        funds_with_holdings, total_rows, elapsed,
    )
    return total_rows


# ========== 1.6 RBSA 行业暴露（申万二级行业）==========

def update_industry_map(force: bool = False) -> int:
    """从东方财富拉取申万二级行业映射，写入 stock_industry_map 表。

    申万二级行业不常变动，默认 90 天内不重复拉取（force=True 强制刷新）。
    Returns: 写入的记录数
    """
    with _db_conn() as conn:
        # 检查是否需要更新
        if not force:
            row = conn.execute("SELECT value FROM meta WHERE key = 'industry_map_updated'").fetchone()
            if row:
                last_update = datetime.strptime(row[0], "%Y-%m-%d")
                if datetime.now() - last_update < timedelta(days=90):
                    logger.info("行业映射距上次更新不足 90 天，跳过")
                    return 0

        logger.info("正在拉取申万二级行业映射...")
        try:
            records = _fetch_industry_map()
        except Exception as e:
            logger.error("拉取行业映射失败: %s", e)
            return 0

        if not records:
            logger.warning("行业映射为空")
            return 0

        today = datetime.now().strftime("%Y-%m-%d")
        conn.executemany(
            "INSERT OR REPLACE INTO stock_industry_map (stock_code, industry_code, industry_name, update_date) "
            "VALUES (?, ?, ?, ?)",
            [(sc, ic, in_, today) for sc, ic, in_ in records],
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('industry_map_updated', ?)",
            (today,),
        )
    logger.info("行业映射更新完成: %d 条记录", len(records))
    return len(records)


def _build_candidates(stock_code: str) -> list[tuple[str, dict]]:
    """根据股票代码生成 emweb 查询候选列表。

    港股（5位数字）: 优先尝试 HK 前缀的 HSF10，再尝试独立 HKF10。
    北交所（92开头）: BJ 前缀。
    A股（6开头=上交所，0/3开头=深交所）。
    """
    hsf10 = "https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax"
    hkf10 = "https://emweb.securities.eastmoney.com/PC_HKF10/CompanySurvey/PageAjax"

    if len(stock_code) == 5:
        return [
            (hsf10, {"code": f"HK{stock_code}"}),
            (hkf10, {"code": stock_code}),
        ]
    elif stock_code.startswith("92"):
        return [
            (hsf10, {"code": f"BJ{stock_code}"}),
            (hsf10, {"code": f"SZ{stock_code}"}),
        ]
    elif stock_code.startswith("6"):
        return [(hsf10, {"code": f"SH{stock_code}"})]
    else:
        return [(hsf10, {"code": f"SZ{stock_code}"})]


def _fetch_hk_industry_push2(hk_stocks: list[str], results: dict[str, tuple[str, str]]) -> int:
    """港股行业映射回退方案：通过 push2 实时行情 API 获取行业分类。

    emweb 的 PC_HKF10 接口已废弃（返回 404），HSF10+HK 前缀无数据，
    改用 push2.eastmoney.com 的个股行情接口获取 f100（所属行业）字段。

    Returns: 成功映射的数量
    """
    added = 0
    for stock_code in hk_stocks:
        secid = f"116.{stock_code}"
        try:
            resp = _push2_fetch(
                "https://push2.eastmoney.com/api/qt/stock/get",
                {
                    "secid": secid,
                    "fields": "f57,f58,f100",
                    "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                },
                timeout=10,
            )
            data = resp.json()
            stock_data = data.get("data")
            if stock_data and isinstance(stock_data, dict):
                industry = stock_data.get("f100", "")
                if industry and industry != "-":
                    results[stock_code] = (industry, industry)
                    added += 1
                    logger.debug("港股 %s 行业映射(push2): %s", stock_code, industry)
                    continue
        except Exception as e:
            logger.debug("港股 %s push2 查询失败: %s", stock_code, e)
    return added


def _fetch_industry_map() -> list[tuple[str, str, str]]:
    """从东方财富 emweb 拉取股票→申万二级行业映射。

    返回 [(stock_code, industry_code, industry_name), ...]
    使用 asyncio 并发拉取，约 3-5 分钟完成全量。
    """
    with _db_conn() as conn:
        all_stocks = [
            r[0] for r in conn.execute(
                "SELECT DISTINCT stock_code FROM fund_holdings"
            ).fetchall()
        ]

    if not all_stocks:
        return []

    logger.info("需要查询 %d 只股票的行业分类...", len(all_stocks))

    semaphore = asyncio.Semaphore(30)
    results: dict[str, tuple[str, str]] = {}
    success = 0
    fail = 0

    async def _fetch_one(session, stock_code: str):
        nonlocal success, fail
        candidate_urls = _build_candidates(stock_code)
        async with semaphore:
            for url, params in candidate_urls:
                for _ in range(2):
                    try:
                        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                            if resp.status != 200:
                                continue
                            data = await resp.json(content_type=None)
                            items = data.get("jbzl", [])
                            if items:
                                item = items[0]
                                em2016 = item.get("EM2016", "")
                                if em2016:
                                    parts = em2016.split("-")
                                    industry = parts[1] if len(parts) > 1 else parts[0]
                                    results[stock_code] = (em2016, industry)
                                    success += 1
                                    return
                    except Exception as e:
                        logger.warning("股票 %s 行业映射查询失败: %s", stock_code, e)
            fail += 1

    async def _run():
        connector = aiohttp.TCPConnector(limit=30, ssl=False)
        async with aiohttp.ClientSession(
            connector=connector,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://emweb.securities.eastmoney.com/"},
        ) as session:
            tasks = [_fetch_one(session, sc) for sc in all_stocks]
            await asyncio.gather(*tasks)

    asyncio.run(_run())
    logger.info("行业查询完成: 成功 %d, 失败 %d", success, fail)

    # 港股（5位代码）emweb F10 接口已废弃（404），改用 push2 实时行情接口回退
    _hk_unmapped = [s for s in all_stocks if len(s) == 5 and s not in results]
    if _hk_unmapped:
        _hk_success = _fetch_hk_industry_push2(_hk_unmapped, results)
        logger.info("港股 push2 行业回退: 新增 %d 条", _hk_success)

    return [(sc, info[0], info[1]) for sc, info in results.items()]





# ========== 主流程 ==========

def run_pipeline(steps: list[int] | None = None):
    """执行数据基座全流程。steps=None 表示全部执行。"""
    all_steps = {1, 2, 3, 4, 5, 6, 7, 8}
    steps = steps or all_steps

    with _db_conn() as conn:

        # Step 1: 基金列表（每周更新，不足 7 天自动跳过）
        if 1 in steps:
            logger.info("=== Step 1: 基金列表获取与过滤 ===")
            update_fund_list_weekly()

        # Step 2: 净值更新
        #   首次（本地无净值数据）→ async_download_all_nav：pingzhongdata 单请求/基金，高并发全量
        #   日常（已有数据）→ async_update_nav_incremental：lsjz 按日期真·增量，仅补缺失
        if 2 in steps:
            has_nav = conn.execute("SELECT 1 FROM fund_nav LIMIT 1").fetchone()
            if has_nav:
                logger.info("=== Step 2: 净值增量更新（并发增量）===")
                total_new = asyncio.run(async_update_nav_incremental(concurrency=20))
            else:
                logger.info("=== Step 2: 净值首次全量下载（pingzhongdata 高并发）===")
                total_new = asyncio.run(async_download_all_nav(concurrency=50))
            logger.info("净值更新完成，共新增 %d 条", total_new)

        # Step 3: 宏观指数（EMA60 增量）
        if 3 in steps:
            logger.info("=== Step 3: 宏观指数获取 ===")
            # 本地有数据则只拉最近少量做增量续算；无数据则冷启动拉 250 条
            has_index = conn.execute(
                "SELECT 1 FROM index_daily WHERE code = 'sh000300' LIMIT 1"
            ).fetchone()
            datalen = 10 if has_index else 250
            index_data = fetch_index_daily(datalen=datalen)
            n = save_index_daily("sh000300", index_data)
            logger.info("沪深300日线新增 %d 条", n)
            # 同步拉取沪深300ETF（510300）日线，用于资金流斜率
            has_etf = conn.execute(
                "SELECT 1 FROM index_daily WHERE code = 'sh510300' LIMIT 1"
            ).fetchone()
            etf_datalen = 10 if has_etf else 250
            etf_data = fetch_etf_daily(datalen=etf_datalen)
            n_etf = save_index_daily("sh510300", etf_data)
            logger.info("沪深300ETF(510300)日线新增 %d 条", n_etf)

        # Step 4: 重仓股（全量并发下载）
        if 4 in steps:
            logger.info("=== Step 4: 重仓股数据获取 ===")
            asyncio.run(async_download_all_holdings())
            # 持仓入库后立即更新行业映射，确保 Step 6 特征计算中 RBSA 可用
            logger.info("更新申万行业映射（持仓→行业）...")
            total_mapped = update_industry_map(force=True)
            logger.info("行业映射完成: %d 条", total_mapped)

        # Step 6: RBSA 行业暴露（行业映射已在 Step 5 完成，此处仅日志确认）
        if 6 in steps:
            logger.info("=== Step 6: RBSA 行业暴露 ===")
            mapped = conn.execute(
                "SELECT COUNT(*) FROM stock_industry_map"
            ).fetchone()[0]
            holdings_funds = conn.execute(
                "SELECT COUNT(DISTINCT code) FROM fund_holdings"
            ).fetchone()[0]
            logger.info("stock_industry_map: %d 条, fund_holdings 覆盖: %d 只基金", mapped, holdings_funds)

        # Step 7: 特征计算（全量本地计算入库）
        if 7 in steps:
            logger.info("=== Step 7: 特征计算 ===")
            _features.calc_all_features()

        # Step 8: 推荐引擎（需模型就绪）
        if 8 in steps:
            logger.info("=== Step 8: 推荐引擎 ===")
            # 检查模型是否存在，不存在则训练
            from pathlib import Path as _Path
            model_path = _Path("models/lgb_model.txt")
            if not model_path.exists():
                logger.info("模型不存在，跳过推荐（需手动运行 python recommend.py --retrain）")
            else:
                logger.info("模型已就绪，可运行推荐管线")

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
        _features.calc_all_features()
    else:
        run_pipeline()


def _call_llm(
    prompt: str,
    system_prompt: str = "",
    temperature: float = 0.2,
    max_tokens: int = 1024,
) -> str | None:
    """统一 LLM 调用接口，包含重试逻辑。
    
    重试策略：限流(429)、服务端错误(5xx)、网络瞬时异常 → 指数退避重试最多3次。
    配置缺失或客户端错误(4xx非429) → 不重试直接返回 None。
    """
    settings = _load_settings()
    llm_cfg = settings.get("llm", {})
    api_key = llm_cfg.get("api_key", "")
    if not api_key:
        logger.warning("LLM 未配置")
        return None

    t0 = time.time()
    attempt = 0
    try:
        from openai import OpenAI
        client = OpenAI(base_url=llm_cfg.get("base_url"), api_key=api_key)

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        # ponytail: 可重试的错误：429限流、5xx服务端、网络瞬时异常
        _RETRYABLE_TERMS = ("429", "500", "502", "503", "504",
                             "RateLimit", "ConnectionError", "Timeout",
                             "Connection reset", "RemoteDisconnected")
        for attempt in range(3):
            t_call = time.time()
            try:
                resp = client.chat.completions.create(
                    model=llm_cfg.get("model", "gpt-4o-mini"),
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format={"type": "json_object"},
                )
                elapsed = (time.time() - t0) * 1000
                logger.info("LLM 调用成功: model=%s attempt=%d/%d duration=%dms tokens=%d",
                            llm_cfg.get("model"), attempt + 1, 3, int(elapsed),
                            resp.usage.total_tokens if resp.usage else 0)
                return resp.choices[0].message.content
            except Exception as e:
                e_str = str(e)
                e_type = type(e).__name__
                is_retryable = any(term in e_str or term in e_type for term in _RETRYABLE_TERMS)
                if is_retryable:
                    delay = 2 ** attempt * 2
                    logger.warning("LLM 可重试错误 (attempt %d/3, %ds后重试): %s: %s",
                                   attempt + 1, delay, e_type, e_str[:120])
                    time.sleep(delay)
                    continue
                elapsed = (time.time() - t0) * 1000
                logger.error("LLM 不可重试错误: attempt=%d/%d duration=%dms error=%s: %s",
                             attempt + 1, 3, int(elapsed), e_type, e_str[:120])
                return None
        elapsed = (time.time() - t0) * 1000
        logger.error("LLM 重试耗尽: attempts=%d duration=%dms", attempt + 1, int(elapsed))
        return None
    except Exception as e:
        elapsed = (time.time() - t0) * 1000
        logger.error("LLM 异常: duration=%dms error=%s", int(elapsed), e)
        return None

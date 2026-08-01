"""数据基座：基金列表、净值、指数、持仓、行业映射、RBSA。"""

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

from app.database import db_conn, meta_get, meta_set, DB_PATH
from app.data.fetchers import fetch as _push2_fetch, fetch_async
from app.data.nav import async_update_nav_incremental, async_download_all_nav
from app.data.store import save_fund_list, save_index_daily, backfill_guard
from app.features import calculator as _features
from app.utils.log import get_logger

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

logger = get_logger(__name__)

_API_FUND_LIST_URL = "https://fund.eastmoney.com/js/fundcode_search.js"
_API_HOLDINGS_URL = "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
_API_INDEX_URL = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
_API_HS300_SYMBOL = "sh000300"

_EXCLUDE_KEYWORDS = ["货币", "债券", "封闭", "偏债", "QDII", "FOF", "理财", "定开", "定期开放", "持有", "LOF", "后端"]
_EXCLUDE_CODE_PREFIXES = ("15", "16", "18", "50", "51", "55", "56", "58", "59")


def fetch_fund_list(settings: dict | None = None) -> list[dict]:
    resp = _push2_fetch(
        _API_FUND_LIST_URL,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://fund.eastmoney.com/"},
        timeout=30,
    )
    resp.encoding = "utf-8"
    m = re.search(r"var\s+r\s*=\s*(\[.*?\])\s*;", resp.text, re.DOTALL)
    if not m:
        logger.error("基金列表 JS 解析失败")
        return []
    data = json.loads(m.group(1))
    type_map = {"股票型": "股票型", "混合型": "混合型", "指数型": "指数型"}
    result = []
    for code, _, name, full_type, *_ in data:
        base_type = full_type.split("-")[0]
        if base_type not in type_map:
            continue
        if code.startswith(_EXCLUDE_CODE_PREFIXES):
            continue
        if name.endswith("Y"):
            continue
        if any(kw in name for kw in _EXCLUDE_KEYWORDS):
            continue
        result.append({"code": code, "name": name, "type": type_map[base_type], "is_buyable": 1})
    logger.info("基金列表: 源 %d 只 → 筛选后 %d 只", len(data), len(result))
    return result


_LIST_UPDATE_INTERVAL_DAYS = 7


def update_fund_list_weekly(settings: dict | None = None, force: bool = False) -> int:
    with db_conn() as conn:
        last = meta_get(conn, "fund_list_last_update")
        if last and not force:
            last_dt = datetime.strptime(last, "%Y-%m-%d")
            age_days = (datetime.now() - last_dt).days
            if age_days < _LIST_UPDATE_INTERVAL_DAYS:
                logger.info("基金列表 %d 天前更新过（<%d 天），跳过",
                            age_days, _LIST_UPDATE_INTERVAL_DAYS)
                return -1
    funds = fetch_fund_list(settings)
    n = save_fund_list(funds)
    with db_conn() as conn:
        meta_set(conn, "fund_list_last_update", datetime.now().strftime("%Y-%m-%d"))
    logger.info("基金列表更新完成，写入 %d 条", n)
    return n


# ── 指数数据 ──

def fetch_index_daily(settings: dict | None = None, datalen: int = 250) -> list[dict]:
    url = _API_INDEX_URL
    params = {
        "symbol": _API_HS300_SYMBOL,
        "scale": 240,
        "ma": 60,
        "datalen": datalen,
    }
    resp = _push2_fetch(url, params=params, timeout=15)
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


_API_ETF510300_SYMBOL = "sh510300"


def fetch_etf_daily(datalen: int = 10) -> list[dict]:
    url = _API_INDEX_URL
    params = {
        "symbol": _API_ETF510300_SYMBOL,
        "scale": 240,
        "ma": 60,
        "datalen": datalen,
    }
    resp = _push2_fetch(url, params=params, timeout=15)
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


# ── 持仓数据 ──

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


def fetch_holdings(code: str, settings: dict | None = None) -> tuple[str | None, list[dict]]:
    params = {"type": "jjcc", "code": code, "topline": "10", "year": "", "month": ""}
    resp = requests.get(_API_HOLDINGS_URL, params=params, timeout=15)
    return _parse_holdings_html(resp.text)


_HOLDINGS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://fundf10.eastmoney.com/",
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
}


async def _async_fetch_holdings_one(
    session: "aiohttp.ClientSession",
    code: str,
    holdings_url: str,
    semaphore: asyncio.Semaphore,
) -> tuple[str, str | None, list[dict]]:
    params = {"type": "jjcc", "code": code, "topline": "10", "year": "", "month": ""}
    async with semaphore:
        try:
            resp = await fetch_async(
                session, holdings_url, params=params, timeout=15,
                headers=_HOLDINGS_HEADERS,
            )
            raw = await resp.read()
            charset = resp.charset or "gbk"
            try:
                text = raw.decode(charset)
            except UnicodeDecodeError:
                text = raw.decode("gbk", errors="replace")
            report_date, holdings = _parse_holdings_html(text)
            return code, report_date, holdings, False
        except Exception as e:
            logger.debug("基金 %s 持仓异步拉取失败: %s", code, str(e)[:120], exc_info=True)
            return code, None, [], True


async def async_download_all_holdings(
    concurrency: int = 20,
    batch_size: int = 200,
) -> int:
    if not HAS_AIOHTTP:
        raise RuntimeError("需要安装 aiohttp: uv add aiohttp")

    holdings_url = _API_HOLDINGS_URL

    with db_conn() as conn:
        all_codes = [
            r[0] for r in conn.execute(
                "SELECT code FROM fund_basic WHERE is_buyable = 1"
            ).fetchall()
        ]

        today = datetime.now()
        m, d = today.month, today.day
        if m < 4:
            latest_quarter = f"{today.year - 1}-09-30"
        elif m == 4 and d <= 21:
            latest_quarter = f"{today.year - 1}-12-31"
        elif m < 7 or (m == 7 and d <= 21):
            latest_quarter = f"{today.year}-03-31"
        elif m < 10 or (m == 10 and d <= 21):
            latest_quarter = f"{today.year}-06-30"
        else:
            latest_quarter = f"{today.year}-09-30"

        local_latest = dict(
            conn.execute(
                "SELECT code, MAX(report_date) FROM fund_holdings GROUP BY code"
            ).fetchall()
        )
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
        all_failed: set[str] = set()
        start_time = time.monotonic()

        async with aiohttp.ClientSession(headers=_HOLDINGS_HEADERS, connector=connector, trust_env=False) as session:
            for i in range(0, len(all_codes), batch_size):
                batch = all_codes[i: i + batch_size]
                coros = [
                    _async_fetch_holdings_one(session, c, holdings_url, semaphore)
                    for c in batch
                ]
                results = await asyncio.gather(*coros)

                batch_rows = 0
                for code, report_date, holdings, failed in results:
                    if failed:
                        all_failed.add(code)
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

        if backfill_guard(all_failed, len(all_codes), "持仓拉取"):
            for code in all_failed:
                try:
                    resp = requests.get(
                        holdings_url,
                        params={"type": "jjcc", "code": code, "topline": "10", "year": "", "month": ""},
                        headers=_HOLDINGS_HEADERS, timeout=10,
                    )
                    if resp.status_code == 200:
                        text = resp.text
                        report_date, holdings = _parse_holdings_html(text)
                        if holdings and report_date and report_date != local_latest.get(code):
                            conn.executemany(
                                "INSERT OR REPLACE INTO fund_holdings "
                                "(code, report_date, stock_code, stock_name, weight) "
                                "VALUES (?, ?, ?, ?, ?)",
                                [(code, report_date, h["stock_code"], h["stock_name"], h["weight"])
                                 for h in holdings],
                            )
                            total_rows += len(holdings)
                            funds_with_holdings += 1
                            local_latest[code] = report_date
                except Exception as e:
                    logger.debug("补查基金 %s 失败: %s", code, str(e)[:120], exc_info=True)
            conn.commit()

    elapsed = time.monotonic() - start_time
    logger.info(
        "持仓下载完成: %d 只有持仓, 共 %d 条, 耗时 %.1f 秒",
        funds_with_holdings, total_rows, elapsed,
    )
    return total_rows


# ── 行业映射 ──

def update_industry_map(force: bool = False) -> int:
    with db_conn() as conn:
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
            logger.error("拉取行业映射失败: %s", str(e)[:120], exc_info=True)
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


_HK_NAME_INDUSTRY_HINTS: dict[str, str] = {
    "银行": "银行", "保险": "保险", "证券": "证券", "期货": "期货",
    "地产": "地产", "置业": "地产", "物业": "地产",
    "石油": "石油天然气", "燃气": "燃气", "煤炭": "煤炭", "电力": "电力",
    "汽车": "汽车", "医药": "医药", "生物": "生物医药", "医疗": "医疗器械",
    "半导体": "半导体", "芯片": "半导体", "软件": "软件服务", "互联网": "互联网",
    "科技": "科技", "通信": "通信", "食品": "食品饮料", "饮料": "食品饮料",
    "航空": "航空", "航运": "航运", "钢铁": "钢铁", "有色金属": "有色金属",
    "化工": "化工", "建筑": "建筑", "建材": "建筑材料", "零售": "零售",
    "游戏": "游戏", "传媒": "传媒", "水务": "水务", "公用事业": "公用事业",
}


def _infer_hk_industry_by_name(name: str) -> str:
    """数据源（东财 push2）无行业字段时，用股票名推断行业（仅作兜底）。"""
    for kw, industry in _HK_NAME_INDUSTRY_HINTS.items():
        if kw in name:
            return industry
    return ""


def _fetch_hk_industry_push2(hk_stocks: list[str], results: dict[str, tuple[str, str]]) -> int:
    """用 push2 批量行情接口为港股补行业分类。

    单个 stock/get 接口的 f127 虽能返回行业，但逐只请求会触发全局限速（10 次/分钟，
    数百只港股需半小时以上）；改用 ulist.np/get 批量接口（f12=代码, f100=东财行业），
    一次请求多只，秒级完成。个别港股东财无行业字段（如恒生银行），用名称兜底。
    """
    added = 0
    batch_size = 80
    for i in range(0, len(hk_stocks), batch_size):
        batch = hk_stocks[i:i + batch_size]
        secids = ",".join(f"116.{c}" for c in batch)
        try:
            resp = _push2_fetch(
                "https://push2.eastmoney.com/api/qt/ulist.np/get",
                {
                    "secids": secids,
                    "fields": "f12,f14,f100",
                    "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                },
                timeout=15,
            )
            data = resp.json().get("data") or {}
            diff = data.get("diff") or []
            for item in diff:
                if not isinstance(item, dict):
                    continue
                code = item.get("f12", "")
                industry = item.get("f100", "")
                if not industry or industry == "-":
                    industry = _infer_hk_industry_by_name(item.get("f14", "") or "")
                if code and industry:
                    results[code] = (industry, industry)
                    added += 1
                    logger.debug("港股 %s 行业映射(push2): %s", code, industry)
        except Exception as e:
            logger.debug("港股 push2 批量查询失败: %s", str(e)[:120], exc_info=True)
    return added


def _fetch_industry_map() -> list[tuple[str, str, str]]:
    with db_conn() as conn:
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
    logger.info("首次行业查询完成: 成功 %d, 失败 %d", success, fail)

    failed = [s for s in all_stocks if s not in results]
    if backfill_guard(failed, len(all_stocks), "行业映射"):
        _headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://emweb.securities.eastmoney.com/"}
        for sc in failed:
            for url, params in _build_candidates(sc):
                try:
                    resp = requests.get(url, params=params, headers=_headers, timeout=10)
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    items = data.get("jbzl", [])
                    if items:
                        item = items[0]
                        em2016 = item.get("EM2016", "")
                        if em2016:
                            parts = em2016.split("-")
                            industry = parts[1] if len(parts) > 1 else parts[0]
                            results[sc] = (em2016, industry)
                            break
                except Exception as e:
                    logger.debug("补查股票 %s 失败: %s", sc, str(e)[:120], exc_info=True)
        recovered = len([s for s in failed if s in results])
        logger.info("补查完成: 恢复 %d 只", recovered)

    _hk_unmapped = [s for s in all_stocks if len(s) == 5 and s not in results]
    if _hk_unmapped:
        _hk_success = _fetch_hk_industry_push2(_hk_unmapped, results)
        logger.info("港股 push2 行业回退: 新增 %d 条", _hk_success)

    return [(sc, info[0], info[1]) for sc, info in results.items()]


# ── 主流程 ──

def run_pipeline(steps: list[int] | None = None):
    all_steps = {1, 2, 3, 4, 6, 7, 8}
    steps = steps or all_steps

    with db_conn() as conn:

        if 1 in steps:
            logger.info("=== Step 1: 基金列表获取与过滤 ===")
            t1 = time.time()
            update_fund_list_weekly()
            logger.info("Step1 基金列表完成 (%.0fms)", (time.time() - t1) * 1000)

        if 2 in steps:
            has_nav = conn.execute("SELECT 1 FROM fund_nav LIMIT 1").fetchone()
            if has_nav:
                logger.info("=== Step 2: 净值增量更新（并发增量）===")
                t2 = time.time()
                total_new = asyncio.run(async_update_nav_incremental(concurrency=20))
            else:
                logger.info("=== Step 2: 净值首次全量下载（pingzhongdata 高并发）===")
                t2 = time.time()
                total_new = asyncio.run(async_download_all_nav(concurrency=30))
            logger.info("Step2 净值更新完成: %d 条 (%.0fms)",
                        total_new, (time.time() - t2) * 1000)

        if 3 in steps:
            logger.info("=== Step 3: 宏观指数获取 ===")
            try:
                index_data = fetch_index_daily(datalen=250)
                n = save_index_daily("sh000300", index_data)
                logger.info("沪深300日线新增 %d 条", n)
            except Exception as e:
                logger.error("沪深300 日线获取失败: %s", str(e)[:120], exc_info=True)
            try:
                etf_data = fetch_etf_daily(datalen=250)
                n_etf = save_index_daily("sh510300", etf_data)
                logger.info("沪深300ETF(510300)日线新增 %d 条", n_etf)
            except Exception as e:
                logger.error("沪深300ETF 日线获取失败: %s", str(e)[:120], exc_info=True)

        if 4 in steps:
            logger.info("=== Step 4: 重仓股数据获取 ===")
            asyncio.run(async_download_all_holdings())
            logger.info("更新申万行业映射（持仓→行业）...")
            total_mapped = update_industry_map()
            logger.info("行业映射完成: %d 条", total_mapped)

        if 6 in steps:
            logger.info("=== Step 6: RBSA 行业暴露 ===")
            mapped = conn.execute(
                "SELECT COUNT(*) FROM stock_industry_map"
            ).fetchone()[0]
            holdings_funds = conn.execute(
                "SELECT COUNT(DISTINCT code) FROM fund_holdings"
            ).fetchone()[0]
            logger.info("stock_industry_map: %d 条, fund_holdings 覆盖: %d 只基金",
                        mapped, holdings_funds)

        if 7 in steps:
            logger.info("=== Step 7: 特征计算 ===")
            _features.calc_all_features()

        if 8 in steps:
            logger.info("=== Step 8: 推荐引擎 ===")
            model_path = Path("models/lgb_model.txt")
            if not model_path.exists():
                logger.info("模型不存在，自动训练中...")
                from app.engine.recommend import prepare_lgb_training_data, train_lgb_model
                X_train, y_train, X_val, y_val = prepare_lgb_training_data()
                if len(X_train) == 0:
                    logger.error("训练样本为空，跳过模型训练")
                else:
                    train_lgb_model(X_train, y_train, X_val, y_val)
            else:
                logger.info("模型已就绪")

    logger.info("数据基座流程完成")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--step":
        step_num = int(sys.argv[2])
        run_pipeline(steps=[step_num])
    elif len(sys.argv) > 1 and sys.argv[1] == "--async-nav":
        concurrency = int(sys.argv[2]) if len(sys.argv) > 2 else 20
        asyncio.run(async_download_all_nav(concurrency=concurrency))
    elif len(sys.argv) > 1 and sys.argv[1] == "--update-nav":
        concurrency = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 20
        asyncio.run(async_update_nav_incremental(concurrency=concurrency))
    elif len(sys.argv) > 1 and sys.argv[1] == "--holdings":
        concurrency = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 20
        asyncio.run(async_download_all_holdings(concurrency=concurrency))
    elif len(sys.argv) > 1 and sys.argv[1] == "--features":
        _features.calc_all_features()
    elif len(sys.argv) > 1 and sys.argv[1] == "--industry-map":
        n = update_industry_map(force=True)
        logger.info("行业映射强制更新完成: %d 条", n)
    elif len(sys.argv) > 1 and sys.argv[1] == "--prune-nav":
        from app.data.nav import prune_nav_history
        n = prune_nav_history()
        logger.info("净值历史修剪: 删除 %d 行", n)
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("VACUUM")
        conn.close()
        logger.info("VACUUM 完成")
    else:
        run_pipeline()

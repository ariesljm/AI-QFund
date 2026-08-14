"""数据基座：基金列表、净值、指数、持仓、行业映射、RBSA。"""

import asyncio
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta


from app.database import db_conn, meta_get, meta_set, DB_PATH
from app.data.fetchers import fetch, fetch_async
from app.repo import meta_keys as META
from app.utils.trading_calendar import trading_day_lag  # 滞后交易日数单一来源
from app.data.ingest import run_batched_fetch, filter_cooldown_targets
from app.data.nav import async_update_nav_incremental, async_download_all_nav
from app.data.store import (save_fund_list, save_index_daily, record_failure,
                            mark_recovered_batch,
                            run_backfill_rounds)
from app.features import calculator as _features
from app.utils.log import get_logger

import httpx

logger = get_logger(__name__)

_API_FUND_LIST_URL = "https://fund.eastmoney.com/js/fundcode_search.js"
_API_HOLDINGS_URL = "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
_API_INDEX_URL = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
_API_HS300_SYMBOL = "sh000300"

_EXCLUDE_KEYWORDS = ["货币", "债券", "封闭", "偏债", "QDII", "FOF", "理财", "定开", "定期开放", "持有", "LOF", "后端"]
_EXCLUDE_CODE_PREFIXES = ("15", "16", "18", "50", "51", "55", "56", "58", "59")


def fetch_fund_list() -> list[dict]:
    resp = fetch(
        _API_FUND_LIST_URL,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://fund.eastmoney.com/"},
        timeout=30,
    )
    resp.encoding = "utf-8"
    m = re.search(r"var\s+r\s*=\s*(\[.*?\])\s*;", resp.text, re.DOTALL)
    if not m:
        logger.error("基金列表 JS 解析失败（正则未匹配 var r=[...]，本次基金列表为空，等待下次更新重试）")
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


def update_fund_list_weekly(force: bool = False) -> int:
    with db_conn() as conn:
        last = meta_get(conn, META.FUND_LIST_LAST_UPDATE)
        if last and not force:
            last_dt = datetime.strptime(last, "%Y-%m-%d")
            age_days = (datetime.now() - last_dt).days
            if age_days < _LIST_UPDATE_INTERVAL_DAYS:
                logger.info("基金列表 %d 天前更新过（<%d 天），跳过",
                            age_days, _LIST_UPDATE_INTERVAL_DAYS)
                return -1
    funds = fetch_fund_list()
    n = save_fund_list(funds)
    with db_conn() as conn:
        meta_set(conn, META.FUND_LIST_LAST_UPDATE, datetime.now().strftime("%Y-%m-%d"))
    logger.info("基金列表更新完成，写入 %d 条", n)
    return n


# ── 指数数据 ──

def _fetch_kline(symbol: str, datalen: int) -> list[dict]:
    """拉取单标的日 K 线（沪深300 指数与 510300 ETF 共用同一接口）。"""
    url = _API_INDEX_URL
    params = {
        "symbol": symbol,
        "scale": 240,
        "ma": 60,
        "datalen": datalen,
    }
    resp = fetch(url, params=params, timeout=15)
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


def fetch_index_daily(datalen: int = 4000, symbol: str = _API_HS300_SYMBOL) -> list[dict]:
    """拉取指数日 K（沪深300 默认；上证/其他标的传 symbol）。datalen 取接口上限内最大值
    （约 3451 条 ≈ 13.5 年），增量更新能补回数据基座停跑不超过十余年的历史缺口
    （原 250 天窗口停跑超 1 年即断档，sh510300 历史只有 258 条即此问题的遗留）。"""
    return _fetch_kline(symbol, datalen)


_API_ETF510300_SYMBOL = "sh510300"


def fetch_etf_daily(datalen: int = 4000) -> list[dict]:
    return _fetch_kline(_API_ETF510300_SYMBOL, datalen)


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
) -> tuple[str, str | None, list[dict]]:
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

    all_codes = filter_cooldown_targets("holdings", all_codes, "持仓")

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
                outcome = {"new_count": 0, "success": set(), "no_update": [], "failed": []}
                for code, (report_date, holdings), failed in results:
                    if failed:
                        outcome["failed"].append(code)
                        continue
                    outcome["success"].add(code)
                    if holdings and report_date and report_date != local_latest.get(code):
                        conn_.executemany(
                            "INSERT OR REPLACE INTO fund_holdings "
                            "(code, report_date, stock_code, stock_name, weight) "
                            "VALUES (?, ?, ?, ?, ?)",
                            [(code, report_date, h["stock_code"], h["stock_name"], h["weight"])
                             for h in holdings],
                        )
                        batch_rows += len(holdings)
                        funds_with_holdings += 1
                        local_latest[code] = report_date
                conn_.commit()
                outcome["new_count"] = batch_rows
                total_rows += batch_rows
                return outcome

            def _backfill_holdings(code) -> None:
                nonlocal total_rows, funds_with_holdings
                resp = fetch(
                    holdings_url,
                    params={"type": "jjcc", "code": code, "topline": "10", "year": "", "month": ""},
                    headers=_HOLDINGS_HEADERS, timeout=10,
                )
                report_date, holdings = _parse_holdings_html(resp.text)
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

            await run_batched_fetch(
                session, fetch_type="holdings", label="持仓",
                targets=all_codes, batch_size=batch_size, conn=conn,
                fetch_one=_fetch_holdings, handle_batch=_save_holdings_batch,
                backfill_one=_backfill_holdings, primary_note="持仓拉取失败",
            )
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
        # 持仓中尚未映射的股票（行业缺失 → RBSA 归为"其他"，影响特征质量）
        unmapped_cnt = conn.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT h.stock_code FROM fund_holdings h "
            "LEFT JOIN stock_industry_map i ON h.stock_code = i.stock_code "
            "WHERE i.stock_code IS NULL)"
        ).fetchone()[0]
        if not force:
            last_update_raw = meta_get(conn, META.INDUSTRY_MAP_UPDATED)
            if last_update_raw:
                last_update = datetime.strptime(last_update_raw, "%Y-%m-%d")
                if datetime.now() - last_update < timedelta(days=90) and unmapped_cnt == 0:
                    logger.info("行业映射距上次更新不足 90 天且无未映射股票，跳过")
                    return 0
            elif unmapped_cnt == 0:
                # 无更新记录且无待映射股票：无需更新
                return 0

        logger.info("正在拉取申万二级行业映射（未映射 %d 只）...", unmapped_cnt)
        try:
            # 非 force 走增量：只查未映射股票（持仓周更引入的新股票即时补齐）
            records = _fetch_industry_map(unmapped_only=not force)
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
        meta_set(conn, META.INDUSTRY_MAP_UPDATED, today)
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


def _push2_secid(stock_code: str) -> str:
    """东财 push2 secid：沪(6开头)=1.，港股(5位)=116.，深/北交(其余)=0.。"""
    if len(stock_code) == 5:
        return f"116.{stock_code}"
    if stock_code.startswith("6"):
        return f"1.{stock_code}"
    return f"0.{stock_code}"


def _fetch_industry_push2(stocks: list[str], results: dict[str, tuple[str, str]]) -> int:
    """用 push2 批量行情接口补行业分类（A 股 + 港股统一）。

    F10 接口（emweb）对云服务器 IP 反爬严格、并发高极易被限流（行业映射缺失的
    主要根因）；push2 ulist 批量接口一次请求多只（f12=代码, f100=东财行业），
    请求数少一个量级且走 TLS 指纹伪装，成功率更高。
    个别股票无行业字段（如部分港股）时按名称兜底。
    """
    added = 0
    batch_size = 80
    for i in range(0, len(stocks), batch_size):
        batch = stocks[i:i + batch_size]
        secids = ",".join(_push2_secid(c) for c in batch)
        try:
            resp = fetch(
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
                    logger.debug("股票 %s 行业映射(push2): %s", code, industry)
        except Exception as e:
            logger.debug("push2 批量行业查询失败: %s", str(e)[:120], exc_info=True)
    return added


def _fetch_industry_map(unmapped_only: bool = False) -> list[tuple[str, str, str]]:
    with db_conn() as conn:
        all_stocks = [
            r[0] for r in conn.execute(
                "SELECT DISTINCT stock_code FROM fund_holdings"
            ).fetchall()
        ]
        if unmapped_only:
            # 增量语义：只查尚未映射的股票（90 天全量重查由 force 路径触发）
            mapped = {r[0] for r in conn.execute("SELECT stock_code FROM stock_industry_map")}
            all_stocks = [s for s in all_stocks if s not in mapped]
    all_stocks = filter_cooldown_targets("industry_map", all_stocks, "行业映射")
    if not all_stocks:
        return []
    logger.info("需要查询 %d 只股票的行业分类...", len(all_stocks))

    # 架构深化候选 5：F10 批量并发/熔断/失败-冷却记录收敛进 ingest.run_batched_fetch 骨架
    #（不再自写第二套 gather+AsyncClient）；并发控制留在 fetch_one 内嵌 Semaphore
    #（与持仓路径同房式模式：骨架 batch_size 管批量粒度，fetch_one 管并发）
    # 并发 5：东财单 IP 实测阈值并发≈10，留一半余量；总速率由 fetchers 全局 QPS 闸门兜底
    semaphore = asyncio.Semaphore(5)
    results: dict[str, tuple[str, str]] = {}

    async def _fetch_one(session, stock_code: str) -> tuple[str, tuple[str, str] | None, bool]:
        """F10 单只查询；失败返回 failed（骨架统一记录并走兜底链）。"""
        async with semaphore:
            for url, params in _build_candidates(stock_code):
                try:
                    # 统一走 fetch_async（内部已含重试/退避/限流熔断），不再自写重试
                    resp = await fetch_async(session, url, params=params, timeout=15)
                    data = resp.json()
                    items = data.get("jbzl", [])
                    if items:
                        item = items[0]
                        em2016 = item.get("EM2016", "")
                        if em2016:
                            parts = em2016.split("-")
                            industry = parts[1] if len(parts) > 1 else parts[0]
                            return stock_code, (em2016, industry), False
                except Exception as e:
                    logger.warning("股票 %s 行业映射查询失败: %s", stock_code, e)
            return stock_code, None, True

    def _handle_batch(conn_, batch_results) -> dict:
        """收集 F10 结果到共享 results；不落库（入库由 update_industry_map 统一执行）。"""
        outcome = {"new_count": 0, "success": set(), "no_update": [], "failed": []}
        for code, payload, failed in batch_results:
            if failed:
                outcome["failed"].append(code)
                continue
            outcome["success"].add(code)
            results[code] = payload
        return outcome

    async def _run() -> dict:
        limits = httpx.Limits(max_connections=10, max_keepalive_connections=10)
        async with httpx.AsyncClient(
            limits=limits, verify=False, trust_env=False,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://emweb.securities.eastmoney.com/"},
        ) as session:
            # 回填后置：先走 push2 批量兜底，避免全失败时同步串行补查数千只
            return await run_batched_fetch(
                session, fetch_type="industry_map", label="行业映射",
                targets=all_stocks, batch_size=200,
                fetch_one=_fetch_one, handle_batch=_handle_batch,
                backfill_one=None,
                no_update_note="接口无行业数据", primary_note="行业映射拉取失败",
            )

    outcome = asyncio.run(_run())
    logger.info("首次行业查询完成: 成功 %d, 失败 %d",
                len(results), len(all_stocks) - len(results))

    # push2 批量兜底：F10 接口对云服务器 IP 反爬严格（行业映射缺失根因），
    # 未查到的股票统一走 push2 批量（一次 80 只、TLS 指纹伪装），请求数少一个量级
    failed = [s for s in all_stocks if s not in results]
    if failed:
        _push2_success = _fetch_industry_push2(failed, results)
        logger.info("push2 行业兜底: 新增 %d 条", _push2_success)
        if _push2_success:
            mark_recovered_batch("industry_map", [s for s in failed if s in results])

    # 剩余股票同步回填（F10 + push2 单只），轮次/失败记录由 run_backfill_rounds 统一
    failed = [s for s in all_stocks if s not in results]
    if failed:
        # 骨架熔断（批次失败率>50%）会让部分股票未进入批次、无 primary 记录——
        # 补记一次，保证冷却 attempts 按周期累积（与骨架已记录的不重复：仅补未处理者）
        handled = set(outcome.get("failed", []))
        for sc in failed:
            if sc not in handled:
                record_failure("industry_map", sc, "行业映射拉取失败", stage="primary")
        _headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://emweb.securities.eastmoney.com/"}

        def _backfill_one(sc: str) -> None:
            for url, params in _build_candidates(sc):
                try:
                    resp = fetch(url, params=params, headers=_headers, timeout=10)
                except Exception as e:
                    logger.debug("股票 %s 行业映射补查失败: %s", sc, str(e)[:120])
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
                        return
            # F10 候选接口均不可用 → push2 单只兑底（同步路径也走 TLS 伪装）
            try:
                resp = fetch(
                    "https://push2.eastmoney.com/api/qt/ulist.np/get",
                    {"secids": _push2_secid(sc), "fields": "f12,f14,f100",
                     "ut": "bd1d9ddb04089700cf9c27f6f7426281"},
                    timeout=10,
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
                        return
            except Exception as e:
                logger.debug("股票 %s 行业映射 push2 补查失败: %s", sc, str(e)[:120])
            # 候选接口均不可用或返回空：视为仍失败，交由 run_backfill_rounds 记录
            raise RuntimeError("行业映射补查失败（候选接口均不可用或返回空）")

        run_backfill_rounds("industry_map", failed, _backfill_one,
                            len(all_stocks), label="行业映射", rounds=2, delay=30)

    return [(sc, info[0], info[1]) for sc, info in results.items()]


def mark_short_history_funds() -> int:
    """数据不足打标：首条净值距今不足 60 天 → is_buyable=0。

    与特征计算最小窗口对齐（calc_features 净值 <60 天跳过特征）：不足 60 天
    历史的基金无法参与特征/赛道链路，留在候选池只会占用下载与计算资源；
    与停更打标同构：基金列表每周全表重建（save_fund_list 全置 1）后自动恢复，
    净值更新后本步骤重新按首条净值日期打标（候选/特征/训练查询均带 is_buyable=1，
    打标自动生效）。返回打标数量。
    """
    fresh: list[str] = []
    cutoff = (datetime.now().date() - timedelta(days=_MIN_NAV_DAYS)).isoformat()
    with db_conn() as conn:
        for code, first in conn.execute(
            "SELECT code, MIN(date) FROM fund_nav GROUP BY code").fetchall():
            if first and first > cutoff:
                fresh.append(code)
        if fresh:
            conn.executemany(
                "UPDATE fund_basic SET is_buyable = 0 WHERE code = ?", [(c,) for c in fresh])
    if fresh:
        logger.info("数据不足打标: %d 只基金首条净值距今不足 %d 天，is_buyable=0",
                    len(fresh), _MIN_NAV_DAYS)
    return len(fresh)


# ── 主流程 ──

_STALE_NAV_LAG_DAYS = 10
"""停更判定阈值（交易日）：净值日期滞后全局最新超该值视为停更，退出推荐/特征/训练池。"""


_MIN_NAV_DAYS = 60
"""候选池最小净值跨度（天）：首条净值距今不足该值视为数据不足，退出推荐/特征/训练池。

与特征计算最小窗口（calc_features 净值 <60 天跳过）对齐：
不足 60 天历史的基金连特征都算不了，不应占用候选池与下载资源。"""


def mark_stale_funds() -> int:
    """停更基金打标：净值日期滞后全局最新超 10 个交易日 → is_buyable=0。

    停更基金净值不更新、特征永远陈旧，应从推荐/特征/训练池退出（候选/特征/训练查询
    均带 is_buyable=1 过滤，打标自动生效）；基金列表每周全表重建（save_fund_list 全置 1）
    后自动恢复，仅屏蔽重建窗口内停更的基金。返回打标数量。
    """
    stale: list[str] = []
    with db_conn() as conn:
        global_max = conn.execute("SELECT MAX(date) FROM fund_nav").fetchone()[0]
        if not global_max:
            return 0
        dates = sorted(r[0] for r in conn.execute("SELECT DISTINCT date FROM fund_nav").fetchall())
        dates_set = set(dates)
        for code, latest in conn.execute(
            "SELECT code, MAX(date) FROM fund_nav GROUP BY code").fetchall():
            # 滞后判定单一来源：trading_day_lag（用净值实际日期集保持原口径，消除 O(N) 位置映射）
            if latest and trading_day_lag(latest, global_max, days=dates_set) > _STALE_NAV_LAG_DAYS:
                stale.append(code)
        if stale:
            conn.executemany(
                "UPDATE fund_basic SET is_buyable = 0 WHERE code = ?", [(c,) for c in stale])
    if stale:
        logger.info("停更打标: %d 只基金净值滞后超 %d 个交易日，is_buyable=0",
                    len(stale), _STALE_NAV_LAG_DAYS)
    return len(stale)


# ── 步骤语义单一来源（架构深化 D）──
# 原 pipeline._daily_data_steps 与 run_pipeline 内两套编号漂移，现收敛于此；
# 步骤 6（RBSA 统计）与 8（模型就绪检查）为特殊步骤，不进每日集合。
_STEP_FUND_LIST = 1      # 基金列表获取与过滤（周重建）
_STEP_NAV = 2            # 净值增量/全量下载 + 停更打标
_STEP_INDEX = 3          # 宏观指数（沪深300/上证/ETF）
_STEP_HOLDINGS = 4       # 重仓股 + 行业映射（成功后置位 holdings_last_run）
_STEP_RBSA_STATS = 6     # RBSA 行业暴露统计（仅日志）
_STEP_FEATURES = 7       # 特征计算
_STEP_MODEL_READY = 8    # 推荐模型就绪检查（仅日志）
ALL_STEPS = frozenset({_STEP_FUND_LIST, _STEP_NAV, _STEP_INDEX, _STEP_HOLDINGS,
                       _STEP_RBSA_STATS, _STEP_FEATURES, _STEP_MODEL_READY})
_HOLDINGS_INTERVAL_DAYS = 7


def daily_steps() -> list[int]:
    """每日数据基座步骤集合（编排单一来源，架构深化 D）。

    持仓/行业映射按 _HOLDINGS_INTERVAL_DAYS 天周期执行：距上次持仓 >7 天追加
    Step 4，否则仅基础步骤 [1,2,3,7]。置位由 Step 4 成功后写（run_pipeline），
    失败不更新、下次运行自动重试；首次部署无记录视为到期（触发自举）。
    原 pipeline._daily_data_steps 与此重复（两套编号漂移），现收敛于此。
    """
    with db_conn() as conn:
        last_raw = meta_get(conn, META.HOLDINGS_LAST_RUN)
    if last_raw:
        try:
            last = datetime.strptime(last_raw, "%Y-%m-%d").date()
            elapsed = (datetime.now().date() - last).days
        except ValueError:
            elapsed = _HOLDINGS_INTERVAL_DAYS + 1
    else:
        elapsed = _HOLDINGS_INTERVAL_DAYS + 1
    if elapsed > _HOLDINGS_INTERVAL_DAYS:
        return [1, 2, 3, _STEP_HOLDINGS, _STEP_FEATURES]
    return [1, 2, 3, _STEP_FEATURES]


def run_pipeline(steps: list[int] | None = None) -> None:
    all_steps = ALL_STEPS
    steps = steps or sorted(all_steps)

    with db_conn() as conn:

        if _STEP_FUND_LIST in steps:
            logger.info("=== Step 1: 基金列表获取与过滤 ===")
            t1 = time.time()
            update_fund_list_weekly()
            logger.info("Step1 基金列表完成 (%.0fms)", (time.time() - t1) * 1000)

        if _STEP_NAV in steps:
            has_nav = conn.execute("SELECT 1 FROM fund_nav LIMIT 1").fetchone()
            if has_nav:
                logger.info("=== Step 2: 净值增量更新（并发增量）===")
                t2 = time.time()
                total_new = asyncio.run(async_update_nav_incremental(concurrency=10))
            else:
                logger.info("=== Step 2: 净值首次全量下载（pingzhongdata 高并发）===")
                t2 = time.time()
                total_new = asyncio.run(async_download_all_nav(concurrency=15))
            logger.info("Step2 净值更新完成: %d 条 (%.0fms)",
                        total_new, (time.time() - t2) * 1000)
            # 净值更新后立即打标停更/数据不足基金：让后续持仓/特征/推荐步骤自动跳过（查询均带 is_buyable=1）
            mark_stale_funds()
            mark_short_history_funds()

        if _STEP_INDEX in steps:
            logger.info("=== Step 3: 宏观指数获取 ===")
            try:
                index_data = fetch_index_daily(datalen=4000)
                n = save_index_daily("sh000300", index_data)
                logger.info("沪深300日线新增 %d 条", n)
            except Exception as e:
                logger.error("沪深300 日线获取失败: %s", str(e)[:120], exc_info=True)
            try:
                sse_data = fetch_index_daily(datalen=4000, symbol="sh000001")
                n_sse = save_index_daily("sh000001", sse_data)
                logger.info("上证指数日线新增 %d 条", n_sse)
            except Exception as e:
                logger.error("上证指数 日线获取失败: %s", str(e)[:120], exc_info=True)
            try:
                etf_data = fetch_etf_daily(datalen=4000)
                n_etf = save_index_daily("sh510300", etf_data)
                logger.info("沪深300ETF(510300)日线新增 %d 条", n_etf)
            except Exception as e:
                logger.error("沪深300ETF 日线获取失败: %s", str(e)[:120], exc_info=True)

        if _STEP_HOLDINGS in steps:
            logger.info("=== Step 4: 重仓股数据获取 ===")
            asyncio.run(async_download_all_holdings())
            logger.info("更新申万行业映射（持仓→行业）...")
            total_mapped = update_industry_map()
            logger.info("行业映射完成: %d 条", total_mapped)
            # Step 4 成功后才置位持仓周期标记：失败不更新，下次运行自动重试
            # （此前在 pipeline 提前置位，失败也被记作"今天已跑"）
            meta_set(conn, META.HOLDINGS_LAST_RUN, datetime.now().strftime("%Y-%m-%d"))

        if _STEP_RBSA_STATS in steps:
            logger.info("=== Step 6: RBSA 行业暴露 ===")
            mapped = conn.execute(
                "SELECT COUNT(*) FROM stock_industry_map"
            ).fetchone()[0]
            holdings_funds = conn.execute(
                "SELECT COUNT(DISTINCT code) FROM fund_holdings"
            ).fetchone()[0]
            logger.info("stock_industry_map: %d 条, fund_holdings 覆盖: %d 只基金",
                        mapped, holdings_funds)

        if _STEP_FEATURES in steps:
            logger.info("=== Step 7: 特征计算 ===")
            _features.calc_all_features()

        if _STEP_MODEL_READY in steps:
            logger.info("=== Step 8: 推荐引擎 ===")
            # 重训判定 / 路径 / 训练全部收敛进模型 seam（app/model.py），管线不自行判断
            from app.model import get_or_train
            model = get_or_train()
            if model is None:
                logger.error("无可用模型，推荐引擎跳过")
            else:
                logger.info("模型已就绪")

    logger.info("数据基座流程完成")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--step":
        step_num = int(sys.argv[2])
        if step_num not in ALL_STEPS:
            logger.error("未知步骤 %s，可选: %s", step_num, sorted(ALL_STEPS))
        else:
            run_pipeline(steps=[step_num])
    elif len(sys.argv) > 1 and sys.argv[1] == "--async-nav":
        concurrency = int(sys.argv[2]) if len(sys.argv) > 2 else 6
        asyncio.run(async_download_all_nav(concurrency=concurrency))
    elif len(sys.argv) > 1 and sys.argv[1] == "--update-nav":
        concurrency = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 6
        asyncio.run(async_update_nav_incremental(concurrency=concurrency))
    elif len(sys.argv) > 1 and sys.argv[1] == "--holdings":
        concurrency = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 6
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

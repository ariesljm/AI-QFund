"""数据基座行业映射 module（架构深化 ⑨ 拆分）：行业映射抓取/维护。

从 foundation.py 拆出：行业映射维护是自洽概念，独立可测（test_industry_map）。
"""

import asyncio
from datetime import datetime, timedelta

import httpx

from app.data.fetchers import fetch, fetch_async
from app.data.ingest import filter_cooldown_targets, run_batched_fetch
from app.data.store import (
    STAGE_PRIMARY,
    mark_recovered_batch,
    record_failure,
    run_backfill_rounds,
    save_industry_map,
)
from app.database import db_conn
from app.repo import meta_keys as META
from app.repo.base import (
    get_industry_map_gap_count,
    get_industry_map_targets,
    get_meta,
    save_meta,
)
from app.utils.log import get_logger

logger = get_logger("data.industry_map")

def update_industry_map(force: bool = False) -> int:
    with db_conn() as conn:
        # 持仓中尚未映射的股票（行业缺失 → RBSA 归为"其他"，影响特征质量）
        unmapped_cnt = get_industry_map_gap_count()
        if not force:
            last_update_raw = get_meta(META.INDUSTRY_MAP_UPDATED)
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
        save_industry_map(conn, records)
        save_meta(META.INDUSTRY_MAP_UPDATED, today)
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
    all_stocks, mapped = get_industry_map_targets()
    if unmapped_only:
        # 增量语义：只查尚未映射的股票（90 天全量重查由 force 路径触发）
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
        success: set[str] = set()
        failed_codes: list[str] = []
        for code, payload, failed in batch_results:
            if failed:
                failed_codes.append(code)
                continue
            success.add(code)
            results[code] = payload
        return {"new_count": 0, "success": success, "no_update": [], "failed": failed_codes}

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
                record_failure("industry_map", sc, "行业映射拉取失败", stage=STAGE_PRIMARY)
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



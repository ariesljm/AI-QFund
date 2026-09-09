"""数据基座：基金列表、净值、指数、持仓、行业映射、RBSA。"""

import asyncio
import json
import re
import sqlite3
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from app.data.fetchers import fetch
from app.data.holdings import async_download_all_holdings
from app.data.industry_map import update_industry_map
from app.data.nav import async_download_all_nav, async_update_nav_incremental
from app.data.store import (
    mark_funds_unbuyable,
    save_fund_list,
    save_index_daily,
)
from app.database import DB_PATH, db_conn
from app.features import calculator as _features
from app.features.calculator import EMA_WARMUP_NAVS
from app.repo import meta_keys as META
from app.repo.base import (
    get_index_rows,
    get_industry_map_stats,
    get_interval_days,
    get_meta,
    get_nav_time_state,
    has_index_data,
    has_nav_data,
    save_meta,
)
from app.utils.log import get_logger
from app.utils.trading_calendar import trading_day_lag  # 滞后交易日数单一来源

logger = get_logger(__name__)

_API_FUND_LIST_URL = "https://fund.eastmoney.com/js/fundcode_search.js"
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
    last = get_meta(META.FUND_LIST_LAST_UPDATE)
    if last and not force:
        age_days = get_interval_days(META.FUND_LIST_LAST_UPDATE)
        if age_days is not None and age_days < _LIST_UPDATE_INTERVAL_DAYS:
                logger.info("基金列表 %d 天前更新过（<%d 天），跳过",
                            age_days, _LIST_UPDATE_INTERVAL_DAYS)
                return -1
    funds = fetch_fund_list()
    if not funds:
        # 空列表守卫：源解析失败/接口异常返回空时拒绝落库（save_fund_list 是
        # 全表 DELETE 后重建，空列表入库会清空候选池且 7 天内不自愈）。
        # 不置位周更时间戳，下次运行自动重试；已有数据不受本次失败影响。
        logger.error("基金列表拉取结果为空（源解析失败或接口异常），保留现有 fund_basic 不变，"
                     "等待下次运行重试")
        return 0
    n = save_fund_list(funds)
    save_meta(META.FUND_LIST_LAST_UPDATE, datetime.now().strftime("%Y-%m-%d"))
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


# 指数增量窗口：本地已有数据时每日拉取条数。需覆盖：特征计算 idx_ma60/bias_60d
# 回看 60 条、EMA60 预热、指数动量 21 日——120 条留足余量。
_INDEX_INCREMENT_WINDOW = 120
# 首次/空表时拉取上限内最大值（约 3451 条 ≈ 13.5 年），与原行为一致。
_INDEX_BACKFILL_WINDOW = 4000





# ── 持仓数据 ──

def mark_short_history_funds() -> int:
    """数据不足打标：自身净值条数 < EMA_WARMUP_NAVS（span60+confirm2=62）→ is_buyable=0。

    与监控趋势防线预热（ema60_exit 需 span+confirm 条净值）及特征最小窗口
    （calc_features 净值 <60 条跳过）同源对齐：原按 60 自然日判定（约仅 40 个
    交易日），基金入池后特征被静默跳过、趋势防线无法生效。改按基金自身净值
    条数计数，单一口径。基金列表每周全表重建后自动恢复；返回打标数量。
    """
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT code FROM fund_basic WHERE is_buyable = 1 AND code IN ("
            "  SELECT code FROM fund_nav GROUP BY code HAVING COUNT(*) < ?)",
            (EMA_WARMUP_NAVS,),
        ).fetchall()
    fresh = [r[0] for r in rows]
    if fresh:
        mark_funds_unbuyable(fresh)
        logger.info("数据不足打标: %d 只基金净值不足 %d 条，is_buyable=0",
                    len(fresh), EMA_WARMUP_NAVS)
    return len(fresh)


# ── 主流程 ──

_STALE_NAV_LAG_DAYS = 10
"""停更判定阈值（交易日）：净值日期滞后全局最新超该值视为停更，退出推荐/特征/训练池。"""


def mark_stale_funds() -> int:
    """停更基金打标：净值日期滞后全局最新超 10 个交易日 → is_buyable=0。

    停更基金净值不更新、特征永远陈旧，应从推荐/特征/训练池退出（候选/特征/训练查询
    均带 is_buyable=1 过滤，打标自动生效）；基金列表每周全表重建（save_fund_list 全置 1）
    后自动恢复，仅屏蔽重建窗口内停更的基金。返回打标数量。
    """
    stale: list[str] = []
    ranges, dates = get_nav_time_state()
    if not ranges:
        return 0
    global_max = max(v[1] for v in ranges.values() if v[1])
    if not global_max:
        return 0
    dates_set = set(dates)
    for code, (_first, latest) in ranges.items():
        # 滞后判定单一来源：trading_day_lag（用净值实际日期集保持原口径）
        if latest and trading_day_lag(latest, global_max, days=dates_set) > _STALE_NAV_LAG_DAYS:
            stale.append(code)
    if stale:
        mark_funds_unbuyable(stale)
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

# ── 数据基座 Step 注册表（2026-09 架构深化 候选3）──
# step 状态语义（成功才置位 / 后置打标 / 失败各自 try）此前散在 run_pipeline 各 if 块：
# pipeline 自愈硬编码 steps=[4]、CLI --step 靠 ALL_STEPS 校验，调用者被迫内隐
# "哪步该重跑、失败会怎样"。注册表把 执行→成功后置 显式化，run_pipeline / 自愈 /
# CLI 消费同一份定义；on_success 只在 run 无异常后执行（失败不置位、下次自动重试）。

@dataclass(frozen=True)
class PipelineStep:
    """数据基座单步定义：执行体 + 成功后置动作（on_success）。"""
    id: int
    name: str
    run: Callable[[], None]
    on_success: tuple[Callable[[], None], ...] = ()


def _step_fund_list() -> None:
    """Step 1：基金列表获取与过滤（周重建，内部按间隔幂等）。"""
    update_fund_list_weekly()


def _step_nav() -> None:
    """Step 2：净值增量/首次全量下载 + 停更/短历史打标（后置）。"""
    t = time.time()
    if has_nav_data():
        total_new = asyncio.run(async_update_nav_incremental(concurrency=10))
    else:
        total_new = asyncio.run(async_download_all_nav(concurrency=15))
    logger.info("净值更新完成: %d 条 (%.0fms)", total_new, (time.time() - t) * 1000)


def _mark_stale_after_nav() -> None:
    """Step 2 后置：净值更新后立即打标停更/数据不足基金，让后续步骤自动跳过。"""
    mark_stale_funds()
    mark_short_history_funds()


def _step_index() -> None:
    """Step 3：宏观指数（沪深300/上证/ETF），三标的各自 try（单个失败不中断）。"""
    # 增量窗口 120 条（覆盖 EMA60 预热 + 特征/回测最大回看窗口，含余量）：
    # 本地已有数据时无需拉 13.5 年全量；历史断档由 --index-backfill 显式补拉。
    datalen = _INDEX_INCREMENT_WINDOW if has_index_data() else _INDEX_BACKFILL_WINDOW
    for symbol, label in [("sh000300", "沪深300"), ("sh000001", "上证指数"),
                          ("sh510300", "沪深300ETF")]:
        try:
            if symbol == "sh510300":
                data = fetch_etf_daily(datalen=datalen)
            else:
                data = fetch_index_daily(datalen=datalen, symbol=symbol)
            n = save_index_daily(symbol, data)
            logger.info("%s日线新增 %d 条", label, n)
        except Exception as e:
            logger.error("%s 日线获取失败: %s", label, str(e)[:120], exc_info=True)


def _check_index_freshness(threshold: int = 3) -> None:
    """Step 3 后置：指数新鲜度核查（审计 P1-2）——缺口静默破坏 EMA60/regime/市场状态列。

    与净值不同，指数断档此前无显式告警；超阈值 error 级日志并记 meta（INDEX_FRESHNESS）。
    无交易日历缓存不误报。
    """
    rows = get_index_rows("sh000300")
    if not rows:
        logger.error("指数数据完全缺失（index_daily 无 sh000300）——EMA60/regime/市场状态列"
                     "全部失真，请运行 python -m app.data.foundation --index-backfill")
        return
    latest = rows[-1][0]
    raw = get_meta(META.TRADE_DATES_CACHE)
    if not raw:
        return
    try:
        days = set(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        return
    today = datetime.now().date().isoformat()
    if today in days:
        expected = max((d for d in days if d < today), default=None)
    else:
        expected = max((d for d in days if d <= today), default=None)
    if expected is None or latest >= expected:
        return
    lag = trading_day_lag(latest, expected, days=days)
    if lag < threshold:
        return
    logger.error("指数新鲜度: 本地最新 %s 滞后 %d 个交易日(>%d)——EMA60/regime/市场状态列"
                 "可能失真；停跑超 ~6 个月的历史缺口需手动 --index-backfill", latest, lag, threshold)
    try:
        save_meta(META.INDEX_FRESHNESS, json.dumps(
            {"latest": latest, "lag": lag, "threshold": threshold,
             "at": datetime.now().strftime("%Y-%m-%d")}, ensure_ascii=False))
    except Exception as e:
        logger.warning("指数新鲜度 meta 记录失败: %s", str(e)[:100])


def _step_holdings() -> None:
    """Step 4：重仓股下载 + 行业映射（成功后置位 holdings_last_run，失败不置位）。"""
    asyncio.run(async_download_all_holdings())
    logger.info("更新申万行业映射（持仓→行业）...")
    total_mapped = update_industry_map()
    logger.info("行业映射完成: %d 条", total_mapped)


def _mark_holdings_run() -> None:
    """Step 4 后置：成功才置位持仓周期标记（失败不更新，下次运行自动重试）。"""
    save_meta(META.HOLDINGS_LAST_RUN, datetime.now().strftime("%Y-%m-%d"))


def _step_rbsa_stats() -> None:
    """Step 6：RBSA 行业暴露统计（仅日志）。"""
    mapped, holdings_funds = get_industry_map_stats()
    logger.info("stock_industry_map: %d 条, fund_holdings 覆盖: %d 只基金",
                mapped, holdings_funds)


def _step_features() -> None:
    """Step 7：特征计算。"""
    _features.calc_all_features()


def _step_model_ready() -> None:
    """Step 8：推荐模型就绪检查（重训判定收敛进模型 seam，管线不自行判断）。"""
    from app.model import get_or_train
    model = get_or_train()
    if model is None:
        logger.error("无可用模型，推荐引擎跳过")
    else:
        logger.info("模型已就绪")


STEP_REGISTRY: dict[int, PipelineStep] = {
    _STEP_FUND_LIST: PipelineStep(_STEP_FUND_LIST, "基金列表获取与过滤", _step_fund_list),
    _STEP_NAV: PipelineStep(_STEP_NAV, "净值更新与停更打标", _step_nav,
                            on_success=(_mark_stale_after_nav,)),
    _STEP_INDEX: PipelineStep(_STEP_INDEX, "宏观指数获取", _step_index,
                              on_success=(_check_index_freshness,)),
    _STEP_HOLDINGS: PipelineStep(_STEP_HOLDINGS, "重仓股与行业映射", _step_holdings,
                                 on_success=(_mark_holdings_run,)),
    _STEP_RBSA_STATS: PipelineStep(_STEP_RBSA_STATS, "RBSA 行业暴露统计", _step_rbsa_stats),
    _STEP_FEATURES: PipelineStep(_STEP_FEATURES, "特征计算", _step_features),
    _STEP_MODEL_READY: PipelineStep(_STEP_MODEL_READY, "模型就绪检查", _step_model_ready),
}


def run_pipeline(steps: list[int] | None = None) -> None:
    """数据基座主入口：按注册表编排 step（架构深化 候选3）。

    step 定义/失败语义/成功置位收敛到 STEP_REGISTRY；本函数只负责按序执行
    + on_success（run 无异常才执行）。自愈（pipeline.py）与 CLI（--step）
    消费同一注册表，调用者不再内隐"哪步该重跑、失败会怎样"。
    """
    selected = sorted(ALL_STEPS) if steps is None else steps
    for sid in sorted(set(selected) & set(STEP_REGISTRY)):
        step = STEP_REGISTRY[sid]
        logger.info("=== Step %d: %s ===", step.id, step.name)
        t = time.time()
        step.run()
        for post in step.on_success:
            post()
        logger.info("Step%d %s完成 (%.0fms)", step.id, step.name, (time.time() - t) * 1000)
    logger.info("数据基座流程完成")
_HOLDINGS_INTERVAL_DAYS = 7
"""无持仓披露冷却（天）：ETF联接/商品基金等在 fundf10 本就无重仓股披露，
拉到空属常态而非故障。季报频率下月度重试足够——满 3 个周期后进 30 天冷却，
避免每周对千余只无披露基金反复请求；基金后续首次披露时由 mark_recovered_batch 自动恢复。"""


def daily_steps() -> list[int]:
    """每日数据基座步骤集合（编排单一来源，架构深化 D）。

    持仓/行业映射按 _HOLDINGS_INTERVAL_DAYS 天周期执行：距上次持仓 >7 天追加
    Step 4，否则仅基础步骤 [1,2,3,7]。置位由 Step 4 成功后写（run_pipeline），
    失败不更新、下次运行自动重试；首次部署无记录视为到期（触发自举）。
    原 pipeline._daily_data_steps 与此重复（两套编号漂移），现收敛于此。
    """
    # 窄读收敛（ADR-0005 残留收尾）：无记录/解析失败 → None → 视为到期
    elapsed = get_interval_days(META.HOLDINGS_LAST_RUN)
    if elapsed is None:
        elapsed = _HOLDINGS_INTERVAL_DAYS + 1
    if elapsed > _HOLDINGS_INTERVAL_DAYS:
        return [1, 2, 3, _STEP_HOLDINGS, _STEP_FEATURES]
    return [1, 2, 3, _STEP_FEATURES]

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
    elif len(sys.argv) > 1 and sys.argv[1] == "--index-backfill":
        # 历史断档补拉：Step3 每日增量窗口只有 120 条，停跑超窗口的历史缺口用本命令补
        for sym in (_API_HS300_SYMBOL, "sh000001", _API_ETF510300_SYMBOL):
            try:
                data = fetch_index_daily(datalen=_INDEX_BACKFILL_WINDOW, symbol=sym)
                n = save_index_daily(sym, data)
                logger.info("%s 历史补拉: 新增 %d 条", sym, n)
            except Exception as e:
                logger.error("%s 历史补拉失败: %s", sym, str(e)[:120], exc_info=True)
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

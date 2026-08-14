"""宏观数据获取 module（底层数据域）：抓取板块行情 / 资金流 / 财经新闻并入库。

对外提供 fetch_macro_inputs 聚合入口，一次抓取当日全部宏观输入；
细粒度函数（load_board_sectors / fetch_flow / fetch_news）供独立测试与复用。
解析与入库逻辑集中在数据域，供推荐决策域（macro_agent）经 seam 消费。
"""

import json
import time
from dataclasses import dataclass
from app.utils.log import get_logger
from app.data.fetchers import fetch as _fetch
from app.features.sector import is_industry_code
import app.repo as repo

logger = get_logger("data.macro")

_BOARD_URL = "https://push2ex.eastmoney.com/getAllBKChanges?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wzchanges&pageindex=0&pagesize=500"
_EM_NEWS_URL = "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns"

_PSEUDO_PREFIXES = ("昨日", "当日", "今日")

_CONCEPT_CODES = frozenset({
    "BK0490", "BK0492", "BK0493", "BK0494",
    "BK0498", "BK0499", "BK0501", "BK0505", "BK0506",
    "BK0509", "BK0511", "BK0514", "BK0519", "BK0523",
    "BK0525", "BK0528", "BK0534", "BK0535", "BK0536",
    "BK0548", "BK0549", "BK0552", "BK0554",
    "BK0728", "BK0742", "BK0743",
    "BK1022", "BK1023", "BK1024", "BK1025",
    "BK1047", "BK1048",
    "BK1204",
})


@dataclass
class MacroInputs:
    """一次宏观数据抓取的完整结果：板块行情 + 资金流 + 财经新闻。"""
    board_sectors: list[dict]
    flow: dict
    news: dict


def fetch_macro_inputs(date_str: str) -> MacroInputs:
    """聚合抓取当日宏观输入：板块 → 资金流 → 新闻，全部入库后返回。

    新闻/资金流/板块均为当天数据；推荐引擎每日调用一次，实时抓取成本可接受。
    """
    sectors = load_board_sectors()
    flow = fetch_flow(date_str, sectors)
    news = fetch_news(date_str, sectors)
    return MacroInputs(board_sectors=sectors, flow=flow, news=news)


def _http_get(url: str, timeout: float = 12) -> str:
    return _fetch(url, timeout=timeout).text


def _is_pseudo_sector(name: str) -> bool:
    """排除伪板块：含下划线的系统分类，或以 昨日/当日/今日 开头的技术形态分类"""
    if not name:
        return True
    if '_' in name:
        return True
    return name.startswith(_PSEUDO_PREFIXES)


def _is_concept_name(name: str, code: str = "") -> bool:
    """判断板块是否为概念/风格/指数类（精确BK代码列表）。"""
    if code and code in _CONCEPT_CODES:
        return True
    if not name:
        return True
    return False


def load_board_sectors() -> list[dict]:
    """加载东方财富板块行情数据，返回过滤后的有效行业板块列表。"""
    txt = _http_get(_BOARD_URL)
    data = json.loads(txt)
    allbk = (data.get("data") or {}).get("allbk", [])
    return [
        b for b in allbk
        if not _is_pseudo_sector(b.get('n', '') or '')
        and is_industry_code(b.get('c', '') or '')
        and bool((b.get('n') or '').strip())
    ]


def fetch_em_finance_news(date_str: str, retries: int = 3) -> list[dict]:
    """抓取东方财富「财经要闻」栏目（column=346）当天的新闻。

    返回: [{"time": "HH:MM", "title": "...", "summary": "..."}]
    优先取 date_str 当天；跨日运行（凌晨）当天新闻未生成时回退到最近交易日（T-1），
    与板块/资金流数据同口径。避免全量 7×24 新闻造成的上下文过大。

    抓取失败时自动重试，重试耗尽仍失败则抛异常终止推荐管线，
    避免用旧/空数据兜底导致推荐结果失真。
    """
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        entries: list[dict] = []
        seen_titles: set[str] = set()
        # 目标新闻日期：第一页确定后整轮沿用（当天有新闻用当天，否则回退最新日期）
        target_day = date_str
        try:
            for page in range(1, 4):  # 单日新闻较多时翻页，最多 3 页兜底
                url = (
                    f"{_EM_NEWS_URL}?client=web&biz=web_news_col&column=346"
                    f"&order=1&needInteractData=0&page_index={page}&page_size=50&req_trace=1"
                )
                txt = _http_get(url, timeout=15)
                data = json.loads(txt)
                items = (data.get("data") or {}).get("list") or []
                if not items:
                    break
                if page == 1:
                    # 以本页「最大日期」判定当天是否有新闻（而非仅看首条——首条可能
                    # 是昨日深夜/置顶新闻，误判会丢弃列表中的当天新闻）；跨日运行
                    # （凌晨）时回退到接口实际返回的最新日期，与板块/资金流同口径。
                    latest_day = max((it.get("showTime") or "")[:10] for it in items)
                    if latest_day >= date_str:
                        target_day = date_str
                    else:
                        target_day = latest_day
                        logger.info("跨日运行：当天(%s)新闻尚未生成，回退到最近交易日 %s 新闻",
                                    date_str, target_day)
                # 后续页沿用 target_day：page>1 时本页首条已不是 target_day，
                # 说明已取完（倒序）；第一页即使首条是昨日深夜新闻也不中断（混排场景）
                if page > 1 and not (items[0].get("showTime") or "").startswith(target_day):
                    break
                for it in items:
                    show_time = it.get("showTime") or ""
                    if not show_time.startswith(target_day):
                        continue
                    title = (it.get("title") or "").strip()
                    if not title or title in seen_titles:
                        continue
                    seen_titles.add(title)
                    entries.append({
                        "time": show_time[11:16],
                        "title": title,
                        "summary": (it.get("summary") or "").strip(),
                    })
                if len(items) < 50:
                    break
            return entries
        except Exception as e:
            last_err = e
            logger.warning("东方财富财经要闻抓取失败(第%d/%d次): %s",
                           attempt, retries, str(e)[:120])
            if attempt < retries:
                time.sleep(1.5 * attempt)
    raise RuntimeError(f"东方财富财经要闻连续{retries}次抓取失败，终止推荐: {last_err}")


def fetch_news(date_str: str, sectors: list) -> dict:
    """抓取板块排行 + 东方财富财经要闻，写入 macro_news。sectors 由 fetch_macro_inputs 一次性抓取。"""
    top_gainers = top_losers = etf_net_flow = ""
    try:
        if sectors:
            sorted_by_chg = sorted(sectors, key=lambda x: float(x.get("u", 0) or 0), reverse=True)
            gainers = sorted_by_chg[:9]
            losers = sorted_by_chg[-5:][::-1]
            top_gainers = "、".join(f"{d['n']}({float(d.get('u',0)or 0):+.2f}%)" for d in gainers)
            top_losers = "、".join(f"{d['n']}({float(d.get('u',0)or 0):+.2f}%)" for d in losers)
            by_flow = max(sectors, key=lambda x: float(x.get("zjl", 0) or 0)) if sectors else None
            etf_net_flow = f"{by_flow['n']}: {float(by_flow.get('zjl',0)or 0):,.0f}元" if by_flow else ""
    except Exception as e:
        logger.warning("板块排行抓取失败: %s", str(e)[:120], exc_info=True)

    em_entries = fetch_em_finance_news(date_str)

    news = ""
    if em_entries:
        lines = []
        for e in em_entries:
            line = f"[{e['time']}] {e['title']}"
            if e.get("summary"):
                line += f"：{e['summary']}"
            lines.append(line)
        news = "\n".join(lines)

    if top_gainers or top_losers or em_entries:
        repo.save_macro_news(date_str, news, top_gainers, top_losers, etf_net_flow)
    logger.info("快讯入库: 领涨[%s] 领跌[%s] 东财要闻=%d条",
                top_gainers[:40], top_losers[:40], len(em_entries))
    return {
        "summary": news, "top_gainers": top_gainers,
        "top_losers": top_losers, "etf_net_flow": etf_net_flow,
        "em_entries": em_entries,
    }


def fetch_flow(date_str: str, sectors: list) -> dict:
    """抓取行业板块资金流排名（主力净流入/涨跌），排除概念/风格板块。"""
    result = {"summary": ""}
    try:
        if not sectors:
            return result
        real_sectors = [s for s in sectors if not _is_concept_name(s.get("n", "") or "", s.get("c", "") or "")]
        if not real_sectors:
            real_sectors = sectors
        sorted_by_flow = sorted(real_sectors, key=lambda x: float(x.get("zjl", 0) or 0), reverse=True)
        lines = []
        for d in sorted_by_flow[:10]:
            name = d.get("n", "")
            flow_val = float(d.get("zjl", 0) or 0)
            chg = float(d.get("u", 0) or 0)
            lines.append(f"{name}: 主力净流入{flow_val:,.0f}万元, 涨跌{chg:+.2f}%")
        result["summary"] = "\n".join(lines)
        result["top_flows"] = [
            {"name": d.get("n", ""), "flow": float(d.get("zjl", 0) or 0), "pct": d.get("u", "")}
            for d in sorted_by_flow[:5]
        ]
        result["top_outflows"] = [
            {"name": d.get("n", ""), "flow": float(d.get("zjl", 0) or 0), "pct": d.get("u", "")}
            for d in reversed(sorted_by_flow[-5:])
        ]
        # 全市场主力资金净额（所有筛选行业的净流入加总，单位万元）：
        # UI 展示的"净额"应是市场整体口径，而非流入/流出 top N 的部分差。
        result["total_net"] = sum(
            float(d.get("zjl", 0) or 0) for d in real_sectors
        )
        repo.save_flow_data(date_str, result)
        # 全行业板块每日快照：量化定池面板数据源（只存行业口径，与资金流同源）
        repo.save_sector_snapshot(date_str, real_sectors)
        logger.info("资金流已获取: 筛选后申万行业%d个, 板块快照%d条", len(sectors), len(real_sectors))
    except Exception as e:
        logger.warning("资金流抓取失败: %s", str(e)[:120], exc_info=True)
    return result

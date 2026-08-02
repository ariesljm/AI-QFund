"""宏观分析 agent：多源数据 → LLM 选赛道 → MacroContext。

输出供 recommend.py（推荐）和 monitor.py（监控）直接消费。
"""

import json
import logging
import time
from app.utils.log import get_logger
import re as _re
from dataclasses import dataclass, field, asdict
from datetime import datetime

from app.llm.client import call_llm
from app.llm.prompts import (sector_selection_prompt, sector_selection_system_prompt,
                             news_brief_prompt)
from app.database import db_conn as _db_conn  # 保留用于 ensure_column/迁移
import app.repo as repo
from app.data.fetchers import fetch as _fetch
from app.features.sector import is_industry_code, is_industry_name

logger = get_logger("macro_agent")

_BOARD_URL = "https://push2ex.eastmoney.com/getAllBKChanges?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wzchanges&pageindex=0&pagesize=500"
_EM_NEWS_URL = "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns"


@dataclass(frozen=True)
class MacroContext:
    news_summary: str = ""
    news_brief: str = ""
    recommended_sectors: list[str] = field(default_factory=list)
    risk_sectors: list[str] = field(default_factory=list)
    sector_reasoning: str = ""
    regime_label: str = "neutral"
    cls_stock_mentions: list[dict] = field(default_factory=list)
    date: str = ""
    top_flows: list[dict] = field(default_factory=list)
    top_outflows: list[dict] = field(default_factory=list)


def build_macro_context(date_str: str | None = None, force: bool = False) -> MacroContext:
    """聚合多源数据 + LLM 选赛道，返回宏观上下文。

    缓存先行：命中当天缓存直接返回（不再白抓网络）。
    force=True 时跳过缓存，强制实时抓取新闻+LLM 重新选赛道。
    板块行情一次性抓取，供资金流与新闻复用，避免同一接口重复请求。
    """
    _ensure_column()
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")

    if not force:
        cached = _load_cache(date_str)
        if cached is not None:
            return cached

    sectors = _load_board_sectors()

    flow = _fetch_flow(date_str, sectors)

    news = _fetch_news(date_str, sectors)

    news_brief = _summarize_news(news.get("summary", ""))

    ctx = _suggest_sectors(date_str, news, flow, news_brief)
    if not ctx.recommended_sectors:
        # 空赛道是合法决策（空推荐日）：仅缓存当日，按日期隔离不污染后续
        logger.info("LLM 显式判定今日无合适赛道，作为空推荐日处理")
    _save_cache(ctx)
    return ctx


# ── DB 缓存 ──

def _ensure_column():
    with _db_conn() as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(macro_news)").fetchall()}
        if "context_json" not in cols:
            conn.execute("ALTER TABLE macro_news ADD COLUMN context_json TEXT")
            conn.commit()


def _load_cache(date_str: str) -> MacroContext | None:
    d = repo.get_cached_context(date_str)
    if d:
        try:
            return MacroContext(**{k: v for k, v in d.items() if k in MacroContext.__dataclass_fields__})
        except Exception as e:
            logger.warning("宏观缓存解析失败: %s", str(e)[:120], exc_info=True)
    return None


def _save_cache(ctx: MacroContext) -> None:
    repo.save_context(ctx.date, asdict(ctx))


# ── 数据源 ──

def _http_get(url: str, timeout: float = 12) -> str:
    return _fetch(url, timeout=timeout).text


def _fetch_em_finance_news(date_str: str, retries: int = 3) -> list[dict]:
    """抓取东方财富「财经要闻」栏目（column=346）当天的新闻。

    返回: [{"time": "HH:MM", "title": "...", "summary": "..."}]
    只取当天新闻，避免全量 7×24 新闻造成的上下文过大。

    抓取失败时自动重试，重试耗尽仍失败则抛异常终止推荐管线，
    避免用旧/空数据兜底导致推荐结果失真。
    """
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        entries: list[dict] = []
        seen_titles: set[str] = set()
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
                # 接口按时间倒序返回，若当前页首条已不是当天，说明当天新闻已取完
                first_time = (items[0].get("showTime") or "")
                if not first_time.startswith(date_str):
                    break
                for it in items:
                    show_time = it.get("showTime") or ""
                    if not show_time.startswith(date_str):
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


_PSEUDO_PREFIXES = ("昨日", "当日", "今日")


def _is_pseudo_sector(name: str) -> bool:
    """排除伪板块：含下划线的系统分类，或以 昨日/当日/今日 开头的技术形态分类"""
    if not name:
        return True
    if '_' in name:
        return True
    return name.startswith(_PSEUDO_PREFIXES)


def _load_board_sectors() -> list[dict]:
    """加载东方财富板块行情数据，返回过滤后的有效行业板块列表。"""
    txt = _http_get(_BOARD_URL)
    data = json.loads(txt)
    allbk = (data.get("data") or {}).get("allbk", [])
    return [
        b for b in allbk
        if not _is_pseudo_sector(b.get('n', '') or '')
        and is_industry_code(b.get('c', '') or '')
        and is_industry_name(b.get('n', '') or '')
    ]


def _fetch_news(date_str: str, sectors: list) -> dict:
    """抓取板块排行 + 东方财富财经要闻，写入 macro_news。sectors 由 build_macro_context 一次性抓取。"""
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

    em_entries = _fetch_em_finance_news(date_str)

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


def _fetch_flow(date_str: str, sectors: list) -> dict:
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
        repo.save_flow_data(date_str, result)
        logger.info("资金流已获取: 筛选后申万行业%d个", len(sectors))
    except Exception as e:
        logger.warning("资金流抓取失败: %s", str(e)[:120], exc_info=True)
    return result


# ── LLM 选赛道 ──

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


def _is_concept_name(name: str, code: str = "") -> bool:
    """判断板块是否为概念/风格/指数类（精确BK代码列表）。"""
    if code and code in _CONCEPT_CODES:
        return True
    if not name:
        return True
    return False


def _load_sector_insights() -> str:
    return repo.get_sector_insights()


def _load_available_sectors() -> list[str]:
    return repo.get_available_sectors()


def _build_sector_prompt(date_str: str, news: dict, flow: dict) -> str:
    lessons = _load_sector_insights()
    available = _load_available_sectors()
    return sector_selection_prompt(
        date_str=date_str,
        available=available,
        top_gainers=news.get("top_gainers", ""),
        top_losers=news.get("top_losers", ""),
        etf_net_flow=news.get("etf_net_flow", ""),
        news_summary=news.get("summary", ""),
        flow_summary=flow.get("summary"),
        lessons=lessons or None,
        market_tech=repo.get_market_technical_summary() or None,
    )


def _summarize_news(news_summary: str) -> str:
    """用 LLM 把今日新闻压缩为精炼摘要；失败或为空时回退原文，不阻塞主流程。"""
    if not news_summary or not news_summary.strip():
        return news_summary
    try:
        content = call_llm(news_brief_prompt(news_summary), temperature=0.1, max_tokens=512)
        if content and content.strip():
            return content.strip()
    except Exception as e:
        logger.warning("新闻摘要生成失败，回退原文: %s", str(e)[:120])
    return news_summary


def _suggest_sectors(date_str: str, news: dict, flow: dict,
                     news_brief: str = "") -> MacroContext:
    prompt = _build_sector_prompt(date_str, news, flow)
    system_prompt = sector_selection_system_prompt()
    content = call_llm(prompt, system_prompt=system_prompt, max_tokens=2048)

    flow_top = flow.get("top_flows", [])
    flow_out = flow.get("top_outflows", [])
    if content is None:
        raise RuntimeError("LLM赛道选择调用失败，无法完成宏观分析")

    cleaned = _re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=_re.IGNORECASE)
    try:
        parsed = json.loads(cleaned)
        available = _load_available_sectors()
        avail_set = set(available)

        rec_raw = parsed.get("recommended_sectors", [])
        risk_raw = parsed.get("risk_sectors", [])

        rec_valid = [s for s in rec_raw if s in avail_set]
        risk_valid = [s for s in risk_raw if s in avail_set]

        if rec_raw and not rec_valid:
            raise RuntimeError(f"LLM 推荐赛道均不可投: {rec_raw}，可用赛道: {sorted(avail_set)}")

        if len(rec_valid) < len(rec_raw):
            dropped = set(rec_raw) - set(rec_valid)
            logger.warning("LLM推荐了不可投赛道，已过滤: %s", dropped)
        if len(risk_valid) < len(risk_raw):
            dropped = set(risk_raw) - set(risk_valid)
            logger.warning("LLM回避了不可投赛道，已过滤: %s", dropped)

        ctx = MacroContext(
            news_summary=news.get("summary", ""),
            news_brief=news_brief or news.get("summary", ""),
            recommended_sectors=rec_valid,
            risk_sectors=risk_valid,
            sector_reasoning=parsed.get("reasoning", ""),
            regime_label=parsed.get("regime_label", "neutral"),
            cls_stock_mentions=[],
            date=date_str,
            top_flows=flow_top,
            top_outflows=flow_out,
        )
        return ctx
    except Exception as e:
        raise RuntimeError(f"LLM赛道选择返回无法解析: {e}") from e

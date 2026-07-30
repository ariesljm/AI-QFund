"""宏观分析 agent：多源数据 → LLM 选赛道 → MacroContext。

输出供 recommend.py（推荐）和 monitor.py（监控）直接消费。
"""

import hashlib
import json
import logging
from log_utils import get_logger
import re as _re
import time as _time
from dataclasses import dataclass, field, asdict
from datetime import datetime

from llm import call_llm
from prompts import sector_selection_prompt, sector_selection_system_prompt
from data_store import _db_conn  # 保留用于 ensure_column/迁移
import repo
from fetch import fetch as _fetch
from sector_api import is_industry_code, is_industry_name

logger = get_logger("macro_agent")

_BOARD_URL = "https://push2ex.eastmoney.com/getAllBKChanges?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wzchanges&pageindex=0&pagesize=500"
_CLS_API = "https://www.cls.cn/v1/roll/get_roll_list"


@dataclass(frozen=True)
class MacroContext:
    news_summary: str = ""
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

    force=True 时跳过缓存，强制实时抓取新闻+LLM 重新选赛道。
    资金流数据始终实时刷新。
    """
    _ensure_column()
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")

    sectors = _load_board_sectors()

    flow = _fetch_flow(date_str, sectors)

    if not force:
        cached = _load_cache(date_str)
        if cached is not None:
            return cached

    news = _fetch_news(date_str, sectors)

    ctx = _suggest_sectors(date_str, news, flow)
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


def _cls_sign(params: dict) -> str:
    s = "&".join(f"{k}={params[k]}" for k in sorted(params.keys()))
    return hashlib.md5(hashlib.sha1(s.encode()).hexdigest().encode()).hexdigest()


def _fetch_cls() -> tuple[list[dict], list[dict]]:
    """抓取财联社电报，返回 (条目列表, 关联股票列表)。

    条目: [{"level": "A", "text": "...", "stocks": [...]}]
    关联股票: [{"name": "...", "code": "...", "level": "A", "title": "..."}]
    """
    try:
        ts = int(_time.time())
        params = {
            "app": "CailianpressWeb", "os": "web", "sv": "8.4.6",
            "refresh_type": "1", "rn": "60", "last_time": str(ts), "category": "",
        }
        params["sign"] = _cls_sign(params)
        txt = _http_get(f"{_CLS_API}?{'&'.join(f'{k}={params[k]}' for k in params)}")
        data = json.loads(txt)
        items = data.get("data", {}).get("roll_data", [])
        if not items:
            return [], []

        entries = []
        stocks = []
        seen_texts = set()
        for it in items:
            title = (it.get("title") or "").strip()
            content = (it.get("content") or "").strip()
            level = it.get("level", "C")

            body = content or title
            if not body:
                continue

            dedup_key = body[:120]
            if dedup_key in seen_texts:
                continue
            seen_texts.add(dedup_key)

            text = body
            if title and title != content:
                text = f"{title}。{body}"

            sl = it.get("stock_list", [])
            stock_names = []
            for s in sl:
                sid = s.get("StockID", "")
                name = s.get("name", "")
                if name:
                    stock_names.append(name)
                if name or sid:
                    stocks.append({"name": name, "code": sid, "level": level, "title": title})
            if stock_names:
                text += f" [关联: {'/'.join(stock_names)}]"

            subs = it.get("subjects", [])
            if subs:
                tags = [s.get("subject_name", "") for s in subs if s.get("subject_name")]
                if tags:
                    text += f" ({'、'.join(tags)})"

            entries.append({"level": level, "text": text, "stocks": stock_names})

        return entries, stocks
    except Exception as e:
        logger.warning("财联社电报抓取失败: %s", str(e)[:120], exc_info=True)
        return [], []


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


def _fetch_news(date_str: str, sectors: list | None = None) -> dict:
    """抓取板块排行 + 财联社电报，写入 macro_news。"""
    top_gainers = top_losers = etf_net_flow = ""
    try:
        sectors = _load_board_sectors()
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

    cls_entries, cls_stocks = _fetch_cls()

    news = ""
    if cls_entries:
        lines = []
        for e in cls_entries:
            prefix = "\u203c\ufe0f " if e["level"] == "A" else "\u26a0\ufe0f " if e["level"] == "B" else ""
            lines.append(f"{prefix}{e['text']}")
        news = "\n".join(lines)

    if top_gainers or top_losers or cls_entries:
        repo.save_macro_news(date_str, news, top_gainers, top_losers, etf_net_flow)
    logger.info("快讯入库: 领涨[%s] 领跌[%s] cls=%d条(去重后) 关联股票%d只",
                top_gainers[:40], top_losers[:40], len(cls_entries), len(cls_stocks))
    return {
        "summary": news, "top_gainers": top_gainers,
        "top_losers": top_losers, "etf_net_flow": etf_net_flow,
        "cls_stocks": cls_stocks,
        "cls_entries": cls_entries,
    }


def _fetch_flow(date_str: str, sectors: list | None = None) -> dict:
    """抓取行业板块资金流排名（主力净流入/涨跌），排除概念/风格板块。"""
    result = {"summary": ""}
    try:
        sectors = _load_board_sectors()
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
    )


def _suggest_sectors(date_str: str, news: dict, flow: dict) -> MacroContext:
    prompt = _build_sector_prompt(date_str, news, flow)
    system_prompt = sector_selection_system_prompt()
    content = call_llm(prompt, system_prompt=system_prompt, max_tokens=2048)

    cls_stocks = news.get("cls_stocks", [])

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

        if len(rec_valid) < len(rec_raw):
            dropped = set(rec_raw) - set(rec_valid)
            logger.warning("LLM推荐了不可投赛道，已过滤: %s", dropped)
        if len(risk_valid) < len(risk_raw):
            dropped = set(risk_raw) - set(risk_valid)
            logger.warning("LLM回避了不可投赛道，已过滤: %s", dropped)

        ctx = MacroContext(
            news_summary=news.get("summary", ""),
            recommended_sectors=rec_valid,
            risk_sectors=risk_valid,
            sector_reasoning=parsed.get("reasoning", ""),
            regime_label=parsed.get("regime_label", "neutral"),
            cls_stock_mentions=cls_stocks,
            date=date_str,
            top_flows=flow_top,
            top_outflows=flow_out,
        )
        return ctx
    except Exception as e:
        raise RuntimeError(f"LLM赛道选择返回无法解析: {e}") from e

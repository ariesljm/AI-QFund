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
from data_store import _db_conn
from fetch import fetch as _fetch
from sector_api import is_industry_code, is_industry_name

logger = get_logger("macro_agent")

_BOARD_URL = "https://push2ex.eastmoney.com/getAllBKChanges?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wzchanges&pageindex=0&pagesize=500"
_KUAXUN_URL = ("https://search-api-web.eastmoney.com/search/jsonp?cb=jQuery"
               "&param=%7B%22uid%22%3A%22%22%2C%22keyword%22%3A%22%E5%9F%BA%E9%87%91%22"
               "%2C%22type%22%3A%5B%22cmsArticleWebOld%22%5D%2C%22client%22%3A%22web%22"
               "%2C%22clientType%22%3A%22web%22%2C%22clientVersion%22%3A%22curr%22"
               "%2C%22param%22%3A%7B%22cmsArticleWebOld%22%3A%7B%22searchScope%22%3A%22default%22"
               "%2C%22sort%22%3A%22default%22%2C%22pageIndex%22%3A1%2C%22pageSize%22%3A20"
               "%2C%22preTag%22%3A%22%20%22%2C%22postTag%22%3A%22%20%22%7D%7D%7D")
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

    flow = _fetch_flow(date_str)  # 资金流高频数据，始终刷新

    if not force:
        cached = _load_cache(date_str)
        if cached is not None:
            return cached

    news = _fetch_news(date_str)

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
    with _db_conn() as conn:
        row = conn.execute(
            "SELECT context_json FROM macro_news WHERE date = ? AND context_json IS NOT NULL",
            (date_str,),
        ).fetchone()
    if row:
        try:
            d = json.loads(row[0])
            return MacroContext(**{k: v for k, v in d.items() if k in MacroContext.__dataclass_fields__})
        except Exception as e:
            logger.warning("宏观缓存解析失败: %s", str(e)[:120], exc_info=True)
    return None


def _save_cache(ctx: MacroContext) -> None:
    with _db_conn() as conn:
        conn.execute(
            "INSERT INTO macro_news (date, context_json) VALUES (?, ?) "
            "ON CONFLICT(date) DO UPDATE SET context_json = excluded.context_json",
            (ctx.date, json.dumps(asdict(ctx), ensure_ascii=False)),
        )


# ── 数据源 ──

def _http_get(url: str, timeout: float = 12) -> str:
    return _fetch(url, timeout=timeout).text


def _cls_sign(params: dict) -> str:
    s = "&".join(f"{k}={params[k]}" for k in sorted(params.keys()))
    return hashlib.md5(hashlib.sha1(s.encode()).hexdigest().encode()).hexdigest()


def _fetch_cls() -> tuple[str, list[dict]]:
    """抓取财联社电报，返回 (格式化文本, 关联股票列表)。

    关联股票: [{"name": "阳光电源", "code": "300274", "level": "A", "title": "..."}]
    """
    try:
        ts = int(_time.time())
        params = {
            "app": "CailianpressWeb", "os": "web", "sv": "8.4.6",
            "refresh_type": "1", "rn": "20", "last_time": str(ts), "category": "",
        }
        params["sign"] = _cls_sign(params)
        txt = _http_get(f"{_CLS_API}?{'&'.join(f'{k}={params[k]}' for k in params)}")
        data = json.loads(txt)
        items = data.get("data", {}).get("roll_data", [])
        if not items:
            return "", []

        text_lines = []
        stocks = []
        for it in items:
            title = (it.get("title") or "").strip()
            content = (it.get("content") or "").strip()
            level = it.get("level", "C")
            prefix = "【加红】" if level == "A" else "【重要】" if level == "B" else ""
            line = title or content
            if prefix:
                line = f"{prefix} {line}"
            if content and content != title:
                line = f"{line}。{content}"
            sl = it.get("stock_list", [])
            if sl:
                names = [s.get("name", "") for s in sl if s.get("name")]
                if names:
                    line += f" [关联: {'/'.join(names)}]"
            subs = it.get("subjects", [])
            if subs:
                tags = [s.get("subject_name", "") for s in subs if s.get("subject_name")]
                if tags:
                    line += f" ({'、'.join(tags)})"
            text_lines.append(line)

            for s in sl:
                sid = s.get("StockID", "")
                name = s.get("name", "")
                if name or sid:
                    stocks.append({
                        "name": name, "code": sid, "level": level,
                        "title": title,
                    })

        return "\n".join(text_lines), stocks
    except Exception as e:
        logger.warning("财联社电报抓取失败: %s", str(e)[:120], exc_info=True)
        return "", []


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


def _fetch_news(date_str: str) -> dict:
    """抓取行业涨跌排行 + 东财快讯 + 财联社电报，写入 macro_news 原始字段。"""
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

    kx_news = ""
    try:
        txt = _http_get(_KUAXUN_URL)
        txt = txt.strip()
        if txt.startswith("jQuery("):
            txt = txt[7:-1]
        data = json.loads(txt)
        items = data.get("result", {}).get("cmsArticleWebOld", [])
        headlines = []
        for it in items:
            title = (it.get("title") or "").strip()
            if title:
                title = title.replace("  ", " ")
                headlines.append(title.strip())
                if len(headlines) >= 20:
                    break
        kx_news = "；".join(headlines)
    except Exception as e:
        logger.warning("基金快讯抓取失败: %s", str(e)[:120], exc_info=True)

    cls_text, cls_stocks = _fetch_cls()

    summary_parts = []
    if kx_news:
        summary_parts.append(f"【东方财富快讯】\n{kx_news}")
    if cls_text:
        summary_parts.append(f"【财联社电报】\n{cls_text}")
    news = "\n\n".join(summary_parts)

    with _db_conn() as conn:
        if top_gainers or top_losers:
            conn.execute(
                "INSERT INTO macro_news "
                "(date, news_summary, top_gainers, top_losers, etf_net_flow) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(date) DO UPDATE SET "
                "news_summary=excluded.news_summary, top_gainers=excluded.top_gainers, "
                "top_losers=excluded.top_losers, etf_net_flow=excluded.etf_net_flow",
                (date_str, news, top_gainers, top_losers, etf_net_flow),
            )
    logger.info("快讯入库: 领涨[%s] 领跌[%s] 新闻%d字 cls=%d条",
                top_gainers[:40], top_losers[:40], len(news), len(cls_stocks))
    return {
        "summary": news, "top_gainers": top_gainers,
        "top_losers": top_losers, "etf_net_flow": etf_net_flow,
        "cls_stocks": cls_stocks,
    }


def _fetch_flow(date_str: str) -> dict:
    """抓取行业板块资金流排名（主力净流入/涨跌）。"""
    result = {"summary": ""}
    try:
        sectors = _load_board_sectors()
        if not sectors:
            return result
        sorted_by_flow = sorted(sectors, key=lambda x: float(x.get("zjl", 0) or 0), reverse=True)
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
        with _db_conn() as conn:
            conn.execute(
                "INSERT INTO macro_news (date, flow_json) VALUES (?, ?) "
                "ON CONFLICT(date) DO UPDATE SET flow_json = excluded.flow_json",
                (date_str, json.dumps(result, ensure_ascii=False)),
            )
        logger.info("资金流已获取: 筛选后申万行业%d个", len(sectors))
    except Exception as e:
        logger.warning("资金流抓取失败: %s", str(e)[:120], exc_info=True)
    return result


# ── LLM 选赛道 ──

def _load_sector_insights() -> str:
    with _db_conn() as conn:
        rows = conn.execute(
            "SELECT insight FROM evolution_insights "
            "WHERE insight_type = 'sector' AND active = 1 AND confidence > 0.3 "
            "ORDER BY created_date DESC LIMIT 5"
        ).fetchall()
    if not rows:
        return ""
    return "\n".join(f"  - {r[0]}" for r in rows)


def _load_available_sectors() -> list[str]:
    with _db_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT rbsa_industry_1 FROM fund_features "
            "WHERE rbsa_industry_1 IS NOT NULL AND rbsa_industry_1 != '' "
            "UNION "
            "SELECT DISTINCT rbsa_industry_2 FROM fund_features "
            "WHERE rbsa_industry_2 IS NOT NULL AND rbsa_industry_2 != '' "
            "UNION "
            "SELECT DISTINCT rbsa_industry_3 FROM fund_features "
            "WHERE rbsa_industry_3 IS NOT NULL AND rbsa_industry_3 != ''"
        ).fetchall()
    return [r[0] for r in rows]


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
        return MacroContext(
            news_summary=news.get("summary", ""),
            recommended_sectors=parsed.get("recommended_sectors", []),
            risk_sectors=parsed.get("risk_sectors", []),
            sector_reasoning=parsed.get("reasoning", ""),
            regime_label=parsed.get("regime_label", "neutral"),
            cls_stock_mentions=cls_stocks,
            date=date_str,
            top_flows=flow_top,
            top_outflows=flow_out,
        )
    except Exception as e:
        raise RuntimeError(f"LLM赛道选择返回无法解析: {e}") from e

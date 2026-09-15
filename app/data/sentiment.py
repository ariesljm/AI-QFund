"""票 12 切片四：舆情争议（个股新闻，东财 search-api）。

数据源（2026-09 实测）：`search-api-web.eastmoney.com/search/jsonp`，
按股票代码搜新闻（JSONP 包装，需剥离 cb(...)）；茅台命中 604 条。

争议判定（纯函数）：标题/内容含争议关键词（质疑/争议/泡沫/高位/接盘/闪崩
等）→ 标争议。给 LLM 的切片里争议必须显式可见；例行新闻只计数。
"""

import json

from app.data.fetchers import fetch
from app.utils.log import get_logger

logger = get_logger("data.sentiment")

_SEARCH_URL = "https://search-api-web.eastmoney.com/search/jsonp"

# 舆情争议关键词（命中标题/内容即标）
_CONTROVERSY_KEYWORDS = ("质疑", "争议", "泡沫", "高位", "接盘", "闪崩", "暴跌",
                         "被指", "涉嫌", "操纵", "爆雷", "腰斩", "套牢")

_TYPE_PARAM = {
    "uid": "", "keyword": "", "type": ["cmsArticleWebOld"], "client": "web",
    "clientType": "web", "clientVersion": "curr",
    "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "default",
                                    "pageIndex": 1, "pageSize": 5,
                                    "preTag": "<em>", "postTag": "</em>"}},
}


def _strip_jsonp(text: str) -> dict:
    """JSONP `cb({...})` → dict。"""
    i = text.find("(")
    j = text.rfind(")")
    return json.loads(text[i + 1:j] if 0 <= i < j else text)


def is_controversial(title: str, content: str = "") -> bool:
    """舆情争议判定（纯函数）：标题/内容含争议关键词。"""
    blob = f"{title} {content}"
    return any(k in blob for k in _CONTROVERSY_KEYWORDS)


def fetch_stock_news(stock_code: str, page_size: int = 5) -> list[dict]:
    """个股相关新闻，返回 [{date, title, content}]（按日期倒序）。"""
    p = json.loads(json.dumps(_TYPE_PARAM))   # 深拷贝避免共享变异
    p["keyword"] = stock_code
    p["param"]["cmsArticleWebOld"]["pageSize"] = page_size
    r = fetch(_SEARCH_URL, params={"cb": "cb", "param": json.dumps(p, ensure_ascii=False), "_": "1"})
    data = _strip_jsonp(r.text)
    items = ((data.get("result") or {}).get("cmsArticleWebOld")) or []
    out = []
    for it in items:
        d = str(it.get("date") or "")[:10]
        if d:
            out.append({"date": d, "title": (it.get("title") or "").replace("<em>", "").replace("</em>", ""),
                        "content": (it.get("content") or "")[:200]})
    return out


def sentiment_text(stocks: list[dict]) -> str:
    """舆情争议切片（票 12 切片四）。

    stocks: [{"stock_code", "stock_name"}]。争议新闻显式可见（日期+标题），
    例行只计数；获取失败标缺失（不脑补）。
    """
    parts: list[str] = []
    for s in stocks:
        code = s.get("stock_code", "")
        name = s.get("stock_name") or code
        if not code:
            continue
        try:
            news = fetch_stock_news(code)
        except Exception as e:
            logger.warning("舆情获取失败 %s: %s", code, str(e)[:100])
            parts.append(f"{name}({code})：舆情获取失败（缺失）")
            continue
        if not news:
            parts.append(f"{name}({code})：近月无相关新闻")
            continue
        hot = [n for n in news if is_controversial(n["title"], n["content"])]
        if hot:
            detail = "；".join(f"{n['date']}「{n['title'][:40]}」" for n in hot[:3])
            parts.append(f"⚠️ {name}({code})：舆情争议 {detail}"
                         + (f"（另有 {len(hot) - 3} 条）" if len(hot) > 3 else ""))
        else:
            parts.append(f"{name}({code})：{len(news)} 条相关新闻，无争议信号")
    return "；".join(parts)

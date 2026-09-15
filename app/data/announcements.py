"""票 12 切片二：重仓股风险雷达（个股公告，东财 np-anotice 接口）。

数据源（2026-09 实测）：`np-anotice-stock.eastmoney.com/api/security/ann`，
返回 {data.list: [{title_ch, notice_date, display_time, ...}]}。

负面判定（纯函数）：标题含立案/质押/预亏/减持等关键词 → 标负面；
「解除质押」等解除语义不算（排除词优先）。风控视角宁严勿松——
给 LLM 的切片里负面公告必须显式可见，例行披露只计数不展开。
"""

from datetime import datetime, timedelta

from app.data.fetchers import fetch
from app.utils.log import get_logger

logger = get_logger("data.announcements")

_ANNOUNCE_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"

# 负面关键词（命中即标）；排除词（解除/完毕/完成）优先——避免「解除质押」误报
_NEGATIVE_KEYWORDS = ("立案", "质押", "预亏", "减持", "处罚", "违规", "冻结",
                      "退市", "问询", "警示", "诉讼", "违约", "风险提示")
_EXCLUDE_WORDS = ("解除", "完毕", "完成", "届满", "终止")

# 例行披露栏目名（只计数不展开）
_ROUTINE_COLUMNS = ("业绩", "分红", "董事会", "股东大会", "回购", "澄清")


def is_negative(title: str) -> bool:
    """负面公告判定：含负面关键词且非解除语义（纯函数）。"""
    if not title:
        return False
    if any(w in title for w in _EXCLUDE_WORDS):
        return False
    return any(k in title for k in _NEGATIVE_KEYWORDS)


def fetch_announcements(stock_code: str, days: int = 30,
                        page_size: int = 50) -> list[dict]:
    """个股最近 days 天公告，返回 [{date, title}]（按公告日升序）。

    接口按 sort_date 倒序返回；过滤近 days 天后反转升序。
    请求失败/无公告 → []（调用方记缺失，不脑补）。
    """
    r = fetch(_ANNOUNCE_URL, params={
        "sr": "-1", "page_size": str(page_size), "page_index": "1",
        "ann_type": "A", "client_source": "web", "stock_list": stock_code,
        "f_node": "0", "s_node": "0",
    })
    data = (r.json().get("data") or {}).get("list") or []
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    out: list[dict] = []
    for it in data:
        d = str(it.get("notice_date") or "")[:10]
        title = it.get("title_ch") or it.get("title") or ""
        if d >= cutoff and title:
            out.append({"date": d, "title": title.strip()})
    out.sort(key=lambda x: x["date"])
    return out


def risk_radar_text(stocks: list[dict], days: int = 30) -> str:
    """重仓股风险雷达切片（票 12 切片二）。

    stocks: [{"stock_code", "stock_name"}, ...]（Top10，PIT 可见）。
    输出：负面公告逐条（日期+标题），例行披露只计数——负面必须显式可见。
    """
    parts: list[str] = []
    for s in stocks:
        code = s.get("stock_code", "")
        name = s.get("stock_name") or code
        if not code:
            continue
        try:
            anns = fetch_announcements(code, days=days)
        except Exception as e:
            logger.warning("公告获取失败 %s: %s", code, str(e)[:100])
            parts.append(f"{name}({code})：公告获取失败（缺失）")
            continue
        if not anns:
            parts.append(f"{name}({code})：近 {days} 天无公告")
            continue
        neg = [a for a in anns if is_negative(a["title"])]
        if neg:
            detail = "；".join(f"{a['date']}「{a['title'][:40]}」" for a in neg[:3])
            parts.append(f"⚠️ {name}({code})：负面公告 {detail}"
                         + (f"（另有 {len(neg) - 3} 条）" if len(neg) > 3 else ""))
        else:
            parts.append(f"{name}({code})：近 {days} 天 {len(anns)} 条公告，无负面")
    return "；".join(parts)

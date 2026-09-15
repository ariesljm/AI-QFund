"""基金申赎/规模快照（票 06）：AUM 与申购状态/单日上限。

数据源（2026-09 实测）：
- AUM：东财 `fund.eastmoney.com/pingzhongdata/{code}.js` 的 `Data_fluctuationScale`
  （季度规模变动，series[-1].y 为最新规模，单位亿元）
- 申购状态 + 单日上限：天天基金详情页 `fund.eastmoney.com/{code}.html`，
  正则 `交易状态：限大额 (单日累计购买上限50.00万元)`——开放申购/暂停申购/限大额。

蛋卷 `subscribe_status` 实测不可靠（161725/005827 实际限大额但返回 0），不用。
"""

import json
import re

from app.data.fetchers import fetch
from app.repo.decision import save_purchase_restriction
from app.utils.log import get_logger

logger = get_logger("data.restriction")

_PINGZHONG = "https://fund.eastmoney.com/pingzhongdata/{code}.js"
_FUND_PAGE = "https://fund.eastmoney.com/{code}.html"
_PAGE_REFERER = {"Referer": "https://fund.eastmoney.com/"}

_TRADE_STATUS_RE = re.compile(
    r"交易状态：</span><span class=\"staticCell\">\s*(开放申购|暂停申购|限大额|停止交易)"
    r"(?:\s*\(\s*<span>\s*单日累计购买上限([\d.]+)万元\s*</span>\s*\))?")
"""天天详情页结构：
`<span class="itemTit">交易状态：</span><span class="staticCell">限大额  `
`(<span>单日累计购买上限50.00万元</span>)</span>`（开放/暂停无括号段）。
"""

_SCALE_RE = re.compile(r"Data_fluctuationScale\s*=\s*(\{.*?\})\s*;", re.S)


def parse_trade_status(html: str) -> dict:
    """解析天天基金详情页的交易状态（纯函数，注入 HTML 可测）。

    返回 {"status": "normal"/"limited"/"suspended"|None, "daily_limit": 元|None}。
    匹配不到 → 两项均为 None（状态未知，不写库不误杀）。
    """
    m = _TRADE_STATUS_RE.search(html)
    if not m:
        return {"status": None, "daily_limit": None}
    label, amount = m.group(1), m.group(2)
    status = {"开放申购": "normal", "暂停申购": "suspended", "停止交易": "suspended"}.get(label)
    if label == "限大额":
        status = "limited"
    daily_limit = float(amount) * 10_000 if (status == "limited" and amount) else None
    return {"status": status, "daily_limit": daily_limit}


def parse_scale(js_text: str) -> float | None:
    """解析 pingzhongdata 的 Data_fluctuationScale，返回最新合并规模（元）。"""
    m = _SCALE_RE.search(js_text)
    if not m:
        return None
    try:
        series = json.loads(m.group(1)).get("series") or []
    except (json.JSONDecodeError, TypeError):
        return None
    if not series:
        return None
    y = series[-1].get("y")
    return float(y) * 1e8 if isinstance(y, (int, float)) else None


def fetch_fund_info(code: str) -> dict:
    """单只基金快照：{"aum": 元|None, "status": str|None, "daily_limit": 元|None}。

    两项独立抓取，各自失败不互相拖累（AUM 拿不到不丢弃状态）。
    """
    info: dict = {"aum": None, "status": None, "daily_limit": None}
    r = fetch(_PINGZHONG.format(code=code), headers=_PAGE_REFERER)
    info["aum"] = parse_scale(r.text)
    r2 = fetch(_FUND_PAGE.format(code=code), headers=_PAGE_REFERER)
    st = parse_trade_status(r2.text)
    info["status"], info["daily_limit"] = st["status"], st["daily_limit"]
    return info


def backfill_fund_info(codes: list[str]) -> dict[str, int]:
    """对给定基金集合回填 AUM + 申赎状态。返回 {"done","status_unknown","failed"}。

    幂等（UPDATE/INSERT ON CONFLICT）+ 断点续传（每只提交）；状态未知不算失败
    （未知不误杀，回填继续），缺失清单由日志定位。
    """
    done = status_unknown = failed = 0
    for code in codes:
        try:
            f = fetch_fund_info(code)
        except Exception as e:
            failed += 1
            logger.warning("申赎/规模回填失败 %s: %s", code, str(e)[:120])
            continue
        if f["aum"] is not None:
            # fund_basic 由 save_fund_list 全表重建，此处只 UPDATE aum 列
            from app.database import db_conn
            with db_conn() as conn:
                conn.execute("UPDATE fund_basic SET aum = ? WHERE code = ?", (f["aum"], code))
                conn.commit()
        if f["status"] is not None:
            note = f"单日上限 {f['daily_limit'] / 1e4:.2f} 万元" if f["daily_limit"] else ""
            save_purchase_restriction(code, f["status"], note, f["daily_limit"])
        else:
            status_unknown += 1
        done += 1
    return {"done": done, "status_unknown": status_unknown, "failed": failed}

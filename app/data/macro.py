"""板块行情获取 module（底层数据域）：抓取东方财富板块行情并过滤出有效行业板块。

load_board_sectors 供 sector_history（板块历史快照）复用；概念板块（QFII重仓/
煤化工概念等）与季报 RBSA 的行业体系不同，需在此过滤。
"""

import json

from app.data.fetchers import fetch as _fetch
from app.features.sector import is_industry_code

_BOARD_URL = "https://push2ex.eastmoney.com/getAllBKChanges?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wzchanges&pageindex=0&pagesize=500"

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


def _http_get(url: str, timeout: float = 12) -> str:
    return _fetch(url, timeout=timeout).text


def _is_pseudo_sector(name: str) -> bool:
    """排除伪板块：含下划线的系统分类，或以 昨日/当日/今日 开头的技术形态分类"""
    if not name:
        return True
    if '_' in name:
        return True
    return name.startswith(_PSEUDO_PREFIXES)


_CONCEPT_KEYWORDS = ("概念", "重仓", "风格")


def _is_concept_name(name: str, code: str = "") -> bool:
    """判断板块是否为概念/风格/指数类（精确 BK 代码黑名单 + 名称关键词）。

    黑名单只覆盖 33 个已知代码，但东财板块里还有大量名称带"概念/重仓/风格"的
    非行业板块（黄金概念、QFII重仓、医保重仓…）。这些若混入反推因子宇宙，会让
    净值回归选出概念板块，风格漂移防线对比季报行业名时误判“行业切换”。
    """
    if code and code in _CONCEPT_CODES:
        return True
    if not name:
        return True
    return any(kw in name for kw in _CONCEPT_KEYWORDS)


def load_board_sectors() -> list[dict]:
    """加载东方财富板块行情数据，返回过滤后的有效行业板块列表（排除概念/伪板块）。

    概念板块（QFII重仓/煤化工概念等）与季报 RBSA 的行业体系不同，若混入
    反推因子宇宙会让净值回归选出概念板块，风格漂移防线误判“行业切换”。
    """
    txt = _http_get(_BOARD_URL)
    data = json.loads(txt)
    allbk = (data.get("data") or {}).get("allbk", [])
    return [
        b for b in allbk
        if not _is_pseudo_sector(b.get('n', '') or '')
        and is_industry_code(b.get('c', '') or '')
        and not _is_concept_name(b.get('n', '') or '', b.get('c', '') or '')
        and bool((b.get('n') or '').strip())
    ]

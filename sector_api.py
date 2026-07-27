"""申万行业板块涨跌幅 — 备用 API 方案

方案对比：
  push2.eastmoney.com/api/qt/clist/get  ← 被完全封控（所有 TLS 方案均失败）
  push2ex.eastmoney.com/getAllBKChanges ← ✅ 可用

使用 push2ex 替代方案获取行业板块涨跌幅数据。
"""

import json, os
import urllib3
urllib3.disable_warnings()

import requests
from dataclasses import dataclass, field
from log_utils import get_logger

logger = get_logger(__name__)

# 绕过系统代理（127.0.0.1:10808 不可达时会导致所有 push2 请求失败）
os.environ["NO_PROXY"] = "*"

_push2ex_session = None

def _get_session():
    global _push2ex_session
    if _push2ex_session is None:
        _push2ex_session = requests.Session()
        _push2ex_session.trust_env = False
        _push2ex_session.proxies = {"http": None, "https": None}
        _push2ex_session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        })
    return _push2ex_session

UT = "7eea3edcaed734bea9cbfc24409ed989"

# 申万二级行业板块代码范围（约 152 个）
# BK04xx-BK07xx (经典行业) + BK10xx-BK12xx (新分类行业)
INDUSTRY_CODE_RANGES = [
    (400, 555),   # 行业板块 1
    (725, 748),   # 行业板块 2
    (1015, 1288), # 行业板块 3
]


def fetch_board_changes() -> list[dict]:
    """从 push2ex 获取当日所有板块变动。

    Returns:
        list[dict]: 包含所有板块的数据，每个板块字段：
        - c (str): 板块代码 (BKxxxx)
        - n (str): 板块名称
        - u (str): 涨跌幅 (%)
        - zjl (float): 主力资金净流入
        - m (int): 市场类型 (90=板块)
        - ct (int): 成交数量
    """
    session = _get_session()
    resp = session.get(
        "https://push2ex.eastmoney.com/getAllBKChanges",
        params={
            "ut": UT,
            "dpt": "wzchanges",
            "pageindex": "0",
            "pagesize": "5000",
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("rc") != 0:
        raise RuntimeError(f"push2ex failed: rc={data.get('rc')}")
    return (data.get("data") or {}).get("allbk", [])


def is_industry_code(code: str) -> bool:
    """判断板块代码是否为申万二级行业。"""
    if not code.startswith("BK") or len(code) != 6:
        return False
    try:
        num = int(code[2:])
    except ValueError:
        return False
    return any(low <= num <= high for low, high in INDUSTRY_CODE_RANGES)


# 非申万行业的板块名称黑名单（BK 代码范围内但实为指数/概念/地域板块）
_NON_INDUSTRY_KEYWORDS = {
    # 指数类
    "HS300", "AH股", "上证", "深证", "创业", "科创", "中证",
    "MSCI", "富时", "标准普尔", "道琼斯", "纳斯达克",
    # 风格类
    "大盘股", "小盘股", "微盘股", "中盘股", "权重股", "百元股", "次新股",
    # 地域类
    "深圳", "上海", "北京", "广东", "深圳地产", "上海本地", "北京本地", "广东本地",
    # 索引标记
    "R", "ETF", "LOF",
    # 持仓/持股风格（非行业）
    "重仓", "持股",
    # 概念题材（非行业）
    "概念", "信创", "中特估", "绿色", "新能源",
    # 外资/机构/社保
    "QFII", "养老金", "机构", "社保", "北向",
    # 业绩预告类
    "预增", "预减", "预亏", "预盈",
    # 其他技术/交易类
    "做市", "连板",
}


def is_industry_name(name: str) -> bool:
    """判断板块名称是否属于申万二级行业（排除指数/概念/地域板块）。"""
    if not name or not name.strip():
        return False
    for kw in _NON_INDUSTRY_KEYWORDS:
        if kw in name:
            return False
    return True


@dataclass
class SectorInfo:
    code: str          # BKxxxx
    name: str          # 板块名称
    change_pct: float  # 涨跌幅(%)

def get_industry_sectors(sort_by: str = "change_pct", limit: int = 30) -> list[SectorInfo]:
    """获取申万行业板块涨跌幅排行。

    Args:
        sort_by: 排序字段 "change_pct" (涨跌幅) 或 "fund_flow" (资金流)
        limit: 返回条数

    Returns:
        list[SectorInfo]: 排序后的申万行业板块列表
    """
    all_boards = fetch_board_changes()
    industries = []
    for b in all_boards:
        code = b.get("c", "")
        if not is_industry_code(code):
            continue
        try:
            change_pct = float(b.get("u", 0))
        except (ValueError, TypeError):
            continue
        industries.append(SectorInfo(
            code=code,
            name=b.get("n", ""),
            change_pct=change_pct,
        ))

    if sort_by == "change_pct":
        industries.sort(key=lambda s: s.change_pct, reverse=True)
    # 其他排序方式可扩展

    return industries[:limit]


def get_industry_with_flow(limit: int = 30) -> list[dict]:
    """获取申万行业板块涨跌幅 + 资金流，返回与旧 API 兼容的 dict 格式。

    Returns:
        list[dict]: f12=板块代码, f14=板块名称, f3=涨跌幅, f62=资金流(j)
    """
    all_boards = fetch_board_changes()
    industries = []

    for b in all_boards:
        code = b.get("c", "")
        if not is_industry_code(code):
            continue
        try:
            change_pct = float(b.get("u", 0))
        except (ValueError, TypeError):
            change_pct = 0

        industries.append({
            "f12": code,
            "f14": b.get("n", ""),
            "f3": change_pct,

        })

    industries.sort(key=lambda x: abs(x["f3"]), reverse=True)
    return industries[:limit]


# ===== 自测 =====
if __name__ == "__main__":
    print("===== push2ex 申万行业板块涨跌幅 =====")
    sectors = get_industry_sectors(sort_by="change_pct", limit=10)
    for s in sectors:
        print(f"  {s.code} {s.name}: {s.change_pct:+.2f}%")

    print(f"\n总共提取 {len(sectors)} 条行业板块排名")

    # 对比旧格式
    print("\n===== 兼容格式 (f3/f12/f14) =====")
    items = get_industry_with_flow(limit=5)
    for item in items:
        print(f"  {item['f12']} {item['f14']}: f3={item['f3']:+.2f}% f62={item.get('f62','N/A')}j")

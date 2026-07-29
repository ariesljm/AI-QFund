"""申万行业板块代码过滤器。

数据源：push2ex.eastmoney.com/getAllBKChanges（push2 已被封控）。
仅按 BK 代码范围过滤，不使用名称黑名单——因为 rbsa_industry 赛道名可能与 BK 名不完全一致。
LLM 侧已有 available 赛道清单约束，不会选中概念板块。
"""

from log_utils import get_logger

logger = get_logger(__name__)

# 申万二级行业 BK 代码范围（2021 版，约 120 个）
# BK0400-0555 传统申万行业 + BK0725-0748 + BK1015-1049 + BK1200-1288
# 跳过 BK1050-1199（概念板块密集区）
INDUSTRY_CODE_RANGES = [
    (400, 555),
    (725, 748),
    (1015, 1049),
    (1200, 1288),
]


def is_industry_code(code: str) -> bool:
    """判断板块代码是否在申万行业 BK 代码范围内。"""
    if not code.startswith("BK") or len(code) != 6:
        return False
    try:
        num = int(code[2:])
    except ValueError:
        return False
    return any(low <= num <= high for low, high in INDUSTRY_CODE_RANGES)


def is_industry_name(name: str) -> bool:
    """始终返回 True——不再过滤名称，由 LLM 的 available 清单约束赛道选择。"""
    if not name or not name.strip():
        return False
    return True

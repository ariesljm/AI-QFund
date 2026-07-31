"""申万行业板块代码过滤器。"""

from app.utils.log import get_logger

logger = get_logger(__name__)

INDUSTRY_CODE_RANGES = [
    (400, 555),
    (725, 748),
    (1015, 1049),
    (1200, 1288),
]


def is_industry_code(code: str) -> bool:
    if not code.startswith("BK") or len(code) != 6:
        return False
    try:
        num = int(code[2:])
    except ValueError:
        return False
    return any(low <= num <= high for low, high in INDUSTRY_CODE_RANGES)


def is_industry_name(name: str) -> bool:
    if not name or not name.strip():
        return False
    return True

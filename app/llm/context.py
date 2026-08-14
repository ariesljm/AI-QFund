"""LLM 上下文构建：跨引擎共用的 prompt 素材单一来源。"""

from app import repo


def build_holdings_text(code: str, limit: int = 5) -> str:
    """持仓 → 单行文本（LLM 判断用；recommend/monitor 共用同一格式）。"""
    rows = repo.get_holdings(code, limit)
    if not rows:
        return "无持仓数据"
    return "；".join(
        f"{h['stock_name']}({h['industry'] or '其他'},{h['weight']:.1f}%)" for h in rows
    )

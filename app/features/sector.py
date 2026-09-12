"""申万行业板块代码过滤器 + 风格反推矩阵装配（深模块，架构审查候选 1）。

板块收益矩阵装配曾是重复实现温床：style_track / walk_forward / calculator / monitor
四处各自做「pct_chg 百分数 → 小数 ÷100」换算与「全日期覆盖过滤」，其中 monitor.py 的
注释记录过“不换算会放大 100 倍导致误触发 R1.5”的真实事故。本模块把单位换算、
全日期覆盖、日期对齐三件事单点收敛，消费方只消费结果。
"""

import numpy as np

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


def style_returns_matrix(ret_dates: list[str],
                         by_sector: dict | None = None,
                         sector_frame=None) -> tuple[np.ndarray, list[str]] | None:
    """窗口交易日 → 板块日收益矩阵（小数）。深模块，单位/对齐/过滤单点收敛。

    by_sector: {板块名或码: {date: pct_chg 百分数}}（查询层已按日期分组，未 ÷100）
    sector_frame: date×sector 宽表（load_sector_pct_frame 产物）——精确重排到
        ret_dates 并过滤全窗口缺失板块，与 by_sector 路径同语义。
    返回 (R, names)：R 为 len(ret_dates)×n 小数矩阵；names 为全日期覆盖板块名
    （升序）；板块不足 2 个或窗口过短 → None。消费方不得再做 ÷100 或日期过滤。
    """
    if len(ret_dates) < 2:
        return None
    if sector_frame is not None:
        sub = sector_frame.reindex(list(ret_dates))
        if sub.shape[0] < 2:
            return None
        sub = sub.dropna(axis=1, how="any")  # 全窗口每交易日有数据（与 by_sector 同口径）
        if sub.shape[1] < 2:
            return None
        return sub.to_numpy(dtype=float) / 100.0, list(sub.columns)
    if not by_sector:
        return None
    full = {n: m for n, m in by_sector.items()
            if all(d in m for d in ret_dates)}
    names = sorted(full)
    if len(names) < 2:
        return None
    # 板块 pct_chg 是百分数、基金收益率是小数 → ÷100（历史事故点，单点收敛）
    R = np.array([[full[n][d] / 100.0 for n in names] for d in ret_dates])
    return R, names


def sector_return_series(row_dates: list[str], by_sector: dict,
                         name: str) -> list[float | None]:
    """单行业按 row_dates 顺序的日收益（小数）序列，缺日 None（R1.5 对齐用）。

    row_dates 是消费方自己的净值日期（非交易日历尾部位置对齐）——修复
    monitor 按 trade_dates 尾部 zip 错位的历史隐患。单位 ÷100 与 by_sector
    构建由调用方约束（本函数只消费已分组的百分数值）。
    """
    m = by_sector.get(name) or {}
    return [m.get(d) / 100.0 if d in m else None for d in row_dates]

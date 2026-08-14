"""SVG 图表生成 module（纯函数）：双线走势 / 平滑曲线 / 质量曲线。

供 dashboard module 消费；无 repo/IO 依赖，可直接测试。
"""

from collections.abc import Callable, Sequence

# viewBox 200x100：Y 轴绘制区间（顶 90 → 底 10，共 80 单位高）
_VIEWBOX_W = 200.0
_Y_TOP = 90.0
_Y_HEIGHT = 80.0

# 质量曲线数据点（quality.py 生成）：date/code/abs_ret/decision_loss/decision_gap_best/cum_abs_ret
QualityPoint = dict[str, str | float | None]


def _y_scale(values: Sequence[float], pad_ratio: float) -> tuple[Callable[[float], float], float]:
    """Y 轴线性缩放工厂：数据范围加 pad 后映射到 [10, 90]，返回 (映射函数, 0 值基线 y)。"""
    y_min, y_max = min(values), max(values)
    y_range = y_max - y_min or 1
    pad = y_range * pad_ratio
    y_min -= pad
    y_max += pad
    y_range = y_max - y_min or 1

    def _y(v: float) -> float:
        return _Y_TOP - (v - y_min) / y_range * _Y_HEIGHT

    return _y, _y(0)


def _smooth_path(values: Sequence[float], y_scale: Callable[[float], float]) -> str:
    """三次贝塞尔平滑路径（viewBox 200x100）；点数 < 2 返回空串。"""
    n = len(values)
    if n < 2:
        return ""
    pts = [(i / (n - 1) * _VIEWBOX_W, y_scale(v)) for i, v in enumerate(values)]
    d = f"M {pts[0][0]:.1f},{pts[0][1]:.1f}"
    for i in range(n - 1):
        x0, y0 = pts[i]
        x1, y1 = pts[i + 1]
        mx = (x0 + x1) / 2
        d += f" C {mx:.1f},{y0:.1f} {mx:.1f},{y1:.1f} {x1:.1f},{y1:.1f}"
    return d


def smooth_svg_path(values: Sequence[float | None], pad_ratio: float = 0.1) -> tuple[str, float]:
    """生成平滑 SVG 路径与 0% 基线 y 坐标（alpha 贡献/质量曲线共用，viewBox 200x100）。

    空输入返回 ("", 50)；单点返回水平线。三次贝塞尔平滑，Y 轴按数据范围自动缩放。
    """
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return "", 50
    y_scale, baseline_y = _y_scale(vals, pad_ratio)
    if len(vals) == 1:
        y = y_scale(vals[0])
        return f"M 0,{y:.1f} L 200,{y:.1f}", baseline_y
    return _smooth_path(vals, y_scale), baseline_y


def make_dual_svg(pcts: Sequence[float], hs_pcts: Sequence[float]) -> tuple[str, str, float]:
    """生成两条平滑SVG路径：基金(主色)和沪深300(灰色)。

    viewBox 200x100，根据数据范围自动缩放 Y 轴。
    返回 (fund_svg, hs_svg, baseline_y) 其中 baseline_y 是 0% 线在 SVG 中的 y 坐标。
    """
    all_vals = list(pcts) + list(hs_pcts)
    if not all_vals:
        return "", "", 50
    y_scale, baseline_y = _y_scale(all_vals, 0.1)
    return _smooth_path(pcts, y_scale), _smooth_path(hs_pcts, y_scale), baseline_y


def quality_curve_svg(points: Sequence[QualityPoint]) -> tuple[str, float]:
    """累计超额曲线 SVG（单线），points=[{cum_abs_ret,...}] 按时间序。返回 (path, baseline_y)。"""
    return smooth_svg_path([float(p["cum_abs_ret"]) for p in points if p.get("cum_abs_ret") is not None],
                           pad_ratio=0.15)

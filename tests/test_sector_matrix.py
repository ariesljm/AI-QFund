"""features/sector 深模块测试：板块收益矩阵装配（架构审查候选 1）。

覆盖 ÷100 单位换算、全日期覆盖过滤、严格日期对齐（by_sector 与宽表两路径
同语义）、R1.5 单行业序列按净值日对齐。装配层此前零测试（事故点：单位不
换算放大 100 倍误触发 R1.5）。
"""

import numpy as np
import pandas as pd
import pytest

from app.features.sector import style_returns_matrix, sector_return_series

DAYS = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]


def _rows():
    """三板块原始行；'半导体' 缺 09-03 → 不满足全日期覆盖。"""
    r = []
    for d, pct in [(DAYS[0], 0.5), (DAYS[1], -0.2), (DAYS[2], 1.1), (DAYS[3], 0.3)]:
        r.append((d, "BK0001", "半导体", pct))
    for d, pct in [(DAYS[0], 1.0), (DAYS[1], 0.0), (DAYS[2], -0.5), (DAYS[3], 0.8)]:
        r.append((d, "BK0002", "电力", pct))
    for d, pct in [(DAYS[0], 2.0), (DAYS[1], 1.0), (DAYS[3], 0.1)]:  # 缺 09-03
        r.append((d, "BK0003", "白酒", pct))
    return r


def _by_sector():
    by = {}
    for d, _c, n, pct in _rows():
        by.setdefault(n, {})[d] = pct
    return by


class TestStyleReturnsMatrix:
    def test_division_by_100_single_point(self):
        """百分数 → 小数 ÷100 收敛于此：电力 09-01 = 1.0% → 0.01。"""
        R, names = style_returns_matrix(DAYS, by_sector=_by_sector())
        i = names.index("电力")
        assert R[0, i] == 0.01 and R[1, i] == 0.0

    def test_full_date_coverage_filters_gap_sectors(self):
        """缺口板块（白酒缺 09-03）被过滤，不参与矩阵。"""
        R, names = style_returns_matrix(DAYS, by_sector=_by_sector())
        assert "白酒" not in names
        assert set(names) == {"半导体", "电力"}
        assert R.shape == (4, 2)

    def test_insufficient_sectors_returns_none(self):
        assert style_returns_matrix(DAYS, by_sector={"半导体": {d: 1.0 for d in DAYS}}) is None

    def test_short_window_returns_none(self):
        assert style_returns_matrix([DAYS[0]], by_sector=_by_sector()) is None

    def test_frame_path_matches_rows_path(self):
        """宽表精确重排路径与 by_sector 路径同语义（÷100 + 全日期覆盖）。"""
        by = _by_sector()
        df = pd.DataFrame({n: {d: by[n][d] for d in DAYS if d in by[n]}
                           for n in by}).reindex(DAYS)
        Rf, names_f = style_returns_matrix(DAYS, sector_frame=df)
        Rr, names_r = style_returns_matrix(DAYS, by_sector=by)
        assert names_f == names_r
        np.testing.assert_allclose(Rf, Rr)


class TestSectorReturnSeries:
    def test_aligns_by_row_dates(self):
        """按净值日期对齐（R1.5 修复关键）：row_dates 与板块缺日 → None。"""
        by = _by_sector()
        # 基金净值日缺 09-02（停牌）→ 序列长度=3，缺口为 None，不按位置错配
        series = sector_return_series([DAYS[0], DAYS[2], DAYS[3]], by, "半导体")
        assert series[0] == pytest.approx(0.005)
        assert series[1] == pytest.approx(0.011)
        assert series[2] == pytest.approx(0.003)

    def test_unknown_sector_all_none(self):
        assert sector_return_series(DAYS, _by_sector(), "不存在行业") == [None] * 4

    def test_gap_day_none_for_gap_sector(self):
        """板块缺日 → None（白酒 09-03 无数据）。"""
        series = sector_return_series(DAYS, _by_sector(), "白酒")
        assert series[2] is None
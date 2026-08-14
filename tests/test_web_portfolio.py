"""组合累计收益序列（等权买入持有）+ 夏普/最大回撤测试。

回归根因：追踪监控表在多次推荐下持有天数有歧义，改为组合级绩效指标。
覆盖：多基金等权平均、离场基金截断、推荐日无净值基线点、沪深300对齐。
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.engine.valuation import portfolio_series, max_drawdown, sharpe_ratio
from app.web import app as webapp, charts


FUND_NAV_ROWS = [
    ("AAA", "2026-07-27", 1.00), ("AAA", "2026-07-28", 1.10),
    ("AAA", "2026-07-29", 1.21), ("AAA", "2026-07-30", 1.20),
    ("AAA", "2026-07-31", 1.30),
    ("BBB", "2026-07-27", 2.00), ("BBB", "2026-07-28", 2.05),
    ("BBB", "2026-07-29", 2.10), ("BBB", "2026-07-30", 2.00),
    ("BBB", "2026-07-31", 2.20),
]

HS_ROWS = [
    ("2026-07-27", 3000.0), ("2026-07-28", 3010.0), ("2026-07-29", 3020.0),
    ("2026-07-30", 3005.0), ("2026-07-31", 3040.0),
]


def _fake_nav_rows(codes):
    """按 code 过滤净值行（组合多基金查询）。"""
    return [r for r in FUND_NAV_ROWS if r[0] in codes]


def _fake_index_series(code="sh000300", columns=("date", "close", "volume", "ma60"), since=None):
    return HS_ROWS


@pytest.fixture
def monkey_db(monkeypatch):
    """隔离 DB：mock repo 查询，避免依赖本地数据库。"""
    def fake_tracking():
        return [
            {"code": "AAA", "name": "甲基金", "first_date": "2026-07-27",
             "rec_count": 1, "status": "HOLD", "exit_date": ""},
            {"code": "BBB", "name": "乙基金", "first_date": "2026-07-29",
             "rec_count": 1, "status": "EXIT", "exit_date": "2026-07-30"},
        ]

    def fake_entry(code, date):
        return {"AAA": 1.00, "BBB": 2.00}[code]

    monkeypatch.setattr(webapp.repo, "get_tracking_list", fake_tracking)
    monkeypatch.setattr(webapp.repo, "get_entry_nav", fake_entry)
    monkeypatch.setattr(webapp.repo.nav, "at", lambda code, date: None)
    monkeypatch.setattr(webapp.repo.nav, "batch_latest", _fake_nav_rows)
    monkeypatch.setattr(webapp.repo, "get_index_series", _fake_index_series)


class TestPortfolioSeries:
    def test_equal_weight_and_exit_truncation(self, monkey_db):
        """多基金等权平均 + 离场基金按离场日截断。"""
        dates, port, hs = portfolio_series()
        assert dates == ["2026-07-27", "2026-07-28", "2026-07-29", "2026-07-30", "2026-07-31"]
        # 7-27: 仅甲基金入场 → 0%；7-29 两基金等权 (21%+5%)/2=13%
        assert port[0] == pytest.approx(0.0)
        assert port[1] == pytest.approx(10.0)
        assert port[2] == pytest.approx(13.0)
        # 乙基金 7-30 离场，7-31 仅甲基金 → 30%
        assert port[3] == pytest.approx(10.0)
        assert port[4] == pytest.approx(30.0)

    def test_hs300_aligned(self, monkey_db):
        """沪深300 与组合日期对齐，相对首个可用值归一化。"""
        _, _, hs = portfolio_series()
        assert hs[0] == pytest.approx(0.0)
        assert hs[2] == pytest.approx((3020 / 3000 - 1) * 100, abs=0.01)

    def test_entry_nav_fallback_to_any_date(self, monkeypatch):
        """推荐日无净值时以入场净值作基线点（组合从 0% 起步），不做跨日回退。"""
        monkeypatch.setattr(webapp.repo, "get_tracking_list", lambda: [
            {"code": "AAA", "name": "甲基金", "first_date": "2026-08-01",
             "rec_count": 1, "status": "HOLD", "exit_date": ""},
        ])
        monkeypatch.setattr(webapp.repo, "get_entry_nav", lambda code, date: 1.00)
        monkeypatch.setattr(webapp.repo.nav, "at", lambda code, date: None)
        # 净值仅到 7-31，晚于推荐日 8-01
        monkeypatch.setattr(webapp.repo.nav, "batch_latest", _fake_nav_rows)
        monkeypatch.setattr(webapp.repo, "get_index_series", _fake_index_series)

        dates, port, hs = portfolio_series()
        # 8-01 为基线点，其后无净值 → 仅 1 个点，返回空
        assert dates == []


class TestSharpeAndDrawdown:
    def test_max_drawdown(self):
        """最大回撤 = 累计收益曲线的最大峰谷回撤。"""
        assert max_drawdown([0, 10, 13, 10, 30]) == 3.0
        assert max_drawdown([0, -5, -8]) == 8.0
        assert max_drawdown([0, 5, 10]) == 0.0

    def test_sharpe_ratio_positive_for_up_trend(self):
        """上升趋势组合 → 正夏普；持平组合 → None（零波动）。"""
        s = sharpe_ratio([0, 10, 13, 10, 30])
        assert s is not None and s > 0
        assert sharpe_ratio([5, 5, 5]) is None
        assert sharpe_ratio([0]) is None

    def test_dual_svg_generated(self, monkey_db):
        """组合双线 SVG 正常生成且带 0% 基线。"""
        _, port, hs = portfolio_series()
        svg, hsvg, baseline = charts.make_dual_svg(port, hs)
        assert svg.startswith("M ")
        assert hsvg.startswith("M ")
        assert 0 <= baseline <= 100

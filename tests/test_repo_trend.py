"""Ticket 01：repo 赛道趋势质量聚合层测试。

验证 get_sector_trend_features 把 fund_features（动量/波动）+ sector_daily_snapshot
（净流入）聚合为每个赛道的多周期趋势特征，并给出趋势质量标签；窗口不足优雅降级。
"""

import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.repo import base as repo_base


def _make_db():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE fund_features (code TEXT, date TEXT, rbsa_industry_1 TEXT, "
        "mom_5d REAL, momentum_20d REAL, vol_20d REAL)")
    conn.execute(
        "CREATE TABLE sector_daily_snapshot (date TEXT, sector_code TEXT, "
        "sector_name TEXT, pct_chg REAL, net_flow REAL)")
    return conn


def _seed_features(conn, sector, n=5, mom5=3.0, mom20=5.0, vol=0.2):
    conn.executemany(
        "INSERT INTO fund_features VALUES (?,?,?,?,?,?)",
        [(f"{sector}{i}", "2026-09-03", sector, mom5, mom20, vol)
         for i in range(n)])


def _seed_snapshot(conn, sector, flows):
    """快照日期为决策日(09-03)之前的交易日（未来不可见，与生产一致）。"""
    start = date(2026, 9, 3) - timedelta(days=len(flows))
    for i, f in enumerate(flows):
        conn.execute(
            "INSERT INTO sector_daily_snapshot VALUES (?,?,?,?,?)",
            ((start + timedelta(days=i)).isoformat(), sector, sector, 1.0, f))


class TestTrendAggregation:
    def test_mom_and_flow_agg(self, monkeypatch):
        """多周期特征：动量取基金均值、资金流取近 5 日累计。"""
        conn = _make_db()
        _seed_features(conn, "半导体", n=5, mom5=3.0, mom20=5.0)
        _seed_snapshot(conn, "半导体", [100.0, 200.0, -50.0, 300.0, 50.0])
        monkeypatch.setattr(repo_base, "db_conn", lambda: conn)

        out = repo_base.get_sector_trend_features("2026-09-03")
        f = out["半导体"]
        assert f["mom_5d"] == 3.0
        assert f["mom_20d"] == 5.0
        assert f["vol_20d"] == 0.2
        assert f["flow_5d"] == 600.0  # 100+200-50+300+50
        assert f["flow_days"] == 5

    def test_window_insufficient_degrades(self, monkeypatch):
        """板块快照不足 5 天：flow 用可用天数并标注 flow_days，不报错。"""
        conn = _make_db()
        _seed_features(conn, "食品", n=4)
        _seed_snapshot(conn, "食品", [100.0, 200.0])
        monkeypatch.setattr(repo_base, "db_conn", lambda: conn)

        out = repo_base.get_sector_trend_features("2026-09-03")
        f = out["食品"]
        assert f["flow_5d"] == 300.0
        assert f["flow_days"] == 2

    def test_no_snapshot_flow_none(self, monkeypatch):
        """赛道无板块快照：flow 为 None（不虚构），其余特征正常。"""
        conn = _make_db()
        _seed_features(conn, "医药", n=3)
        monkeypatch.setattr(repo_base, "db_conn", lambda: conn)

        out = repo_base.get_sector_trend_features("2026-09-03")
        f = out["医药"]
        assert f["mom_5d"] == 3.0
        assert f["flow_5d"] is None
        assert f["flow_days"] == 0

    def test_insufficient_members_none(self, monkeypatch):
        """赛道基金成员 <3：动量特征为 None（与定池成员门槛同口径）。"""
        conn = _make_db()
        _seed_features(conn, "稀有金属", n=2)
        monkeypatch.setattr(repo_base, "db_conn", lambda: conn)

        out = repo_base.get_sector_trend_features("2026-09-03")
        assert out["稀有金属"]["mom_5d"] is None


class TestTrendLabel:
    def _trends(self, monkeypatch, sectors_spec):
        """sectors_spec: [(sector, mom5, mom20, flow)] → 趋势特征 dict。"""
        conn = _make_db()
        for s, m5, m20, flow in sectors_spec:
            _seed_features(conn, s, n=5, mom5=m5, mom20=m20)
            if flow is not None:
                _seed_snapshot(conn, s, [flow / 5] * 5)
        monkeypatch.setattr(repo_base, "db_conn", lambda: conn)
        return repo_base.get_sector_trend_features("2026-09-03")

    def test_flow_negative_label(self, monkeypatch):
        """资金流累计为负 → 资金流出标签。"""
        out = self._trends(monkeypatch, [("银行", 2.0, 3.0, -100.0)])
        assert out["银行"]["label"] == "资金流出"

    def test_short_weak_label(self, monkeypatch):
        """5 日动量≤0 → 短期走弱标签。"""
        out = self._trends(monkeypatch, [("煤炭", -1.0, 2.0, 50.0)])
        assert out["煤炭"]["label"] == "短期走弱"

    def test_high_momentum_downweight(self, monkeypatch):
        """20 日动量处截面高位（P75+）→ 高位降权标签。"""
        out = self._trends(monkeypatch, [
            ("A", 2.0, 10.0, 50.0), ("B", 2.0, 9.0, 50.0),
            ("C", 2.0, 3.0, 50.0), ("D", 2.0, 2.0, 50.0),
        ])
        assert out["A"]["label"] == "高位降权"
        assert out["C"]["label"] != "高位降权"

    def test_healthy_label(self, monkeypatch):
        """资金正、短期强、非高位 → 趋势健康。"""
        out = self._trends(monkeypatch, [("军工", 3.0, 5.0, 80.0)])
        assert out["军工"]["label"] == "趋势健康"


class TestSnapshotWindowBoundary:
    def test_uses_latest_five_rows(self, monkeypatch):
        """只取≤决策日的最近快照（6 天→近 5 天），不做全表累计。"""
        conn = _make_db()
        _seed_features(conn, "医药", n=5)
        _seed_snapshot(conn, "医药", [10.0] * 6)  # 6 天
        monkeypatch.setattr(repo_base, "db_conn", lambda: conn)

        out = repo_base.get_sector_trend_features("2026-09-03")
        assert out["医药"]["flow_days"] == 5
        assert out["医药"]["flow_5d"] == 50.0  # 近 5 天累计，非 6 天

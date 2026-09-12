"""历史多期持仓回填测试：多期解析、多期共存幂等、回填增量跳过。

ticket 03（季报重仓多期留存）验收锁：
- 复合主键 (code, report_date, stock_code) 支持多期共存，重复写入幂等；
- year=YYYY&month= 多期页面能按报告期分块解析（此前全归最新一期）；
- 回填已完整覆盖的历史年份跳过（可断点续传）。
"""

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from app.data import holdings as h
from app.data.holdings import (
    _parse_holdings_history,
    _parse_holdings_html,
    backfill_holdings_history,
)
from app.data.store import save_holdings_batch


def _quarter_html(report_date: str, rows: list[tuple[str, str, str]]) -> str:
    """构造单季度持仓 HTML 片段（结构与东财持仓页一致）。"""
    body = "".join(
        f"<tr><td>{i + 1}</td><td><a href='/x'>{code}</a></td>"
        f"<td class='tol'><a href='/x'>{name}</a></td>"
        f"<td class='xglj'><a>x</a></td><td class='tor'>{weight}%</td></tr>"
        for i, (code, name, weight) in enumerate(rows)
    )
    return (
        f"<h4>持仓截止至：<font class='px12'>{report_date}</font></label></h4>"
        f"<table class='w782 comm tzxq t2'><tbody>{body}</tbody></table>"
    )


class TestParseHoldingsHistory:
    def test_multi_period_split_by_report_date(self):
        """多期页面按报告期分块，各期持仓独立、顺序与页面一致。"""
        html = (
            _quarter_html("2025-12-31", [("600519", "贵州茅台", "15.38")])
            + _quarter_html("2025-09-30", [("600809", "山西汾酒", "15.84")])
            + _quarter_html("2025-06-30", [("000858", "五粮液", "14.65")])
        )
        history = _parse_holdings_history(html)
        assert [rd for rd, _ in history] == ["2025-12-31", "2025-09-30", "2025-06-30"]
        assert history[0][1][0]["stock_code"] == "600519"
        assert history[0][1][0]["weight"] == 15.38
        assert history[1][1][0]["stock_code"] == "600809"
        assert history[2][1][0]["stock_name"] == "五粮液"

    def test_single_period_still_works_via_html(self):
        """单期页面经 _parse_holdings_html 仍返回单期契约。"""
        html = _quarter_html("2025-12-31", [("600519", "贵州茅台", "15.38")])
        report_date, holdings = _parse_holdings_html(html)
        assert report_date == "2025-12-31"
        assert len(holdings) == 1
        assert holdings[0]["stock_name"] == "贵州茅台"

    def test_empty_html_returns_empty(self):
        assert _parse_holdings_history("<html>无持仓</html>") == []
        assert _parse_holdings_html("<html>无持仓</html>") == (None, [])


class TestHoldingsMultiPeriodIdempotent:
    def test_multi_period_coexist_and_rewrite_idempotent(self):
        """两期共存；重复写同一期被 REPLACE 而非新增行。"""
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE fund_holdings (code TEXT, report_date TEXT, stock_code TEXT, "
            "stock_name TEXT, weight REAL, PRIMARY KEY (code, report_date, stock_code))"
        )
        save_holdings_batch(conn, [("161725", "2025-12-31", "600519", "贵州茅台", 15.38)])
        save_holdings_batch(conn, [("161725", "2025-09-30", "600809", "山西汾酒", 15.84)])
        # 重复写第一期（模拟重复抓取），不应新增行
        save_holdings_batch(conn, [("161725", "2025-12-31", "600519", "贵州茅台", 15.38)])

        count = conn.execute("SELECT COUNT(*) FROM fund_holdings").fetchone()[0]
        assert count == 2
        dates = sorted(r[0] for r in conn.execute("SELECT report_date FROM fund_holdings"))
        assert dates == ["2025-09-30", "2025-12-31"]


class _FixedDateTime:
    """固定 2026-09-10，使回填的年份范围确定（current_year=2026）。"""

    @staticmethod
    def now():
        return datetime(2026, 9, 10)


class TestBackfillHoldingsHistory:
    def test_skips_already_complete_year(self, monkeypatch):
        """该年 4 期已全部留存 → 跳过，不发起请求。"""
        monkeypatch.setattr(h, "get_buyable_codes", lambda: ["161725"])
        monkeypatch.setattr(
            h, "get_holdings_report_dates_all",
            lambda: {"161725": {"2025-12-31", "2025-09-30", "2025-06-30", "2025-03-31"}},
        )
        monkeypatch.setattr(h, "datetime", _FixedDateTime)

        calls: list = []
        monkeypatch.setattr(h, "fetch", lambda *a, **k: calls.append(1))

        assert backfill_holdings_history(years=1) == 0
        assert calls == []  # 完整年份未发起任何请求

    def test_fetches_and_persists_missing_year(self, monkeypatch):
        """该年无历史 → 拉取并入库该年全部季度，且幂等。"""
        monkeypatch.setattr(h, "get_buyable_codes", lambda: ["161725"])
        monkeypatch.setattr(h, "get_holdings_report_dates_all", lambda: {})
        monkeypatch.setattr(h, "datetime", _FixedDateTime)

        html = "".join(
            _quarter_html(d, [("600519", "贵州茅台", "15.38")])
            for d in ["2025-12-31", "2025-09-30", "2025-06-30", "2025-03-31"]
        )

        def fake_fetch(url, params=None, timeout=15, headers=None):
            assert params["year"] == "2025" and params["month"] == ""
            return httpx.Response(200, content=html.encode("utf-8"))

        monkeypatch.setattr(h, "fetch", fake_fetch)

        assert backfill_holdings_history(years=1) == 4  # 4 期各 1 条

        # 验证已入库（走 conftest 隔离的临时业务库）
        from app.repo.base import get_holdings_report_dates_all
        have = get_holdings_report_dates_all()
        assert have["161725"] == {"2025-12-31", "2025-09-30", "2025-06-30", "2025-03-31"}

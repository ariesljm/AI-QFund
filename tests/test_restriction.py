"""票 06：申赎/规模快照解析（天天详情页交易状态 + pingzhongdata 规模）。

parse_trade_status / parse_scale 是纯函数（注入 HTML/JS 文本可测）：
限大额含单日上限金额换算、暂停/开放映射、未知不误杀；规模解析取最新季度。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import app.data.restriction as rstr
import app.database as db_mod
from app.repo.decision import get_purchase_status, save_purchase_restriction


class TestParseTradeStatus:
    def test_open(self):
        html = '<span class="itemTit">交易状态：</span><span class="staticCell">开放申购  </span>'
        got = rstr.parse_trade_status(html)
        assert got == {"status": "normal", "daily_limit": None}

    def test_suspended(self):
        html = '<span class="itemTit">交易状态：</span><span class="staticCell">暂停申购  </span>'
        got = rstr.parse_trade_status(html)
        assert got == {"status": "suspended", "daily_limit": None}

    def test_limited_with_amount(self):
        """限大额 50.00 万元 → 500,000 元。"""
        html = ('<span class="itemTit">交易状态：</span>'
                '<span class="staticCell">限大额  '
                '(<span>单日累计购买上限50.00万元</span>)</span>')
        got = rstr.parse_trade_status(html)
        assert got == {"status": "limited", "daily_limit": 500_000.0}

    def test_unmatched_returns_none(self):
        assert rstr.parse_trade_status("<html>无交易状态</html>") \
            == {"status": None, "daily_limit": None}


class TestParseScale:
    def test_normal(self):
        js = 'var x=1; Data_fluctuationScale = {"categories":["2026-06-30"],' \
             '"series":[{"y":39.38,"mom":"48.93%"}]};\nvar y=2;'
        assert rstr.parse_scale(js) == pytest.approx(39.38 * 1e8)

    def test_empty_series(self):
        assert rstr.parse_scale('Data_fluctuationScale = {"series":[]};') is None

    def test_no_match(self):
        assert rstr.parse_scale("var z = 3;") is None


class TestRoundtrip:
    def test_save_and_read_status(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "pr.db")
        save_purchase_restriction("161725", "limited", "单日上限 50.00 万元", 500_000.0)
        assert get_purchase_status("161725") == "limited"
        # 幂等更新
        save_purchase_restriction("161725", "normal")
        assert get_purchase_status("161725") == "normal"

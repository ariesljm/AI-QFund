"""app.data.macro 板块行情 module 测试：行业过滤（mock HTTP 传输）。"""

import json

from app.data import macro


class TestLoadBoardSectors:
    def test_filters_pseudo_and_non_industry(self, monkeypatch):
        """行业代码区间过滤 + 伪板块排除（下划线/昨日开头/空名）。"""
        payload = {"data": {"allbk": [
            {"n": "食品饮料", "c": "BK0438", "u": 1.5},
            {"n": "半导体", "c": "BK1036", "u": 2.0},
            {"n": "昨日涨停", "c": "BK9999", "u": 3.0},    # 伪板块（昨日 开头）
            {"n": "系统_分类", "c": "BK0439", "u": 4.0},   # 伪板块（含下划线）
            {"n": "概念类", "c": "BK0100", "u": 5.0},      # 非行业代码区间
            {"n": "", "c": "BK0440", "u": 6.0},            # 空名
        ]}}
        monkeypatch.setattr(macro, "_http_get", lambda url, timeout=12: json.dumps(payload))
        got = macro.load_board_sectors()
        assert [(b["n"], b["c"]) for b in got] == [("食品饮料", "BK0438"), ("半导体", "BK1036")]

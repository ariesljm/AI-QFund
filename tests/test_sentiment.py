"""票 12 切片四：舆情争议（新闻搜索 + 争议判定 + 装配）。

is_controversial 纯函数（关键词）；_strip_jsonp 解析（cb(...) 包装）；
fetch_stock_news（东财 search-api 实测响应片段）；sentiment_text 切片输出。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.data.sentiment as se


class TestIsControversial:
    def test_keywords(self):
        for t in ("被质疑业绩造假", "股价闪崩", "市场争议加大", "高位接盘风险", "涉嫌违规被调查"):
            assert se.is_controversial(t)

    def test_benign_not(self):
        assert not se.is_controversial("贵州茅台发布2026年半年报")
        assert not se.is_controversial("公司日常经营公告")


class TestStripJsonp:
    def test_wrapped(self):
        assert se._strip_jsonp('cb({"code":0,"data":[1,2]})') == {"code": 0, "data": [1, 2]}

    def test_plain_json(self):
        assert se._strip_jsonp('{"code":0}') == {"code": 0}


class TestFetchStockNews:
    def test_parses_items(self, monkeypatch):
        """解析东财 search-api 实测片段：date/title（去 em 标签）。"""
        def fake_fetch(url, params=None, **kw):
            class R:
                def __init__(self):
                    self.text = 'cb({"code":0,"result":{"cmsArticleWebOld":['
                    self.text += '{"date":"2026-08-15 10:11:51","title":"贵州茅台(<em>600519</em>.SH)半年报"'
                    self.text += ',"content":"内容片段"}]}})'
            return R()
        monkeypatch.setattr(se, "fetch", fake_fetch)
        got = se.fetch_stock_news("600519")
        assert len(got) == 1
        assert got[0]["date"] == "2026-08-15"
        assert "<em>" not in got[0]["title"] and "600519" in got[0]["title"]

    def test_empty(self, monkeypatch):
        def fake_fetch(url, params=None, **kw):
            class R:
                def __init__(self):
                    self.text = 'cb({"code":0,"result":{"cmsArticleWebOld":[]}})'
            return R()
        monkeypatch.setattr(se, "fetch", fake_fetch)
        assert se.fetch_stock_news("600519") == []


class TestSentimentText:
    def test_controversy_visible(self, monkeypatch):
        def fake_fetch(code, **kw):
            return [{"date": "2026-09-10", "title": "被质疑业绩造假", "content": ""}]
        monkeypatch.setattr(se, "fetch_stock_news", fake_fetch)
        txt = se.sentiment_text([{"stock_code": "600519", "stock_name": "贵州茅台"}])
        assert "舆情争议" in txt and "质疑" in txt

    def test_benign_counts(self, monkeypatch):
        def fake_fetch(code, **kw):
            return [{"date": "2026-09-01", "title": "半年报发布", "content": ""}] * 3
        monkeypatch.setattr(se, "fetch_stock_news", fake_fetch)
        txt = se.sentiment_text([{"stock_code": "600519", "stock_name": "贵州茅台"}])
        assert "无争议信号" in txt and "3 条" in txt

    def test_failure_marked(self, monkeypatch):
        def fake_fetch(code, **kw):
            raise RuntimeError("网络")
        monkeypatch.setattr(se, "fetch_stock_news", fake_fetch)
        txt = se.sentiment_text([{"stock_code": "600519", "stock_name": "贵州茅台"}])
        assert "获取失败" in txt and "缺失" in txt

"""app.data.macro 宏观数据获取 module 测试：板块/资金流/新闻解析与入库（mock HTTP 传输）。"""

import json
import pytest

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


class TestFetchEmFinanceNews:
    def test_parse_dedupe_and_page_stop(self, monkeypatch):
        """正常解析：时间/标题/摘要投影、标题去重、空页停。"""
        pages = iter([
            {"data": {"list": [
                {"showTime": "2026-08-11 09:30", "title": "新闻A", "summary": "摘要A"},
                {"showTime": "2026-08-11 09:10", "title": "新闻B", "summary": ""},
                {"showTime": "2026-08-11 09:10", "title": "新闻B", "summary": "重复标题"},
            ]}},
            {"data": {"list": []}},
        ])
        monkeypatch.setattr(macro, "_http_get", lambda url, timeout=12: json.dumps(next(pages)))
        got = macro.fetch_em_finance_news("2026-08-11")
        assert got == [
            {"time": "09:30", "title": "新闻A", "summary": "摘要A"},
            {"time": "09:10", "title": "新闻B", "summary": ""},
        ]

    def test_cross_day_fallback_to_latest(self, monkeypatch):
        """跨日运行（凌晨）：当天新闻未生成时回退到接口返回的最新日期。"""
        payload = {"data": {"list": [
            {"showTime": "2026-08-10 15:00", "title": "昨闻", "summary": ""},
        ]}}
        monkeypatch.setattr(macro, "_http_get", lambda url, timeout=12: json.dumps(payload))
        got = macro.fetch_em_finance_news("2026-08-11")
        assert got == [{"time": "15:00", "title": "昨闻", "summary": ""}]

    def test_mixed_days_uses_today_when_first_is_yesterday(self, monkeypatch):
        """首条是昨日深夜新闻但列表含当天新闻：以最大日期判定，收当天不误回退（8-12 复现）。"""
        payload = {"data": {"list": [
            {"showTime": "2026-08-11 23:58", "title": "昨日深夜", "summary": ""},
            {"showTime": "2026-08-12 14:19", "title": "今日新闻", "summary": "摘要"},
            {"showTime": "2026-08-12 09:00", "title": "今日早间", "summary": ""},
        ]}}
        monkeypatch.setattr(macro, "_http_get", lambda url, timeout=12: json.dumps(payload))
        got = macro.fetch_em_finance_news("2026-08-12")
        titles = [g["title"] for g in got]
        assert "今日新闻" in titles and "今日早间" in titles
        assert "昨日深夜" not in titles

    def test_paging_keeps_target_day(self, monkeypatch):
        """翻页时目标日期沿用第一页：第二页首条已非当天则停止，不收集昨日新闻。"""
        pages = iter([
            {"data": {"list": [
                {"showTime": "2026-08-12 14:00", "title": "当天A", "summary": ""},
                {"showTime": "2026-08-12 09:00", "title": "当天B", "summary": ""},
            ]}},
            {"data": {"list": [
                {"showTime": "2026-08-11 23:00", "title": "昨日遗留", "summary": ""},
            ]}},
        ])
        monkeypatch.setattr(macro, "_http_get", lambda url, timeout=12: json.dumps(next(pages)))
        got = macro.fetch_em_finance_news("2026-08-12")
        assert [g["title"] for g in got] == ["当天A", "当天B"]

    def test_retries_then_raises(self, monkeypatch):
        """连续失败重试耗尽后抛异常终止管线（不用空数据兜底）。"""
        calls = {"n": 0}

        def boom(url, timeout=12):
            calls["n"] += 1
            raise ConnectionError("断网")

        monkeypatch.setattr(macro, "_http_get", boom)
        monkeypatch.setattr(macro.time, "sleep", lambda s: None)  # 跳过重试退避
        with pytest.raises(RuntimeError, match="连续3次抓取失败"):
            macro.fetch_em_finance_news("2026-08-11", retries=3)
        assert calls["n"] == 3


class TestFetchNews:
    def test_assembles_and_saves(self, monkeypatch):
        """领涨/领跌/资金流排行 + 新闻拼接 + macro_news 入库。"""
        sectors = [
            {"n": "食品饮料", "c": "BK0438", "u": 1.5, "zjl": 100},
            {"n": "半导体", "c": "BK1036", "u": -2.0, "zjl": -50},
        ]
        saved = {}
        monkeypatch.setattr(macro.repo, "save_macro_news", lambda *a: saved.update(args=a))
        monkeypatch.setattr(macro, "fetch_em_finance_news", lambda date_str: [
            {"time": "09:30", "title": "新闻A", "summary": "摘要A"}])
        got = macro.fetch_news("2026-08-11", sectors)
        assert got["top_gainers"] == "食品饮料(+1.50%)、半导体(-2.00%)"
        assert got["top_losers"] == "半导体(-2.00%)、食品饮料(+1.50%)"
        assert got["etf_net_flow"] == "食品饮料: 100元"
        assert "[09:30] 新闻A：摘要A" in got["summary"]
        assert saved  # save_macro_news 被调用


class TestFetchFlow:
    def test_concept_filter_and_totals(self, monkeypatch):
        """概念代码（BK0536）排除出资金流排名；净额为全行业加总。"""
        sectors = [
            {"n": "食品饮料", "c": "BK0438", "u": 1.5, "zjl": 100},
            {"n": "半导体", "c": "BK1036", "u": -2.0, "zjl": -50},
            {"n": "基金重仓", "c": "BK0536", "u": 2.0, "zjl": 200},  # 概念，排除
        ]
        saved = {}
        monkeypatch.setattr(macro.repo, "save_flow_data", lambda d, r: saved.update(data=r))
        monkeypatch.setattr(macro.repo, "save_sector_snapshot", lambda d, s: None)
        got = macro.fetch_flow("2026-08-11", sectors)
        assert got["total_net"] == 50
        assert got["top_flows"][0]["name"] == "食品饮料"
        assert got["top_outflows"][0]["name"] == "半导体"
        assert "基金重仓" not in got["summary"]
        assert saved["data"]["total_net"] == 50


class TestFetchMacroInputs:
    def test_chains_sources(self, monkeypatch):
        """聚合入口：板块 → 资金流 → 新闻 一次抓取并打包。"""
        monkeypatch.setattr(macro, "load_board_sectors", lambda: [{"n": "x"}])
        monkeypatch.setattr(macro, "fetch_flow", lambda d, s: {"summary": "flow"})
        monkeypatch.setattr(macro, "fetch_news", lambda d, s: {"summary": "news"})
        inputs = macro.fetch_macro_inputs("2026-08-11")
        assert inputs.board_sectors == [{"n": "x"}]
        assert inputs.flow == {"summary": "flow"}
        assert inputs.news == {"summary": "news"}

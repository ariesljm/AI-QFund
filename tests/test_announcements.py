"""票 12 切片二：重仓股风险雷达（负面公告判定纯函数 + 装配）。

is_negative 纯函数（含排除词：解除质押不误报）；fetch_announcements 解析
（东财 np-anotice 实测响应片段做 fixture）；risk_radar_text 切片输出
（负面显式可见、例行计数、获取失败标缺失）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


import app.data.announcements as ann


class TestIsNegative:
    def test_keywords(self):
        for t in ("关于控股股东减持计划的公告", "收到证监会立案调查通知",
                  "2026年半年度业绩预亏公告", "股东股份质押公告", "违规担保事项"):
            assert ann.is_negative(t)

    def test_exclude_words_override(self):
        """解除语义不算负面（排除词优先）。"""
        assert not ann.is_negative("关于解除部分股份质押的公告")
        assert not ann.is_negative("减持计划实施完毕")

    def test_routine_not_negative(self):
        assert not ann.is_negative("2026年半年度业绩报告")
        assert not ann.is_negative("董事会决议公告")

    def test_empty(self):
        assert not ann.is_negative("")


class TestFetchAnnouncements:
    def test_parses_listing(self, monkeypatch):
        """解析东财 np-anotice 响应（实测片段）：过滤近窗口、升序、title_ch。"""
        def fake_fetch(url, params=None, **kw):
            class R:
                def json(self):
                    return {"data": {"list": [
                        {"title_ch": "贵州茅台:关于召开2026年半年度业绩说明会的公告",
                         "notice_date": "2026-09-01 00:00:00"},
                        {"title_ch": "贵州茅台:控股股东减持计划公告",
                         "notice_date": "2026-09-14 00:00:00"},
                    ]}}
            return R()
        monkeypatch.setattr(ann, "fetch", fake_fetch)
        got = ann.fetch_announcements("600519", days=30)
        assert [a["date"] for a in got] == ["2026-09-01", "2026-09-14"]  # 升序
        assert got[1]["title"].startswith("贵州茅台")

    def test_outside_window_filtered(self, monkeypatch):
        def fake_fetch(url, params=None, **kw):
            class R:
                def json(self):
                    return {"data": {"list": [
                        {"title_ch": "旧公告", "notice_date": "2020-01-01 00:00:00"},
                    ]}}
            return R()
        monkeypatch.setattr(ann, "fetch", fake_fetch)
        assert ann.fetch_announcements("600519", days=30) == []

    def test_empty_response(self, monkeypatch):
        def fake_fetch(url, params=None, **kw):
            class R:
                def json(self):
                    return {"data": None}
            return R()
        monkeypatch.setattr(ann, "fetch", fake_fetch)
        assert ann.fetch_announcements("600519") == []


class TestRiskRadarText:
    def test_negative_visible(self, monkeypatch):
        def fake_fetch(code, **kw):
            return [{"date": "2026-09-01", "title": "控股股东减持计划公告"}]
        monkeypatch.setattr(ann, "fetch_announcements", fake_fetch)
        txt = ann.risk_radar_text([{"stock_code": "600519", "stock_name": "贵州茅台"}])
        assert "负面公告" in txt and "减持" in txt and "600519" in txt

    def test_clean_count_only(self, monkeypatch):
        def fake_fetch(code, **kw):
            return [{"date": "2026-08-15", "title": "业绩说明会公告"}]
        monkeypatch.setattr(ann, "fetch_announcements", fake_fetch)
        txt = ann.risk_radar_text([{"stock_code": "600519", "stock_name": "贵州茅台"}])
        assert "无负面" in txt and "1 条公告" in txt

    def test_failure_marked_missing(self, monkeypatch):
        def fake_fetch(code, **kw):
            raise RuntimeError("网络")
        monkeypatch.setattr(ann, "fetch_announcements", fake_fetch)
        txt = ann.risk_radar_text([{"stock_code": "600519", "stock_name": "贵州茅台"}])
        assert "获取失败" in txt and "缺失" in txt

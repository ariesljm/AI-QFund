"""票 12 切片三（US17 经理负荷）：F10 解析 + 切片文本装配。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.data.manager import manager_text, parse_managers

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "f10_000001.html"


class TestParseManagers:
    def test_fixture_current_managers(self):
        html = FIXTURE.read_text(encoding="utf-8")
        info = parse_managers(html)
        assert "郑晓辉" in info["current"]
        assert "刘睿聪" in info["current"]          # 共管 2 人
        assert info["tenure_months"] is not None and info["tenure_months"] >= 12
        assert info["change_count"] > 0
        assert "王泽实" in info["changes"]

    def test_changes_dedup(self):
        html = FIXTURE.read_text(encoding="utf-8")
        info = parse_managers(html)
        assert len(info["changes"]) == len(set(info["changes"]))

    def test_empty_html_safe(self):
        info = parse_managers("<html>无表格</html>")
        assert info["current"] == []
        assert info["tenure_months"] is None
        assert info["change_count"] == 0


class TestManagerText:
    def test_fixture_text_has_signals(self, monkeypatch):
        html = FIXTURE.read_text(encoding="utf-8")
        monkeypatch.setattr("app.data.manager.fetch_managers",
                            lambda code: parse_managers(html))
        txt = manager_text("000001")
        assert "现任经理" in txt
        assert "郑晓辉" in txt
        assert "共管" in txt

    def test_fetch_failure_visible_missing(self, monkeypatch):
        monkeypatch.setattr("app.data.manager.fetch_managers", lambda code: None)
        txt = manager_text("000001")
        assert "获取失败" in txt                     # 缺失显式可见

    def test_no_current_visible(self, monkeypatch):
        monkeypatch.setattr("app.data.manager.fetch_managers",
                            lambda code: {"current": [], "tenure_months": None,
                                          "change_count": 0, "changes": []})
        txt = manager_text("000001")
        assert "无现任经理数据" in txt

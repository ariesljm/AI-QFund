"""B5：app/web/quotes.py _fetch_live 真实解析测试。

背景：web 层对 quotes 的既有测试把 _fetch_live 整体 monkeypatch 掉，解析逻辑本身
零覆盖。本文件构造腾讯 qt.gtimg.cn 真实格式（v_s_sh 前缀 + `~` 分号字段、GBK 编码）
的响应文本，通过 monkeypatch quotes.fetch 注入字节后直连 _fetch_live，覆盖：
字段提取（code/name/price/change_percent/source）、短行/无匹配行/非法 float 行忽略、
空响应抛 RuntimeError、字段带逗号/空值/千分位等特殊值行为。不发真实网络请求。
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.web import quotes


def _gbk_fetch(text: str):
    """把响应文本包装成 fetch() 的返回值形态（httpx.Response.content 字节，GBK）。"""

    def _fake(url, **kwargs):
        return SimpleNamespace(content=text.encode("gbk", errors="replace"))

    return _fake


def _parse(monkeypatch, text: str) -> list[dict]:
    """注入响应文本并直连私有方法 _fetch_live（真实解析路径）。"""
    monkeypatch.setattr(quotes, "fetch", _gbk_fetch(text))
    return quotes.IndexQuoteCache()._fetch_live()


class TestFetchLiveParsing:
    def test_parses_valid_lines_skips_noise(self, monkeypatch):
        """两个正常指数解析出完整字段；短行/非 v_s_sh 行/停牌行/非法涨跌幅行被忽略。"""
        text = "\n".join([
            'v_s_sh000001="1~上证指数~000001~3530.25~-12.34~-0.35~7890123456~2345678901"',
            'v_s_sh000300="1~沪深300~000300~4012.58~10.25~0.26~4567890123~1234567890"',
            'v_s_sh000688="1~科创50~000688"',                         # 短行：不足 6 字段
            'v_s_sz000001="1~平安银行~000001~11.50~0.10~0.88~1~2"',    # 非 v_s_sh 前缀 → 正则不匹配
            'v_s_sh000852="1~中证1000~000852~--~0.00~0.00~1~2~3~4"',   # price 非法（停牌 "--"）
            'v_s_sh000016="1~上证50~000016~2600.00~5.00~--~1~2"',      # 涨跌幅非法
        ]) + "\n"
        items = _parse(monkeypatch, text)
        assert items == [
            {"code": "sh000001", "name": "上证指数", "price": 3530.25,
             "change_percent": -0.35, "source": "live"},
            {"code": "sh000300", "name": "沪深300", "price": 4012.58,
             "change_percent": 0.26, "source": "live"},
        ]

    def test_empty_response_raises_runtimeerror(self, monkeypatch):
        """空响应：无任何 v_s_sh 行 → 抛 RuntimeError('行情响应为空')。"""
        with pytest.raises(RuntimeError):
            _parse(monkeypatch, "")

    def test_response_without_valid_line_raises(self, monkeypatch):
        """有响应但全部行不可解析 → 同样抛 RuntimeError。"""
        with pytest.raises(RuntimeError):
            _parse(monkeypatch,
                   'v_s_sh000999="1~某指数~000999~abc~x~y~1~2"\n')

    def test_special_field_values(self, monkeypatch):
        """字段带逗号/空值/千分位：合法行原样解析，非法值整行忽略。"""
        text = "\n".join([
            'v_s_sh000922="1~红利,低波指数~000922~1234.56~1.23~0.10~1~2"',  # name 含逗号 → 保留
            'v_s_sh000001="1~上证指数~000001~3,530.25~-12.34~-0.35~1~2"',   # price 千分位逗号 → 忽略
            'v_s_sh000300="1~沪深300~000300~~10.25~0.26~1~2"',              # price 空字段 → 忽略
            'v_s_sh000016="1~上证50~000016~2600.00~5.00~+0.55~1~2"',        # 合法 → 解析
        ]) + "\n"
        items = _parse(monkeypatch, text)
        assert items == [
            {"code": "sh000922", "name": "红利,低波指数", "price": 1234.56,
             "change_percent": 0.10, "source": "live"},
            {"code": "sh000016", "name": "上证50", "price": 2600.00,
             "change_percent": 0.55, "source": "live"},
        ]

"""LLM 客户端空内容重试测试。

根因：代理偶发 HTTP 200 但 content 为空（非异常），旧逻辑直接返回空串，
导致下游 JSON 解析失败。修复：空内容视为失败进入重试。
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import openai

import app.llm.client as client_mod
from app.llm.client import call_llm, call_llm_json, parse_llm_json


class _FakeMsg:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMsg(content)


class _FakeResp:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]
        self.usage = None


def _install_fake(monkeypatch, sequence):
    """sequence: 依次返回的 content 列表（超出取最后一个）。"""
    state = {"i": 0}

    class _FakeCompletions:
        def create(self, **kwargs):
            content = sequence[min(state["i"], len(sequence) - 1)]
            state["i"] += 1
            return _FakeResp(content)

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeOpenAI:
        def __init__(self, *a, **k):
            pass

        chat = _FakeChat()

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)
    monkeypatch.setattr(
        client_mod, "load_settings",
        lambda: {"llm": {"api_key": "sk-test", "base_url": "http://x/v1", "model": "m"}},
    )
    return state


class TestCallLlmEmptyRetry:
    def test_empty_then_valid_retries(self, monkeypatch):
        """首次空内容 → 重试第二次拿到合法内容。"""
        state = _install_fake(monkeypatch, ["", '{"ok": 1}'])
        assert call_llm("p", max_tokens=100) == '{"ok": 1}'
        assert state["i"] == 2

    def test_all_empty_returns_none(self, monkeypatch):
        """三次均为空 → 返回 None。"""
        state = _install_fake(monkeypatch, ["", "", ""])
        assert call_llm("p", max_tokens=100) is None
        assert state["i"] == 3

    def test_valid_first_no_retry(self, monkeypatch):
        """首次即合法 → 不重试。"""
        state = _install_fake(monkeypatch, ["ok", ""])
        assert call_llm("p", max_tokens=100) == "ok"
        assert state["i"] == 1


class TestParseLlmJson:
    def test_plain_json(self):
        assert parse_llm_json('{"a": 1}') == {"a": 1}

    def test_strips_markdown_fences(self):
        assert parse_llm_json('```json\n{"a": 1}\n```') == {"a": 1}
        assert parse_llm_json('```\n{"a": 1}\n```') == {"a": 1}

    def test_empty_returns_none(self):
        assert parse_llm_json(None) is None
        assert parse_llm_json("   ") is None

    def test_invalid_returns_none(self):
        assert parse_llm_json("not json") is None


class TestCallLlmJson:
    def test_valid_returns_dict(self, monkeypatch):
        _install_fake(monkeypatch, ['{"ok": 1}'])
        assert call_llm_json("p", max_tokens=100) == {"ok": 1}

    def test_call_failure_returns_fallback(self, monkeypatch):
        _install_fake(monkeypatch, ["", "", ""])
        assert call_llm_json("p", max_tokens=100, fallback=[]) == []

    def test_parse_failure_returns_fallback(self, monkeypatch):
        _install_fake(monkeypatch, ["not json"])
        assert call_llm_json("p", max_tokens=100, fallback=[]) == []

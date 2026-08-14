"""LLM 客户端空内容重试测试。

根因：代理偶发 HTTP 200 但 content 为空（非异常），旧逻辑直接返回空串，
导致下游 JSON 解析失败。修复：空内容视为失败进入重试。
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
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
    # 重试退避不真睡，测试不拖时间
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)
    return state


def _install_fake_error(monkeypatch, status):
    """create() 恒抛指定 status 的异常（模拟 503/429/5xx/4xx）。"""
    state = {"i": 0}

    class _FakeCompletions:
        def create(self, **kwargs):
            state["i"] += 1
            err = openai.APIError("temporary failure", request=None, body=None)
            err.status_code = status
            raise err

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
    sleeps = []
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: sleeps.append(s))
    return state, sleeps


class TestCallLlmEmptyRetry:
    def test_empty_then_valid_retries(self, monkeypatch):
        """首次空内容 → 重试第二次拿到合法内容。"""
        state = _install_fake(monkeypatch, ["", '{"ok": 1}'])
        assert call_llm("p", max_tokens=100) == '{"ok": 1}'
        assert state["i"] == 2

    def test_all_empty_returns_none(self, monkeypatch):
        """全部为空 → 重试满 6 次后返回 None。"""
        state = _install_fake(monkeypatch, ["", "", ""])
        with pytest.raises(client_mod.LLMError):
            call_llm("p", max_tokens=100)
        assert state["i"] == client_mod._LLM_MAX_ATTEMPTS

    def test_valid_first_no_retry(self, monkeypatch):
        """首次即合法 → 不重试。"""
        state = _install_fake(monkeypatch, ["ok", ""])
        assert call_llm("p", max_tokens=100) == "ok"
        assert state["i"] == 1


class TestRetryOnTransientError:
    def test_503_retries_slow_base_then_gives_up(self, monkeypatch):
        """503（账号池不可用）：基础退避 15s、上限 60s，重试满 6 次后失败。"""
        state, sleeps = _install_fake_error(monkeypatch, 503)
        with pytest.raises(client_mod.LLMError):
            call_llm("p", max_tokens=100)
        assert state["i"] == client_mod._LLM_MAX_ATTEMPTS
        assert len(sleeps) == client_mod._LLM_MAX_ATTEMPTS - 1
        assert sleeps[0] >= client_mod._LLM_RETRY_BASE_SLOW
        assert all(s <= client_mod._LLM_RETRY_MAX_DELAY for s in sleeps)

    def test_429_retries_slow_base(self, monkeypatch):
        """429 限流：同样走慢基础退避。"""
        state, sleeps = _install_fake_error(monkeypatch, 429)
        with pytest.raises(client_mod.LLMError):
            call_llm("p", max_tokens=100)
        assert state["i"] == client_mod._LLM_MAX_ATTEMPTS
        assert sleeps[0] >= client_mod._LLM_RETRY_BASE_SLOW

    def test_5xx_retries_fast_base(self, monkeypatch):
        """普通 5xx：快速基础退避（2s 起步）。"""
        state, sleeps = _install_fake_error(monkeypatch, 500)
        with pytest.raises(client_mod.LLMError):
            call_llm("p", max_tokens=100)
        assert state["i"] == client_mod._LLM_MAX_ATTEMPTS
        assert sleeps[0] >= client_mod._LLM_RETRY_BASE_FAST
        assert sleeps[0] < client_mod._LLM_RETRY_BASE_SLOW

    def test_4xx_no_retry(self, monkeypatch):
        """客户端错误（401 鉴权）确定性失败：不重试，直接抛 LLMError。"""
        state, sleeps = _install_fake_error(monkeypatch, 401)
        with pytest.raises(client_mod.LLMError):
            call_llm("p", max_tokens=100)
        assert state["i"] == 1
        assert sleeps == []


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

    def test_fence_with_surrounding_text(self):
        """代码围栏前/后夹带解释文字仍可解析。"""
        assert parse_llm_json("以下是分析：\n```json\n{\"a\": 1}\n```\n以上完毕") == {"a": 1}

    def test_text_before_json(self):
        """JSON 前夹带解释文字仍可解析（提取首个顶层片段）。"""
        assert parse_llm_json("根据分析结果，推荐如下：{\"a\": 1} 请确认") == {"a": 1}

    def test_array_json(self):
        """数组型 JSON 可解析。"""
        assert parse_llm_json("```json\n[1, 2, 3]\n```") == [1, 2, 3]


class TestCallLlmJson:
    def test_valid_returns_dict(self, monkeypatch):
        _install_fake(monkeypatch, ['{"ok": 1}'])
        assert call_llm_json("p", max_tokens=100) == {"ok": 1}

    def test_call_failure_raises_llmerror(self, monkeypatch):
        """技术失败（重试耗尽）→ 抛 LLMError（候选 7：不再吞成 fallback）。"""
        _install_fake(monkeypatch, ["", "", ""])
        with pytest.raises(client_mod.LLMError):
            call_llm_json("p", max_tokens=100, fallback=[])

    def test_parse_failure_returns_fallback(self, monkeypatch):
        _install_fake(monkeypatch, ["not json"])
        assert call_llm_json("p", max_tokens=100, fallback=[]) == []

    def test_validator_passes_returns_cleaned(self, monkeypatch):
        """validator 校验通过：返回清洗后的结果。"""
        _install_fake(monkeypatch, ['{"code": "000001"}'])

        def v(parsed):
            return {"code": parsed["code"], "name": "基金A"}

        assert call_llm_json("p", max_tokens=100, fallback=None, validator=v) == {
            "code": "000001", "name": "基金A"}

    def test_validator_rejects_returns_fallback(self, monkeypatch):
        """validator 返回 None：视为无效，回退 fallback。"""
        _install_fake(monkeypatch, ['{"code": "999999"}'])

        def v(parsed):
            return None

        assert call_llm_json("p", max_tokens=100, fallback={"x": 1}, validator=v) == {"x": 1}

    def test_validator_raises_returns_fallback(self, monkeypatch):
        """validator 抛异常：视为无效，回退 fallback。"""
        _install_fake(monkeypatch, ['{"code": "000001"}'])

        def v(parsed):
            raise ValueError("bad")

        assert call_llm_json("p", max_tokens=100, fallback=None, validator=v) is None

"""统一 LLM 调用接口，包含重试逻辑。"""

import time
from log_utils import get_logger
from data_store import _load_settings

logger = get_logger("llm")


def call_llm(
    prompt: str,
    system_prompt: str = "",
    temperature: float = 0.2,
    max_tokens: int = 1024,
) -> str | None:
    """统一 LLM 调用接口，包含重试逻辑。

    重试策略：限流(429)、服务端错误(5xx)、网络瞬时异常 → 指数退避重试最多3次。
    配置缺失或客户端错误(4xx非429) → 不重试直接返回 None。
    """
    settings = _load_settings()
    llm_cfg = settings.get("llm", {})
    api_key = llm_cfg.get("api_key", "")
    if not api_key:
        logger.warning("LLM 未配置")
        return None

    t0 = time.time()
    try:
        from openai import OpenAI
        client = OpenAI(base_url=llm_cfg.get("base_url"), api_key=api_key)

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        _RETRYABLE_TERMS = ("429", "500", "502", "503", "504",
                             "RateLimit", "ConnectionError", "Timeout",
                             "Connection reset", "RemoteDisconnected")
        for attempt in range(3):
            try:
                resp = client.chat.completions.create(
                    model=llm_cfg.get("model", "gpt-4o-mini"),
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format={"type": "json_object"},
                )
                elapsed = (time.time() - t0) * 1000
                tokens = resp.usage.total_tokens if resp.usage else 0
                logger.info_event("llm_success", "LLM 调用成功",
                                  extra={"model": llm_cfg.get("model"), "attempt": attempt + 1,
                                         "duration_ms": int(elapsed), "tokens": tokens})
                return resp.choices[0].message.content
            except Exception as e:
                e_str = str(e)
                e_type = type(e).__name__
                is_retryable = any(term in e_str or term in e_type for term in _RETRYABLE_TERMS)
                if is_retryable:
                    delay = 2 ** attempt * 2
                    logger.warn_event("llm_retry", f"LLM 可重试错误 ({delay}s后重试)",
                                      extra={"attempt": attempt + 1, "delay": delay,
                                             "error_type": e_type, "error": e_str[:120]})
                    time.sleep(delay)
                    continue
                elapsed = (time.time() - t0) * 1000
                logger.error_event("llm_fatal", "LLM 不可重试错误",
                                   extra={"attempt": attempt + 1, "duration_ms": int(elapsed),
                                          "error_type": e_type, "error": e_str[:120]},
                                   exc_info=True)
                return None
        elapsed = (time.time() - t0) * 1000
        logger.error_event("llm_exhausted", "LLM 重试耗尽",
                           extra={"attempts": attempt + 1, "duration_ms": int(elapsed)})
        return None
    except Exception as e:
        elapsed = (time.time() - t0) * 1000
        logger.error_event("llm_crash", "LLM 异常",
                           extra={"duration_ms": int(elapsed), "error": str(e)},
                           exc_info=True)
        return None
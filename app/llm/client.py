"""统一 LLM 调用接口，包含重试逻辑。"""

import json
import re
import time
from app.utils.log import get_logger
from app.config import load_settings

logger = get_logger("llm")


def call_llm(
    prompt: str,
    system_prompt: str = "",
    temperature: float = 0.2,
    max_tokens: int = 1024,
) -> str | None:
    settings = load_settings()
    llm_cfg = settings.get("llm", {})
    api_key = llm_cfg.get("api_key", "")
    if not api_key:
        logger.warning("LLM 未配置")
        return None

    t0 = time.time()
    try:
        from openai import OpenAI
    except ImportError:
        logger.warning("openai 库未安装")
        return None

    base_url = llm_cfg.get("base_url", "https://api.openai.com/v1")
    model = llm_cfg.get("model", "gpt-4o-mini")
    client = OpenAI(api_key=api_key, base_url=base_url)

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    last_error = None
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            text = resp.choices[0].message.content or ""
            if not text.strip():
                # 代理偶发 200 但 content 为空：视为失败进入重试
                raise ValueError("LLM 返回空内容")
            elapsed = time.time() - t0
            logger.info("LLM 调用成功 (%d tokens, %.1fs)", resp.usage.total_tokens if resp.usage else 0, elapsed)
            return text
        except Exception as e:
            last_error = e
            status = getattr(e, "status_code", 0) or getattr(e, "code", 0)
            if status and isinstance(status, int) and 400 <= status < 500 and status != 429:
                logger.warning("LLM 客户端错误(%d)，不重试: %s", status, str(e)[:120])
                return None
            if attempt < 2:
                import random, time as _time
                delay = (2 ** attempt) + random.random()
                logger.warning("LLM 调用失败(第%d次重试, %.1f秒后): %s", attempt + 1, delay, str(e)[:120])
                _time.sleep(delay)
            else:
                logger.error("LLM 调用三次重试均失败: %s", str(e)[:200])
                return None
    return None


def parse_llm_json(content: str | None):
    """剥离 markdown 代码围栏后解析 JSON；内容为空或解析失败返回 None。"""
    if not content:
        return None
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.IGNORECASE)
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        return None


def call_llm_json(
    prompt: str,
    system_prompt: str = "",
    temperature: float = 0.2,
    max_tokens: int = 1024,
    fallback=None,
):
    """调用 LLM 并把返回解析为 JSON；调用失败或解析失败统一返回 fallback。"""
    content = call_llm(prompt, system_prompt=system_prompt, temperature=temperature, max_tokens=max_tokens)
    parsed = parse_llm_json(content)
    if parsed is None:
        return fallback
    return parsed

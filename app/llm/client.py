"""统一 LLM 调用接口，包含重试逻辑。"""

import json
import random
import re
import time
from collections.abc import Callable
from typing import Any
from app.utils.log import get_logger
from app.config import load_settings

logger = get_logger("llm")

# 审计滚动保留上限：超过后清理最旧记录，避免无界增长（LLM 审计属技术记录，不随决策域清除）
_AUDIT_MAX_ROWS = 5000


class LLMError(RuntimeError):
    """LLM 调用技术失败（配置缺失/库缺失/网络/重试耗尽）。

    架构深化候选 7：统一技术失败契约——调用方不再各自对 None 解读、
    raise 不同文案；空业务结果（解析失败等）仍走 fallback/None 路径。
    """

# ── LLM 重试策略 ──
# 第三方 LLM 服务常有暂时性故障：503 账号池不可用（ALL_ACCOUNTS_INACTIVE）、429 限流、
# 5xx、连接超时。这类错误短退避重试必然继续失败，需指数退避 + 抖动，且对
# 503/429（服务端恢复慢）加大基础间隔。4xx（鉴权/参数错误）为确定性错误不重试。
_LLM_MAX_ATTEMPTS = 6          # 初始 1 次 + 重试 5 次
_LLM_RETRY_BASE_SLOW = 15.0    # 503/429：服务端恢复慢，基础间隔（秒）
_LLM_RETRY_BASE_FAST = 2.0     # 其他暂时性错误（5xx/连接/超时）
_LLM_RETRY_MAX_DELAY = 60.0    # 单次等待上限（秒）
_LLM_TIMEOUT = 300.0           # 单次请求总超时（秒）：大 max_tokens 慢生成场景放宽，避免误杀


def _audit_write(caller: str, prompt: str, raw_output: str, parsed_result, ok: bool,
                 duration_ms: int, tokens: int) -> None:
    """写入 LLM 决策审计（P0-3）：prompt 快照 + 原始输出 + 解析结果，可复现排查。

    审计写入失败不阻断主流程（技术记录，容错丢弃）；滚动保留最近 _AUDIT_MAX_ROWS 条。
    """
    try:
        from app.database import db_conn
        preview = prompt.strip().replace("\n", " ")[:200]
        parsed_json = None
        if parsed_result is not None:
            try:
                parsed_json = json.dumps(parsed_result, ensure_ascii=False)[:2000]
            except (TypeError, ValueError):
                parsed_json = str(parsed_result)[:2000]
        with db_conn() as conn:
            conn.execute(
                "INSERT INTO llm_audit (ts, caller, prompt_hash, prompt_preview, raw_output, "
                "parsed_result, duration_ms, tokens, ok) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (time.strftime("%Y-%m-%d %H:%M:%S"), caller or "",
                 f"{len(prompt)}:{prompt[:64]}", preview,
                 (raw_output or "")[:4000], parsed_json, int(duration_ms), int(tokens or 0),
                 1 if ok else 0),
            )
            conn.execute("DELETE FROM llm_audit WHERE id NOT IN "
                         "(SELECT id FROM llm_audit ORDER BY id DESC LIMIT ?)",
                         (_AUDIT_MAX_ROWS,))
    except Exception as e:
        logger.debug("LLM 审计写入失败: %s", str(e)[:100])


def call_llm(
    prompt: str,
    system_prompt: str = "",
    temperature: float = 0.2,
    max_tokens: int = 16384,
    caller: str = "",
) -> str | None:
    """统一 LLM 调用（带重试 + 决策审计）。caller 标识调用环节（审计用）。"""
    return _call_llm(prompt, system_prompt=system_prompt, temperature=temperature,
                     max_tokens=max_tokens, caller=caller)


def _call_llm(
    prompt: str,
    system_prompt: str = "",
    temperature: float = 0.2,
    max_tokens: int = 16384,
    caller: str = "",
    parse: Callable[[str], Any] | None = None,
) -> Any:
    """LLM 调用核心（架构深化 G）：审计写入的单一 choke point。

    成功 / 解析失败 / 技术失败（未配置/缺库/4xx/重试耗尽）各写且仅写一条 llm_audit；
    parse 可选：对成功返回做后处理（JSON 解析），提供时审计记录 parsed_result，
    解析失败/validator 拒绝返回 None（ok=False）。技术失败一律抛 LLMError。
    """
    settings = load_settings()
    llm_cfg = settings.get("llm", {})
    api_key = llm_cfg.get("api_key", "")
    if not api_key:
        _audit_write(caller, prompt, "", None, False, 0, 0)
        raise LLMError("LLM 未配置")

    t0 = time.time()
    try:
        from openai import OpenAI
    except ImportError:
        _audit_write(caller, prompt, "", None, False, 0, 0)
        raise LLMError("openai 库未安装")

    base_url = llm_cfg.get("base_url", "https://api.openai.com/v1")
    model = llm_cfg.get("model", "gpt-4o-mini")
    # 显式超时（默认 600s 太久）：外部 LLM 服务挂起时快速失败重试，避免拖死管线；
    # max_retries=0：SDK 内部短退避重试交由本层统一指数退避控制，避免叠加空转。
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=_LLM_TIMEOUT, max_retries=0)

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    for attempt in range(_LLM_MAX_ATTEMPTS):
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
            tokens = resp.usage.total_tokens if resp.usage else 0
            logger.info("LLM 调用成功 (%d tokens, %.1fs)", tokens, elapsed)
            if parse is not None:
                # 解析/validator 拒绝：审计 ok=False（tokens 与原始输出同记录）
                try:
                    parsed = parse(text)
                except Exception:
                    _audit_write(caller, prompt, text, None, False, elapsed * 1000, tokens)
                    return None
                if parsed is None:
                    _audit_write(caller, prompt, text, None, False, elapsed * 1000, tokens)
                    return None
                _audit_write(caller, prompt, text, parsed, True, elapsed * 1000, tokens)
                return parsed
            _audit_write(caller, prompt, text, None, True, elapsed * 1000, tokens)
            return text
        except Exception as e:
            status = getattr(e, "status_code", 0) or getattr(e, "code", 0)
            if status and isinstance(status, int) and 400 <= status < 500 and status != 429:
                _audit_write(caller, prompt, "", None, False,
                             (time.time() - t0) * 1000, 0)
                raise LLMError(f"LLM 客户端错误({status}): {e}")
            is_last = attempt == _LLM_MAX_ATTEMPTS - 1
            if is_last:
                _audit_write(caller, prompt, "", None, False,
                             (time.time() - t0) * 1000, 0)
                logger.error("LLM 调用 %d 次重试均失败: %s", _LLM_MAX_ATTEMPTS, str(e)[:200], exc_info=True)
                raise LLMError(f"LLM 调用重试耗尽: {e}")
            # 暂时性错误：指数退避 + 抖动；503/429 服务端恢复慢，基础间隔更大
            base = _LLM_RETRY_BASE_SLOW if status in (503, 429) else _LLM_RETRY_BASE_FAST
            delay = min(base * (2 ** attempt) + random.uniform(0, 1), _LLM_RETRY_MAX_DELAY)
            logger.warning("LLM 调用失败(第%d/%d次, %.1f秒后重试): %s",
                           attempt + 1, _LLM_MAX_ATTEMPTS, delay, str(e)[:120])
            time.sleep(delay)


def parse_llm_json(content: str | None) -> Any:
    """解析 LLM 返回的 JSON；容忍 markdown 代码围栏与前后解释文字。

    依次尝试：直接解析 → 剥离任意位置代码围栏后解析 →
    提取首个顶层 JSON 片段（容忍前后夹带文字）。全部失败返回 None。
    """
    if not content or not content.strip():
        return None
    text = content.strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    # 剥离任意位置的 markdown 代码围栏（```json / ```）
    fence = re.sub(r"```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    try:
        return json.loads(fence)
    except (json.JSONDecodeError, ValueError):
        pass
    # 提取首个顶层 JSON 片段（容忍前面/后面夹带解释文字）
    decoder = json.JSONDecoder()
    for ch in ("{", "["):
        idx = fence.find(ch)
        while idx >= 0:
            try:
                return decoder.raw_decode(fence[idx:])[0]
            except json.JSONDecodeError:
                idx = fence.find(ch, idx + 1)
    return None


def call_llm_json(
    prompt: str,
    system_prompt: str = "",
    temperature: float = 0.2,
    max_tokens: int = 16384,
    fallback: Any = None,
    validator: Callable[[Any], Any] | None = None,
    caller: str = "",
) -> Any:
    """调用 LLM 并把返回解析为 JSON；技术失败抛 LLMError（候选 7），
    解析失败/validator 拒绝返回 fallback。

    validator 可选：对解析结果做语义校验，返回清洗后的结果；返回 None 或抛异常视为无效。
    审计写入收敛在 _call_llm 单一 choke point（架构深化 G）：成功/解析失败各一条，
    技术失败也各写一条 ok=False——消除双写，tokens 与解析结果同记录。
    """
    def _parse(content: str) -> Any:
        parsed = parse_llm_json(content)
        if parsed is None:
            return None
        if validator is not None:
            try:
                parsed = validator(parsed)
            except Exception:
                return None
            if parsed is None:
                return None
        return parsed

    result = _call_llm(prompt, system_prompt=system_prompt, temperature=temperature,
                       max_tokens=max_tokens, caller=caller, parse=_parse)
    if result is None:
        return fallback
    return result

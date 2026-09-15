"""票 13：LLM 审计 JSON 校验、剪枝与复合分（纯函数）。

- 严格 schema 校验：`audit_verdict ∈ {PASS, CONDITIONAL_PASS, VETO}`、
  `risk_score` 0–100、VETO 必须带理由、`recommendation_summary` ≤ 50 字
- **解析失败绝不静默降级**（沿用 1.x monitor 原则）：非法值按失败处理
- 剪枝：`VETO` 或 `risk_score > 60` → 剔除（60 边界不剔）
- 复合分：`Final = LGBM × (1 − Risk/100)`，取 Top5

prompt 模板与 LLM 调用走 `app/llm/client.py` 单接口（ADR-0002），接线在引擎层。
"""

VALID_VERDICTS = {"PASS", "CONDITIONAL_PASS", "VETO"}
PRUNE_RISK = 60.0            # risk_score > 60 → 剔除（边界 60 不剔）
SUMMARY_MAX_CHARS = 50
TOP_N = 5


def validate_audit(raw: dict) -> tuple[bool, str]:
    """严格 JSON schema 校验。返回 (ok, error)；非法 → 按失败处理（不静默降级）。

    校验：audit_verdict 合法枚举；risk_score 为数字且在 [0,100]；
    VETO 时必须带非空 veto_reasons；recommendation_summary 不超过 50 字。
    """
    verdict = raw.get("audit_verdict")
    if verdict not in VALID_VERDICTS:
        return False, f"audit_verdict 非法: {verdict!r}（须 ∈ {sorted(VALID_VERDICTS)}）"
    rs = raw.get("risk_score")
    if not isinstance(rs, (int, float)) or isinstance(rs, bool):
        return False, f"risk_score 非数字: {rs!r}"
    if not 0.0 <= float(rs) <= 100.0:
        return False, f"risk_score 越界: {rs}（须 0–100）"
    if verdict == "VETO" and not raw.get("veto_reasons"):
        return False, "VETO 必须带非空 veto_reasons"
    summary = raw.get("recommendation_summary") or ""
    if len(str(summary)) > SUMMARY_MAX_CHARS:
        return False, f"recommendation_summary 超长（> {SUMMARY_MAX_CHARS} 字）"
    return True, ""


def prune(verdict: str, risk_score: float) -> bool:
    """剪枝判定：VETO 或 risk_score > 60 → 剔除。边界：60 保留、60.01 剔除。"""
    return verdict == "VETO" or risk_score > PRUNE_RISK


def composite_score(lgbm_score: float, risk_score: float) -> float:
    """复合分 Final_Score = Score_LGBM × (1 − Risk_Score/100)（票 13）。"""
    return lgbm_score * (1.0 - risk_score / 100.0)


def top_n(scored: list[dict], n: int = TOP_N) -> list[dict]:
    """按 final_score 降序取 TopN（调用方已剪枝）。"""
    return sorted(scored, key=lambda x: x["final_score"], reverse=True)[:n]


# ── 票 14：事件驱动的审计缓存（失效条件三元组）────────────────
# 把 LLM 触发从“时钟”改成“变化”：同一天同一基金两次进入 Top30，
# 只要失效条件三元组没变就复用上次审计，不重复烧 LLM。
# 三元组 = 持仓期次 / 重仓股集合指纹 / 风险事件版本。


def cache_fingerprint(holdings_period: str | None, stock_fingerprint: str,
                      event_version: str | None) -> str:
    """失效条件三元组 → 缓存指纹（任一变化 → 指纹变化 → 失效重跑）。"""
    return f"{holdings_period or '-'}|{stock_fingerprint}|{event_version or '-'}"


def is_cache_valid(cached: dict | None, current: dict) -> bool:
    """缓存读取校验：无缓存或任一失效条件不一致 → 失效（重跑）。

    current 含 holdings_period / stock_fingerprint / event_version。
    """
    if not cached:
        return False
    for key in ("holdings_period", "stock_fingerprint", "event_version"):
        if cached.get(key) != current.get(key):
            return False
    return True


def should_call_llm(is_new_entry: bool, cached: dict | None, current: dict) -> bool:
    """触发规则（票 14）：基金新进 Top30 **或** 任一失效条件变化 → 调用 LLM。

    否则复用缓存（同一天两次跑，LLM 桩只被调用一次——决策周期入口断言）。
    """
    if is_new_entry:
        return True
    return not is_cache_valid(cached, current)

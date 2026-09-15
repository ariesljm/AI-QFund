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

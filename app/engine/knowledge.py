"""票 17：知识库（Bad-Case 与 Good-Case）纯函数核心。

唯一能单调增长的资产——复盘归因 → Few-Shot 回流（2.0 §5.2）。
知识层**只增不改**（ADR-0009）：案例只追加、不修改已有案例。

本模块是纯函数核心：归因分叉判定 / 案例结构化 / 条件检索 / 回流装配。
落库与触发（推荐后异常回撤 > 8% 或触发 EXIT）由决策周期入口接线。
"""

# 特征滞后阈值（交易日）：特征日期滞后决策日超过该值 → 归因"特征失效"
FEATURE_LAG_TRADE_DAYS = 10

# 回流上限：活跃案例注入 prompt 的字符预算（防 prompt 膨胀）
FEW_SHOT_MAX_CHARS = 2000


def attribute_case(lag_days: int | None) -> str:
    """归因分叉（票 17）：量化特征滞后 vs 非结构化隐患漏判。

    lag_days: 特征日期滞后决策日的天数（由特征新鲜度闸门口径给出）；None = 特征缺失。
    - lag_days is None 或 > FEATURE_LAG_TRADE_DAYS → "feature_stale"（记特征失效，
      不是案例——问题在数据，不在判断）
    - 否则 → "hidden_risk"（特征正常却仍异常 → 非结构化隐患漏判，生成标准 Bad-Case）

    触发条件（异常回撤 > 8% 或 EXIT）由调用方保证成立，本函数只分叉不触发。
    """
    if lag_days is None or lag_days > FEATURE_LAG_TRADE_DAYS:
        return "feature_stale"
    return "hidden_risk"


def make_case(case_type: str, decision_date: str, fund: str,
              industry: str | None, audit: dict | None,
              outcome: dict) -> dict:
    """案例结构化（票 17）：可检索的条件字段 + 出处，而非纯文本。

    case_type: "bad" / "good"；decision_date/fund/industry：检索条件；
    audit: 当日审计 JSON（出处）；outcome: 实际结果（40 日收益/是否异常）。
    """
    return {"type": case_type, "decision_date": decision_date, "fund": fund,
            "industry": industry or "", "audit": audit, "outcome": outcome}


def retrieve_cases(cases: list[dict], case_type: str | None = None,
                   industry: str | None = None, limit: int = 5) -> list[dict]:
    """条件检索：按类别/行业过滤（可检索条件字段，非纯文本匹配）。

    返回满足条件的案例（最多 limit 条，按入库顺序取最近）；不改动原列表。
    """
    out = [c for c in cases
           if (case_type is None or c.get("type") == case_type)
           and (industry is None or c.get("industry") == industry)]
    return out[-limit:] if limit and limit > 0 else out


def assemble_few_shot(cases: list[dict]) -> str:
    """活跃案例 → 排雷 prompt 注入文本（确定性格式，票 17 回流）。

    每条：类别 + 决策日 + 基金 + 行业 + 审计要点 + 结果。
    超过 FEW_SHOT_MAX_CHARS 截断（预算保护），不足部分不补。
    """
    parts: list[str] = []
    for c in cases:
        kind = "BAD" if c.get("type") == "bad" else "GOOD"
        audit = c.get("audit") or {}
        reasons = audit.get("veto_reasons") or audit.get("audit_details") or "—"
        outcome = c.get("outcome") or {}
        parts.append(
            f"[{kind}] {c.get('decision_date', '?')} {c.get('fund', '?')} "
            f"({c.get('industry', '?')}) 审计: {reasons} → 实际: {outcome}")
    text = "\n".join(parts)
    if len(text) > FEW_SHOT_MAX_CHARS:
        text = text[:FEW_SHOT_MAX_CHARS] + "…（已截断）"
    return text

"""票 11 完整：2.0 推荐主流程（并行路径，不替换 1.x）。

screen_top30（候选池）→ LLM 排雷审计（可注入打桩）→ 剪枝/复合分 → Top5 落库。
审计走 app/llm/client.py 单接口（ADR-0002），prompt 模板（票 13）与
四组切片装配（票 12）均已就绪。回填完成后调度器切到本路径；
2.0 特征（票 08 v2_assembly）与超额模型（票 09）就绪后替换打分来源。

返回 {"date", "top5": [...]} 或 {"date", "empty": "no_opportunity"/"data_failure"}。
"""

from app.engine.screen_pipeline import screen_top30
from app.llm.audit import composite_score, prune, top_n, validate_audit
from app.repo import decision as decision_repo
from app.repo.decision import get_screen_candidates


def default_audit(fund: dict, slices: dict) -> dict | None:
    """真实 LLM 审计（client.py 单接口；validator=validate_audit，非法即无效）。

    slices: 四组切片文本（由调用方按基金装配；测试可注入简化版）。
    """
    from app.llm.client import call_llm_json
    from app.llm.prompts import audit_system_prompt, audit_user_prompt

    def _validator(parsed):
        ok, _ = validate_audit(parsed)
        return parsed if ok else None

    return call_llm_json(
        audit_user_prompt(fund, slices),
        system_prompt=audit_system_prompt(),
        caller="recommend_v2_audit",
        fallback=None,
        validator=_validator,
    )


def recommend_top5(today: str, audit_fn=None, scorer=None) -> dict:
    """2.0 推荐主流程：Top30 → 审计 → 剪枝/复合分 → Top5 落库。

    audit_fn: (fund, slices) -> audit dict | None（None/非法 → 跳过，不静默降级）；
    缺省 default_audit（LLM）。slices 由 audit_fn 内部装配（保单一归属）。
    scorer: 候选打分函数（透传 screen_top30；缺省 model.score）。
    """
    screened = screen_top30(today, scorer=scorer)
    if "empty" in screened:
        return {"date": today, "empty": screened["empty"]}
    candidates = get_screen_candidates(today)
    audit_fn = audit_fn or default_audit
    ranked: list[dict] = []
    for cand in candidates:
        slices = _slices_for(cand)
        audit = audit_fn(cand, slices)
        if not audit:
            continue                                  # 技术失败/非法 → 跳过
        if prune(audit.get("audit_verdict"), audit.get("risk_score") or 0):
            continue                                  # VETO 或 risk>60 → 剔除
        ranked.append({"code": cand["code"],
                       "final_score": composite_score(cand["score"],
                                                      audit.get("risk_score") or 0),
                       "audit": audit})
    if not ranked:
        return {"date": today, "empty": "data_failure"}   # 全被剪枝/审计失败
    top5 = top_n(ranked, 5)
    decision_repo.save_recommend_v2(today, top5)
    return {"date": today, "top5": [t["code"] for t in top5]}


def _slices_for(cand: dict) -> dict:
    """四组切片装配（票 12；缺失切片明示）。测试可整体替换 audit_fn 绕过。"""
    code = cand["code"]
    from app.data.announcements import risk_radar_text
    from app.data.sentiment import sentiment_text
    from app.llm.context import holdings_change_snapshot
    from app.repo.base import get_holdings_two_periods

    cur, prev = get_holdings_two_periods(code, 10)   # 最近两期（04 回填后真实对比）
    return {
        "holdings_change": holdings_change_snapshot(cur, prev),
        "risk_radar": risk_radar_text([{"stock_code": h["stock_code"],
                                        "stock_name": h["stock_name"]} for h in cur]),
        "management": None,     # 切片三待定（未决 #4）
        "sentiment": sentiment_text([{"stock_code": h["stock_code"],
                                      "stock_name": h["stock_name"]} for h in cur]),
    }


def recommend_v2_enabled() -> bool:
    """2.0 推荐开关（settings.toml [recommend_v2].enabled；默认关闭不破坏 1.x）。"""
    from app.config import load_settings
    return bool(load_settings().get("recommend_v2", {}).get("enabled", False))

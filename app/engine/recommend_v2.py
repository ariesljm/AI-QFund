"""票 11 完整：2.0 推荐主流程（并行路径，不替换 1.x）。

screen_top30（候选池）→ LLM 排雷审计（可注入打桩）→ 剪枝/复合分 → Top5 落库。
审计走 app/llm/client.py 单接口（ADR-0002），prompt 模板（票 13）与
四组切片装配（票 12）均已就绪。回填完成后调度器切到本路径；
2.0 特征（票 08 v2_assembly）与超额模型（票 09）就绪后替换打分来源。

返回 {"date", "top5": [...]} 或 {"date", "empty": "no_opportunity"/"data_failure"}。
"""

from app.engine.screen import select_diversified
from app.engine.screen_pipeline import screen_top30
from app.llm.audit import composite_score, prune, top_n, validate_audit
from app.repo import decision as decision_repo
from app.repo.decision import get_screen_candidates
from app.utils.log import get_logger

logger = get_logger("recommend_v2")


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


def recommend_top5(today: str, audit_fn=None, scorer=None, cid: str = "") -> dict:
    """2.0 推荐主流程：Top30 → 审计 → 剪枝/复合分 → Top5 落库。

    audit_fn: (fund, slices) -> audit dict | None（None/非法 → 跳过，不静默降级）；
    缺省 default_audit（LLM）。slices 由 audit_fn 内部装配（保单一归属）。
    scorer: 候选打分函数（透传 screen_top30；缺省 model.score）。
    cid: correlation id，随 pipeline 贯穿，供 web 报告按批次聚合溯源。
    """
    log = logger.with_cid(cid)
    screened = screen_top30(today, scorer=scorer)
    if "empty" in screened:
        log.warn_event("recommend_empty", f"今日无推荐：{screened['empty']}",
                       extra={"reason": screened["empty"]})
        return {"date": today, "empty": screened["empty"]}
    candidates = get_screen_candidates(today)
    log.info_event("screen_done", f"初筛候选 {len(candidates)} 只进入 LLM 排雷",
                   extra={"candidate_count": len(candidates)})
    audit_fn = audit_fn or default_audit
    ranked: list[dict] = []
    vetoed = 0
    for cand in candidates:
        slices = _slices_for(cand)
        audit = audit_fn(cand, slices)
        if not audit:
            log.warn_event("audit_failed", f"{cand['code']} 审计失败/非法，跳过",
                           extra={"code": cand["code"]})
            continue
        verdict = audit.get("audit_verdict")
        risk = audit.get("risk_score") or 0
        if prune(verdict, risk):
            vetoed += 1
            log.info_event("audit_vetoed", f"{cand['code']} {verdict} risk={risk} 剪枝",
                           extra={"code": cand["code"], "verdict": verdict, "risk": risk})
            continue                                  # VETO 或 risk>60 → 剔除
        log.info_event("audit_pass", f"{cand['code']} {verdict} risk={risk} 通过",
                       extra={"code": cand["code"], "verdict": verdict, "risk": risk})
        ranked.append({"code": cand["code"],
                       "final_score": composite_score(cand["score"], risk),
                       "audit": audit,
                       "features": cand.get("features", {})})
    if not ranked:
        log.warn_event("recommend_empty", f"全部 {len(candidates)} 候选被剪枝/审计失败，无推荐",
                       extra={"reason": "data_failure", "vetoed": vetoed})
        return {"date": today, "empty": "data_failure"}   # 全被剪枝/审计失败
    top5 = _select_top5(ranked, cid=cid)
    for t in top5:
        summary = (t["audit"].get("recommendation_summary") or t["audit"].get("audit_details") or "")[:120]
        log.info_event("recommend_pick", f"Top5 推荐 {t['code']} 复合分={t['final_score']:.3f} 理由：{summary}",
                       extra={"code": t["code"], "score": round(t["final_score"], 3), "summary": summary})
    decision_repo.save_recommend_v2(today, top5)
    log.info_event("recommend_done", f"今日推荐 {len(top5)} 只落库（剪枝 {vetoed}）",
                   extra={"top5": [t["code"] for t in top5], "vetoed": vetoed})
    return {"date": today, "top5": [t["code"] for t in top5]}


def _select_top5(ranked: list[dict], cid: str = "") -> list[dict]:
    """票 02：组合层选 5——贪心去相关 + 同类≤2（可配关闭回退纯分数 top_n）。

    ranked: [{"code","final_score","audit","features"}]。按 final_score 降序后
    走 select_diversified；相关性用最近 60 交易日净值收益，同类用 features
    快照里的 rbsa_industry_1（RBSA 第一行业）。数据缺失不约束（不误杀）。
    """
    from app.config import load_settings
    from app.repo import nav as nav_repo
    from app.features.stats import pearson

    ordered = sorted(ranked, key=lambda x: x["final_score"], reverse=True)
    cfg = load_settings().get("recommend_v2", {})
    if not cfg.get("portfolio_diversify", False):
        return top_n(ordered, 5)
    max_corr = float(cfg.get("portfolio_max_corr", 0.85))
    max_same = int(cfg.get("portfolio_max_same_peer", 2))

    ret_cache: dict[str, dict[str, float] | None] = {}
    def _returns(code: str) -> dict[str, float] | None:
        if code in ret_cache:
            return ret_cache[code]
        rows = nav_repo.series(code, limit=61)
        ret = {}
        for i in range(1, len(rows)):
            d, v0 = rows[i - 1]; v1 = rows[i][1]
            if v0 and v1 and v0 > 0:
                ret[d] = v1 / v0 - 1.0
        ret_cache[code] = ret or None
        return ret_cache[code]

    def corr_fn(c1: str, c2: str) -> float | None:
        r1, r2 = _returns(c1), _returns(c2)
        if not r1 or not r2:
            return None
        common = sorted(set(r1) & set(r2))
        if len(common) < 20:
            return None
        x = [r1[d] for d in common]; y = [r2[d] for d in common]
        r, _ = pearson(x, y)
        return float(r)

    feat_by_code = {r["code"]: r.get("features", {}) for r in ranked}
    def peer_fn(code: str) -> str | None:
        return feat_by_code.get(code, {}).get("rbsa_industry_1")

    return select_diversified(ordered, corr_fn, peer_fn,
                              max_corr=max_corr, max_same_peer=max_same, n=5)


def _slices_for(cand: dict) -> dict:
    """四组切片装配委托 llm/context 单一来源（ADR-0003）；测试可整体替换 audit_fn 绕过。"""
    from app.llm.context import assemble_audit_slices
    return assemble_audit_slices(cand["code"])


def recommend_v2_enabled() -> bool:
    """2.0 推荐开关（settings.toml [recommend_v2].enabled；默认关闭不破坏 1.x）。"""
    from app.config import load_settings
    return bool(load_settings().get("recommend_v2", {}).get("enabled", False))

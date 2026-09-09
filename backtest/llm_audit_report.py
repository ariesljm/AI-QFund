"""LLM 决策质量审计报表（reco-hardening T07）。

回答审计问题：LLM 否决的基金后来表现如何？是否系统性错杀上涨基金？
- 否决记录：recommend_log.vetoed_json（T07 结构化落库）
- 选中记录：recommend_log 本身（结算后 outcome 由 evolve 层写入）

输出 data/llm_audit_report.json：
- veto_summary：被否决基金的平均 40 日收益、错杀率（被否决但后来上涨的比例）
- pick_summary：LLM 选中 vs 候选池均值的裁决损耗趋势（Q5 复用 quality_metrics）
"""

import json
from datetime import datetime


def parse_vetoed(vetoed_json: str) -> list[dict]:
    """解析结构化否决记录 [{code,name,reason}]；异常返回空（防御）。"""
    if not vetoed_json:
        return []
    try:
        rows = json.loads(vetoed_json)
        return [r for r in rows if isinstance(r, dict) and r.get("code")]
    except (json.JSONDecodeError, TypeError):
        return []


def build_veto_report(limit: int = 300) -> dict:
    """否决质量报表：被否决基金回查 40 日收益。"""
    import app.repo as repo

    rows = repo.get_vetoed_audit_rows(limit)
    vetoed_cases: list[dict] = []
    for reco_date, sel_code, _sel_name, veto_json in rows:
        for v in parse_vetoed(veto_json):
            code = v["code"]
            ret = repo.nav.forward_return(code, reco_date)
            if ret is None:
                continue
            vetoed_cases.append({
                "date": reco_date, "vetoed_code": code,
                "vetoed_name": v.get("name", ""), "reason": v.get("reason", ""),
                "selected_code": sel_code, "ret_40d": round(float(ret), 6),
            })
    n = len(vetoed_cases)
    if n == 0:
        return {"n_cases": 0, "note": "暂无结构化否决记录（T07 落库后随推荐积累）"}

    rets = [c["ret_40d"] for c in vetoed_cases]
    avg = sum(rets) / n
    missed = sum(1 for r in rets if r > 0)  # 被否决但后来上涨 = 错杀
    return {
        "n_cases": n,
        "avg_ret_40d_pct": round(avg * 100, 2),
        "missed_rate_pct": round(missed / n * 100, 1),
        "top_missed": sorted(vetoed_cases, key=lambda c: c["ret_40d"], reverse=True)[:5],
        "note": "missed_rate 高 = LLM 系统性错杀上涨基金（应收紧否决权）；低 = 否决质量好",
    }


def build_decision_loss_report(months: int = 6) -> dict:
    """裁决损耗趋势：LLM 选中 vs 候选池均值（复用 quality_metrics.decision_loss）。"""
    import app.repo as repo

    rows = repo.get_quality_metrics(months)
    series = []
    for r in rows:
        series.append({"date": r.get("computed_date"),
                       "decision_loss": r.get("decision_loss"),
                       "sample_count": r.get("sample_count")})
    return {"months": months, "series": series}


if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")
    report = {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
              "veto_report": build_veto_report(),
              "decision_loss": build_decision_loss_report()}
    with open("data/llm_audit_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(json.dumps(report, ensure_ascii=False, indent=1))

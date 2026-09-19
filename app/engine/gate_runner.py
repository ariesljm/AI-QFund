"""影子闸门 2.0 接线（票 19）：LLM 模型版本 Challenger 判定 + 切换台账。

判定纯函数在 app/engine/gate.py（gate_decision，已实现且带"必须能失败"测试）；
本模块是接线层：取 champion/challenger 版本、装配非重叠窗口 IR、调 gate_decision、
通过则写 settings 切模型 + 落台账。

版本标识 = LLM model@base_url（settings.toml [llm]）；2.0 比较对象是 LLM 模型版本
（排序配置只影响初筛 Top30，最终 Top5 由 LLM 审计决定，见 B 设计）。

challenger 窗口回测（用 challenger LLM 重审计历史 Top30 候选 → challenger 组合 →
窗口 IR）是重活（历史候选 + LLM API + 非确定性），单独接缝 _challenger_windows()。
"""

from app.utils.log import get_logger

logger = get_logger("gate_runner")


def llm_version() -> str:
    """当前 LLM 版本标识 = model@base_url（从 settings [llm] 读）。"""
    from app.config import load_settings
    s = load_settings().get("llm", {})
    return f"{s.get('model', '')}@{s.get('base_url', '')}"


def _champion_windows() -> list[float]:
    """champion 窗口 IR 序列：历史推荐组合各期 IC（quality_metrics 按时间正序）。"""
    from app.repo.quality_metric import get_quality_metrics
    ms = get_quality_metrics(limit=60)
    return [m.get("ic") for m in reversed(ms) if m.get("ic") is not None]


def _challenger_windows(challenger_version: str) -> list[float]:
    """challenger 窗口 IR：用 challenger LLM 重审计历史候选 → challenger 组合 → 窗口 IR。

    TODO（下轮）：历史 Top30 候选重跑 challenger LLM 审计（复用 recommend_v2.audit_fn
    注入 challenger model），算 challenger 推荐组合的滚动窗口超额 IR。本轮返回空 → gate 拒绝。
    """
    return []


def _promote(challenger_version: str) -> bool:
    """把 settings.toml [llm] model 切到 challenger（上线；保留 base_url/api_key）。"""
    from pathlib import Path
    p = Path("config/settings.toml")
    if not p.exists():
        return False
    model = challenger_version.split("@", 1)[0]
    lines = p.read_text(encoding="utf-8").splitlines()
    out, changed = [], False
    in_llm = False
    for ln in lines:
        if ln.strip().startswith("[llm]"):
            in_llm = True
            out.append(ln)
            continue
        if in_llm and ln.strip().startswith("["):
            in_llm = False
        if in_llm and ln.strip().startswith("model"):
            out.append(f'model = "{model}"')
            changed = True
            continue
        out.append(ln)
    if changed:
        p.write_text("\n".join(out) + "\n", encoding="utf-8")
    return changed


def run_shadow_gate(cid: str = "") -> dict:
    """影子闸门编排：版本 → 窗口 → gate_decision → 切换 + 台账。"""
    log = logger.with_cid(cid)
    champ = llm_version()
    from app.config import load_settings
    challenger = load_settings().get("recommend_v2", {}).get("challenger_model")
    if not challenger:
        log.info_event("gate_skip", "无 Challenger 候选配置（[recommend_v2].challenger_model），跳过影子闸门")
        return {"passed": False, "reasons": ["no challenger"], "p": None}

    champ_win = _champion_windows()
    chal_win = _challenger_windows(challenger)

    from app.engine.gate import gate_decision
    decision = gate_decision([], chal_win, champ_win, challenger, champ)

    action = "rejected"
    if decision["passed"]:
        action = "promoted" if _promote(challenger) else "rejected"
        if action == "rejected":
            decision["reasons"].append("写 settings 切换失败")

    from app.repo.champion import record_decision
    record_decision(champ, challenger, champ_win, chal_win, [], decision, action)

    log.info_event("gate_decision",
                   f"影子闸门 {champ} vs {challenger}: {'通过' if decision['passed'] else '拒绝'} {decision['reasons']}",
                   extra={"champion": champ, "challenger": challenger,
                          "passed": decision["passed"], "p": decision.get("p")})
    return decision

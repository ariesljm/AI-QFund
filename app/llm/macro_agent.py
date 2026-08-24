"""宏观分析 agent：多源数据 → LLM 选赛道 → MacroContext。

输出供 recommend.py（推荐）和 monitor.py（监控）直接消费。
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime

import app.repo as repo
from app import domain
from app.data.macro import fetch_macro_inputs
from app.engine.sector_pool import SectorPool, build_sector_pool
from app.llm.client import call_llm_json
from app.llm.context import market_technical_text, pool_text
from app.llm.prompts import sector_selection_prompt, sector_selection_system_prompt
from app.utils.log import get_logger

logger = get_logger("macro_agent")



@dataclass(frozen=True)
class MacroContext:
    news_summary: str = ""
    recommended_sectors: list[str] = field(default_factory=list)
    risk_sectors: list[str] = field(default_factory=list)
    sector_reasoning: str = ""
    regime_label: str = "neutral"
    date: str = ""
    top_flows: list[dict] = field(default_factory=list)
    top_outflows: list[dict] = field(default_factory=list)
    # Q4 反馈回路：选赛道 prompt 实际携带的 sector 洞察 id（推荐入库时写入 sector_selections）
    used_sector_insight_ids: list[int] = field(default_factory=list)
    # D5 量化定池：候选池信号、LLM 否决记录、定池摘要（否决质量可度量）
    candidate_sectors: list[dict] = field(default_factory=list)
    vetoed_sectors: list[dict] = field(default_factory=list)
    pool_reasoning: str = ""


def build_macro_context(date_str: str | None = None) -> MacroContext:
    """聚合多源数据 + LLM 选赛道，返回宏观上下文。

    每次调用实时抓取（缓存已移除，避免同一天重复触发时用旧快照）：
    新闻/资金流/板块均为当天数据；部署后一天一次推荐，实时抓取成本可接受。
    """
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")

    inputs = fetch_macro_inputs(date_str)
    news = inputs.news
    flow = inputs.flow

    ctx = _suggest_quant(date_str, news, flow)
    if not ctx.recommended_sectors:
        # 空赛道是合法决策（空推荐日）：LLM 显式判定今日无合适机会
        logger.info("LLM 显式判定今日无合适赛道，作为空推荐日处理")
    # 写入当日宏观上下文快照（覆盖式）：Web 面板"AI赛道分析"展示数据源，非缓存——
    # 每次推荐都实时抓取，快照仅供展示，读路径已删除
    _save_snapshot(ctx)
    return ctx


# ── 当日宏观快照（Web 展示数据源，覆盖式写入） ──

def _save_snapshot(ctx: MacroContext) -> None:
    repo.save_context(ctx.date, asdict(ctx))




# ── 选赛道输入 ──


def _load_sector_insights() -> list[tuple[int, str]]:
    return repo.get_sector_insights()


def _load_available_sectors() -> list[str]:
    return repo.get_available_sectors()


def _build_sector_prompt(date_str: str, news: dict, flow: dict,
                         pool: "SectorPool") -> tuple[str, list[int]]:
    """构建选赛道 prompt，返回 (prompt, 使用的 sector 洞察 id 列表)。

    读取即标记 apply（Q4）：进入 prompt 的洞察 apply_count+1、last_applied_date 更新；
    ids 随 prompt 链路传到推荐入库，供月度结算结果关联调 confidence。
    候选池由量化定池（sector_pool）产出，LLM 只在池内选择/否决（D5）。
    """
    insight_rows = _load_sector_insights()
    if insight_rows:
        repo.mark_insights_applied([i for i, _ in insight_rows], date_str)
    lessons = "\n".join(f"  - {t}" for _, t in insight_rows) or None
    tech = repo.get_market_technical()
    prompt = sector_selection_prompt(
        date_str=date_str,
        pool_text=pool_text([(c.sector, c.mom_5d, c.mom_20d, c.mom_60d, c.n, c.flags)
                            for c in pool.candidates]),
        pool_reasoning=pool.reasoning,
        top_gainers=news.get("top_gainers", ""),
        top_losers=news.get("top_losers", ""),
        etf_net_flow=news.get("etf_net_flow", ""),
        news_summary=news.get("summary", ""),
        flow_summary=flow.get("summary"),
        lessons=lessons,
        market_tech=market_technical_text(tech) if tech else None,
        news_date=news.get("news_date") or date_str,
    )
    return prompt, [i for i, _ in insight_rows]


def _suggest_quant(date_str: str, news: dict, flow: dict) -> MacroContext:
    """D5 新策略：量化定池 + LLM 池内选择/否决。"""
    pool = build_sector_pool(date_str)
    if not pool.candidates:
        logger.info("量化定池无候选赛道（%s），作为空推荐日处理", pool.reasoning)
        return MacroContext(
            news_summary=news.get("summary", ""),
            recommended_sectors=[],
            risk_sectors=[],
            sector_reasoning=pool.reasoning or "量化定池无候选赛道",
            regime_label="neutral",
            date=date_str,
            top_flows=flow.get("top_flows", []),
            top_outflows=flow.get("top_outflows", []),
            pool_reasoning=pool.reasoning,
        )

    prompt, used_insight_ids = _build_sector_prompt(date_str, news, flow, pool)
    system_prompt = sector_selection_system_prompt()
    # 候选3 收敛：LLM 调用统一走 call_llm_json（唯一接口）——解析失败记审计 ok=False，
    # 与原 call_llm + parse_llm_json 两步走（解析在审计外、失败记 ok=True）语义对齐；
    # 技术失败仍抛 LLMError（候选 7），非 JSON 对象的业务判定仍由 _resolve 抛 RuntimeError
    parsed = call_llm_json(prompt, system_prompt=system_prompt, max_tokens=16384,
                           fallback=None, caller="macro_sector_pick")
    return _resolve_sector_selections(
        parsed, _load_available_sectors(), pool, date_str, news, flow,
        used_insight_ids)


def _resolve_sector_selections(
    parsed,
    available: list[str],
    pool: "SectorPool | None",
    date_str: str,
    news: dict,
    flow: dict,
    used_insight_ids: list[int],
) -> MacroContext:
    """LLM 赛道输出 → 校验/过滤 → 空推荐日/告警 → MacroContext（窄 seam，架构深化 H）。

    pool=None 为 free 模式（无池内限制、无候选池字段）；veto/regime 量化覆盖两模式统一。
    业务过滤异常（KeyError 等）原样传播，不伪装成 LLM 解析失败；
    LLM 输出非 JSON 对象才视为解析失败（空推荐日由业务过滤判定）。
    """
    if not isinstance(parsed, dict):
        raise RuntimeError("LLM 赛道选择未返回 JSON 对象（返回数组或其他结构）")
    pool_names = set(pool.candidate_names) if pool else None

    # regime：纯技术规则（close vs EMA60）以代码判定为准，量化覆盖 LLM 输出
    quant_regime = repo.get_market_regime()
    llm_regime = parsed.get("regime_label", "neutral")
    regime_label = quant_regime if quant_regime else domain.normalize_regime_label(llm_regime)
    if quant_regime and domain.normalize_regime_label(llm_regime) != quant_regime:
        logger.info("regime 量化覆盖: LLM=%s → 量化=%s", llm_regime, quant_regime)

    policy = domain.SectorPolicy(available)

    def _resolve(raw_list) -> list[str]:
        """赛道名解析：经 SectorPolicy 别名映射，池模式限池内。"""
        if not isinstance(raw_list, list):
            return []
        return policy.resolve([s for s in raw_list if isinstance(s, str)], pool_names)

    rec_raw = parsed.get("recommended_sectors", [])
    risk_raw = parsed.get("risk_sectors", [])
    rec_valid = _resolve(rec_raw)
    risk_valid = _resolve(risk_raw)

    # 否决优先于推荐：被否决的赛道一律不进推荐（free 模式同样覆盖）
    vetoed_valid = []
    for v in parsed.get("vetoed_sectors", []) if isinstance(parsed.get("vetoed_sectors", []), list) else []:
        if not isinstance(v, dict):
            continue
        matched = policy.resolve([str(v.get("sector", ""))], pool_names)
        if matched:
            vetoed_valid.append({"sector": matched[0], "reason": str(v.get("reason", ""))[:200]})
    vetoed_names = {v["sector"] for v in vetoed_valid}
    rec_valid = [s for s in rec_valid if s not in vetoed_names]

    flow_top = flow.get("top_flows", [])
    flow_out = flow.get("top_outflows", [])
    common = dict(
        news_summary=news.get("summary", ""),
        risk_sectors=risk_valid,
        regime_label=regime_label,
        date=date_str,
        top_flows=flow_top,
        top_outflows=flow_out,
        used_sector_insight_ids=used_insight_ids,
    )

    if rec_raw and not rec_valid:
        # 全无效是 LLM 输出质量的合法业务结果：作为空推荐日处理（不崩溃），
        # 与"LLM 判定无机会"同路径，reasoning 注明原因便于回溯。
        logger.warning("LLM 推荐赛道均无效%s，作为空推荐日处理: %s",
                       "（不在池内或全被否决）" if pool else "（不可投）", rec_raw)
        return MacroContext(
            recommended_sectors=[],
            sector_reasoning=parsed.get("reasoning", "")
            or f"LLM 推荐赛道均无效: {rec_raw}",
            candidate_sectors=[asdict(s) for s in pool.candidates] if pool else [],
            vetoed_sectors=vetoed_valid,
            pool_reasoning=pool.reasoning if pool else "",
            **common,
        )

    # 去重保序后，仅当确有无效赛道被过滤时才告警（重复名去重不应触发）
    if rec_valid and set(rec_raw) - set(rec_valid):
        logger.warning("LLM推荐了无效赛道，已过滤: %s", set(rec_raw) - set(rec_valid))
    if risk_valid and set(risk_raw) - set(risk_valid):
        logger.warning("LLM回避了无效赛道，已过滤: %s", set(risk_raw) - set(risk_valid))
    if parsed.get("vetoed_sectors") and not vetoed_valid:
        logger.warning("LLM否决均无效（池外或格式错误）: %s", parsed.get("vetoed_sectors"))

    return MacroContext(
        recommended_sectors=rec_valid,
        sector_reasoning=parsed.get("reasoning", ""),
        candidate_sectors=[asdict(s) for s in pool.candidates] if pool else [],
        vetoed_sectors=vetoed_valid,
        pool_reasoning=pool.reasoning if pool else "",
        **common,
    )



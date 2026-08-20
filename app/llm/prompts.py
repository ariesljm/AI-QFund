"""LLM prompt 模板模块：所有 prompt 集中管理，业务逻辑不内嵌 prompt 字符串。"""

from typing import Any


def sector_selection_prompt(
    date_str: str,
    pool_text: str,
    pool_reasoning: str,
    top_gainers: str,
    top_losers: str,
    etf_net_flow: str,
    news_summary: str,
    flow_summary: str | None = None,
    lessons: str | None = None,
    market_tech: str | None = None,
) -> str:
    """选赛道 prompt（D5 定案）：LLM 只能在量化候选池内选 3-5 个，可否决池内赛道。

    pool_text：量化定池产出的候选池文本（含 5/20/60 日动量信号）。
    """
    lines = [
        f"你是专业的宏观分析师。基于 {date_str} 的多源数据，"
        "从【量化候选池】中挑选未来短期最值得关注的3-5个赛道，并行使否决权。",
        "",
        "【严格要求】",
        "你只能从【量化候选池】中精确选择行业名称，一字不差。",
        "候选池之外的行业名绝对禁止出现在推荐或回避结果中。",
        "若你认为候选池整体都不合适，可以不推荐任何赛道（recommended_sectors 为空）。",
        "",
        f"【量化候选池】（由趋势+过热规避信号筛出，共{len(pool_text.split(chr(10))) if pool_text else 0}行）：",
        pool_text or "（无候选）",
        "",
        f"量化定池说明: {pool_reasoning or '无'}",
        "",
        "【行业板块排行】（数据来源：东方财富板块行情）",
        f"领涨行业: {top_gainers or '无数据'}",
        f"领跌行业: {top_losers or '无数据'}",
        f"主力资金净流入: {etf_net_flow or '无数据'}",
        "",
        "【财经新闻】（独立来源：东方财富财经要闻）",
        news_summary or "无数据",
        "",
        "【数据同源提示（P1-6）】",
        "板块排行/资金流与量化候选池同源（均为东财板块快照）——它们已用于筛池，",
        "你再读一遍不会获得增量信息；真正的新信息在【财经新闻】与【大盘技术面】。",
        "请把判断重心放在新闻中的产业动态/政策事件，而非复读板块涨跌。",
    ]
    if market_tech:
        lines += [
            "",
            "【大盘技术面】（判定 regime_label 的主要依据）",
            market_tech,
            "",
            "【regime_label】",
            "以量化判定为准（收盘价 vs EMA60，代码单一来源）；",
            "按【大盘技术面】输出 bullish/bearish/neutral，政策/新闻只能辅助参考",
        ]
    if flow_summary:
        lines += ["", "【资金流向】", flow_summary]
    if lessons:
        lines += [
            "",
            "【历史教训：过去类似的赛道选择失败案例，请参考以下模式避免重蹈覆辙】",
            lessons,
            "请在选择赛道时避开出现过以下模式问题的行业类型：",
        ]
    lines += [
        "",
        "【否决权】",
        "候选池由量化信号筛出，但可能包含你不认可的赛道（如新闻/政策明确利空、产业逻辑被证伪）。",
        "对不认可的候选赛道，放入 vetoed_sectors 并给出理由；否决的赛道不会进入推荐。",
        "",
        "输出以下 JSON（纯 JSON，勿用 markdown 代码块）：",
        "{",
        '  "recommended_sectors": ["行业1", "行业2", ...],',
        '  "risk_sectors": ["回避行业1", ...],',
        '  "vetoed_sectors": [{"sector": "否决的池内赛道", "reason": "否决理由"}],',
        '  "regime_label": "bullish/bearish/neutral",',
        '  "reasoning": "为什么选这些赛道，基于新闻中的产业动态/政策风向/资金流向；全部使用中文，描述大盘状态用「牛市/熊市/中性」，禁止出现英文 bullish/bearish/neutral"',
        "}",
    ]
    return "\n".join(lines)



def sector_selection_system_prompt() -> str:
    return "你是量化宏观分析师。只输出纯 JSON 对象，禁止 markdown。"


def news_brief_prompt(news_summary: str) -> str:
    lines = [
        "你是财经新闻摘要员。把下方的今日财经新闻压缩为精炼摘要，供基金监控判读。",
        "",
        "【要求】",
        "1. 保留：涉及的行业/板块名、公司/股票名、政策事件",
        "2. 标注每条新闻对相关行业的利好/利空/中性倾向",
        "3. 控制在 200 字以内，保留最关键信息",
        "4. 新闻较多时按重要程度取舍，宁可少而准",
        "",
        "【今日财经新闻】",
        news_summary or "无数据",
    ]
    return "\n".join(lines)


# P2-8 R4 判定阈值（单一来源，prompt 与日志共用）：把"大幅下降/大面积退出"等模糊表述
# 量化为可执行条件，LLM 只在阈值边界附近做定性补充，规则定边界。
R4_EXPOSURE_DROP_PCT = 15.0      # 核心行业暴露较推荐时下降超过该值（百分点）视为显著流失
R4_WEIGHT_HALVE = 0.5            # 核心行业权重相对推荐时腰斩（×50%）视为根本性变化
R4_HOLDINGS_LOST = 3             # 推荐时前 5 大重仓中 ≥ 该数量退出最新前十大视为论点断裂


def monitor_logic_prompt(
    buy_reason: str,
    sector: str,
    anchor_sector: str,
    anchor_report_date: str,
    anchor_holdings_text: str,
    holdings_text: str,
    rbsa_distribution: str = "",
) -> str:
    """R4 论点证伪 prompt（Q2 定案）：只对比"推荐时论点锚点 vs 最新持仓结构"。

    不做今日宏观/资金流/新闻判断——单日宏观翻转不构成离场依据。
    锚点：推荐时核心行业 + 前 N 大重仓股（含报告期）；最新：前十大重仓 + 最新 RBSA。
    P2-8：判定阈值量化（R4_EXPOSURE_DROP_PCT / R4_WEIGHT_HALVE / R4_HOLDINGS_LOST），
    LLM 只在阈值边界附近做定性补充。
    """
    lines = [
        "你是基金投研审核员。用买入逻辑和最新持仓结构，判定买入论点是否被实质证伪。",
        "注意：本次审核只看结构与基本面证据，不看今日宏观/新闻/资金流。",
        "",
        f"买入逻辑: {buy_reason}",
        f"该基金所属赛道: {sector}",
        "【推荐时论点锚点】",
        f"核心行业: {anchor_sector or '无'}",
        f"推荐时重仓股（报告期 {anchor_report_date or '未知'}）: {anchor_holdings_text or '无'}",
        "",
        "【最新持仓结构】",
        f"当前前十大重仓股: {holdings_text}",
        f"最新行业分布: {rbsa_distribution or '无'}",
        "",
        "输出纯 JSON：",
        "{",
        '  "logic_verdict": "维持/断裂",',
        '  "signal_hint": "HOLD/BUY_MORE/WARNING",',
        '  "sector_risk": true/false,',
        '  "holding_risk": true/false,',
        '  "reason": "说明"',
        "}",
        "",
        "判定规则（只依据结构与基本面，禁止引用今日宏观/新闻/资金流）：",
        f"- 论点证伪（断裂）的量化条件（满足其一）：核心行业暴露较推荐时下降超过 {R4_EXPOSURE_DROP_PCT:.0f} 个百分点"
        f"；或核心行业权重相对推荐时腰斩（下降超过 {R4_WEIGHT_HALVE*100:.0f}%）；"
        f"或推荐时前 5 大重仓中 ≥{R4_HOLDINGS_LOST} 只退出最新前十大；或报告期后持仓结构已根本性变化",
        "- 结构变化但买入逻辑仍成立（行业暴露仍在、核心重仓仍在）→ 维持",
        "- 仅当出现结构性加仓证据（核心重仓显著增持、新重仓与论点一致）→ BUY_MORE",
        "- 无法从结构判断变化方向 → HOLD（维持），不臆测宏观",
        "- 单日宏观/资金流/新闻翻转一律不作为证伪依据",
    ]
    return "\n".join(lines)


def evolution_analysis_prompt(
    successes: list[dict],
    failures: list[dict],
    neutrals: list[dict] | None = None,
    decision_loss: float | None = None,
    loss_streak: int = 0,
) -> str:
    lines = ["你是基金投资策略的系统优化架构师。", ""]
    lines.append("成功案例:")
    for i, s in enumerate(successes, 1):
        lines.append(
            f"  {i}. 赛道:{s['sectors']} | 基金:{s['fund']} | "
            f"大盘:{s['regime']} | 结果:{s['outcome']} 说明:{s['note']}\n"
            f"  推理:{s['reasoning']} | 信号:{s.get('signal', '')} | "
            f"触发:{s.get('signal_triggers', {})} | 逻辑:{s.get('logic', {})}"
        )
    lines.append("")
    lines.append("失败案例:")
    for i, f_ in enumerate(failures, 1):
        lines.append(
            f"  {i}. 赛道:{f_['sectors']} | 基金:{f_['fund']} | "
            f"大盘:{f_['regime']} | 结果:{f_['outcome']} 说明:{f_['note']}\n"
            f"  推理:{f_['reasoning']} | 信号:{f_.get('signal', '')} | "
            f"触发:{f_.get('signal_triggers', {})} | 逻辑:{f_.get('logic', {})}"
        )
    if neutrals:
        lines.append("")
        lines.append("中性案例（微利/微亏，信号不明朗）:")
        for i, n in enumerate(neutrals, 1):
            lines.append(
                f"  {i}. 赛道:{n['sectors']} | 基金:{n['fund']} | "
                f"大盘:{n['regime']} | 结果:{n['outcome']} 说明:{n['note']}\n"
                f"  推理:{n['reasoning']} | 信号:{n.get('signal', '')} | "
                f"触发:{n.get('signal_triggers', {})} | 逻辑:{n.get('logic', {})}"
            )
    # Q8：裁决损耗观测（LLM 选中 vs 候选池均值的 20 日收益差）——让元分析看到定论环节自身的问题
    if decision_loss is not None or loss_streak >= 3:
        lines += ["", "【定论环节裁决损耗观测】"]
        if decision_loss is not None:
            lines.append(f"  当月 LLM 选中基金 vs 候选池均值的 20 日收益差: "
                         f"{decision_loss*100:+.2f}pp（负值 = 定论环节拉低质量）")
        if loss_streak >= 3:
            lines.append(f"  已连续 {loss_streak} 个月为负——请审视定论环节是否存在系统性偏差，"
                         f"并产出针对性的可执行教训")
        lines.append("")
    lines += [
        "",
        "对比成功和失败案例，提取 3-5 条可操作的洞察。每条洞察应：",
        "1. 具体到量化条件或模式特征",
        "2. 可被推荐/监控模块执行",
        "3. 标注洞察类型: sector(赛道选择)/position(基金选择)/timing(时机)/macro(宏观)",
        "4. （P3-11 可选）给出可判定的前置条件 condition——用可量化的判断句描述触发该教训的条件",
        "   （如：基金第一行业∈宏观回避赛道；连续3日主力净流入且站上EMA20），系统后续据此结构化执行",
        '输出 JSON 数组: [{"insight": "...", "type": "sector/position/timing/macro", "condition": "可选的可判定条件"}]',
    ]
    return "\n".join(lines)


def final_pick_prompt(
    candidates: list[dict],
    ctx: Any,
    insights: list[str],
) -> str:
    lines = [
        "【赛道推论】",
        ctx.sector_reasoning or "无",
        f"推荐赛道: {', '.join(ctx.recommended_sectors) or '全市场'}",
        f"回避赛道: {', '.join(ctx.risk_sectors) or '无'}",
    ]
    if insights:
        lines += ["", "【历史教训参考（请参考以下经验避免同类失误）】"]
        lines += [f"  - {r}" for r in insights]
    lines += [
        "",
        "【宏观判定】",
        f"大盘判定: {ctx.regime_label}",
        "",
        "【候选基金（赛道内排序，含重仓股与新闻匹配）】",
    ]
    for i, c in enumerate(candidates, 1):
        sector = c.get("sector") or c.get("rbsa_industry_1", "")
        lines.append(
            f"第{i}名: {c['code']} {c['name']} | 赛道: {sector} | "
            f"卡玛: {c['calmar']:.2f} | "
            f"Hurst: {c['hurst_60d']:.2f} | 组合分: {c['combo']:.3f}"
        )
        ind_parts = []
        for j in range(1, 4):
            ind = c.get(f"rbsa_industry_{j}", "")
            w = c.get(f"rbsa_weight_{j}", 0)
            if ind and w and w > 0:
                ind_parts.append(f"{ind}({w:.1f}%)")
        if ind_parts:
            lines.append(f"  行业分布: {', '.join(ind_parts)}")
        holdings = c.get("holdings", [])
        if holdings:
            hds = [f"{h['stock_name']}({h['industry']},权重{h['weight']:.1f}%)" for h in holdings]
            lines.append(f"  重仓股: {', '.join(hds)}")
        rd = c.get("report_date")
        months = c.get("holdings_months")
        smm = c.get("sector_median_mom")
        gap = c.get("mom_gap")
        if rd and months is not None:
            parts = [f"  持仓时效: {rd} (距今{months}个月)"]
            fund_mom = c.get("momentum_20d", 0) or 0
            parts.append(f"基金20日动量{fund_mom:+.1f}%（量化参考）")
            ret_1m = c.get("ret_1m")
            if ret_1m is not None:
                parts.append(f"近1月涨幅{ret_1m:+.1f}%（22交易日，与前端展示一致，reason 文案引用此值）")
            if smm is not None and gap is not None:
                parts.append(f"赛道同行中位数{smm:+.1f}%")
                parts.append(f"偏离{gap:+.1f}%")
                if abs(gap) > 5:
                    parts.append("(走势异常)")
            lines.append(" | ".join(parts))
    lines += [
        "",
        "【任务指令】",
        "基于以上所有信息（赛道推论、重仓股、进化规则），",
        "从候选中选出最有潜力的一只基金。重点考虑：",
        "1. 基金所在赛道是否被宏观定论认可",
        "2. 赛道内相对强弱和量化指标",
        "3. 是否重复历史教训中提到的失败模式",
        "",
        "【输出两个理由字段，职责分离（P2-7 决策与文案解耦）】",
        '  - "decision_logic"：内部决策依据（给系统审计用），可以用专业术语（RBSA/动量/卡玛等），'
        "要求具体到：选它的核心量化依据、持仓结构依据、与落选者的关键差异、本轮否决/风险点。",
        '  - "reason"：给普通投资者看的大白话（展示用），禁止专业术语，'
        "需包含：这只基金重仓的行业和股票、为什么这些持仓是好的、最近有什么利好或政策支持；",
        "reason 中提及收益涨幅时，必须引用候选名单里的「近1月涨幅」数值（与前端展示一致），",
        "严禁把「20日动量」表述为月涨幅。",
        "不要重复大盘宏观分析结论。",
        "",
        "输出要求（务必严格遵守）：",
        "1. 只输出一个纯 JSON 对象，不要使用 markdown 代码块（禁止 ```json）。",
        "2. 所有指标数值必须严格使用上方候选名单中提供的真实数据。",
        "3. selected_code 必须是候选列表中真实存在的代码字符串：",
        "{",
        '  "selected_code": "选中的基金代码",',
        '  "selected_name": "选中的基金名称",',
        '  "decision_logic": "决策依据（可含专业术语，供审计）",',
        '  "reason": "用大白话说明推荐理由（展示用，禁止专业术语）",',
        '  "vetoed": [',
        '    {"code": "被否决代码", "name": "被否决名称", "reason": "否决理由"}',
        "  ]",
        "}",
    ]
    return "\n".join(lines)


def final_pick_system_prompt() -> str:
    return "你是量化基金推荐决策助手。必须只输出一个纯JSON对象，禁止使用markdown代码块，禁止任何前后说明文字。"

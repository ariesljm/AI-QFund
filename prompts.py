"""LLM prompt 模板模块：所有 prompt 集中管理，业务逻辑不内嵌 prompt 字符串。"""

from typing import Any


def sector_selection_prompt(
    date_str: str,
    available: list[str],
    top_gainers: str,
    top_losers: str,
    etf_net_flow: str,
    news_summary: str,
    flow_summary: str | None = None,
    lessons: str | None = None,
) -> str:
    lines = [
        f"你是专业的宏观分析师。基于 {date_str} 的多源数据，"
        "从【可选赛道清单】中挑选未来短期最值得关注的3-5个赛道，以及需要回避的赛道。",
        "",
        "【严格要求】",
        "你只能从【可选赛道清单】中精确选择行业名称，一字不差。",
        "清单之外的行业名绝对禁止出现在推荐或回避结果中。",
        "若某个行业不在清单中，说明没有对应的可投基金，选了也白选。",
        "",
        "【重要：板块排行名和可选赛道清单名可能不同】",
        "下方板块排行里的行业名（如'饮料乳品''游戏Ⅱ'）和可选赛道清单名（如'食品''游戏'）",
        "是同行业的两种叫法。请按含义自行对应翻译，最终输出必须使用清单中的名称。",
        "例如：排行中'饮料乳品'涨6.9% → 对应清单中'食品'赛道（二者高度重合）。",
        "",
        f"【可选赛道清单】（共{len(available)}个，这就是全部可投的行业名）：",
        "、".join(sorted(available)),
        "",
        "【行业板块排行】",
        f"领涨行业: {top_gainers or '无数据'}",
        f"领跌行业: {top_losers or '无数据'}",
        f"主力资金净流入: {etf_net_flow or '无数据'}",
        "",
        "【财经新闻】",
        news_summary or "无数据",
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
        "输出以下 JSON（纯 JSON，勿用 markdown 代码块）：",
        "{",
        '  "recommended_sectors": ["行业1", "行业2", ...],',
        '  "risk_sectors": ["回避行业1", ...],',
        '  "regime_label": "bullish/bearish/neutral",',
        '  "reasoning": "为什么选这些赛道，基于新闻中的产业动态/政策风向/资金流向"',
        "}",
    ]
    return "\n".join(lines)


def sector_selection_system_prompt() -> str:
    return "你是量化宏观分析师。只输出纯 JSON 对象，禁止 markdown。"


def monitor_logic_prompt(
    buy_reason: str,
    sector: str,
    recommended_sectors: list[str],
    risk_sectors: list[str],
    regime_label: str,
    sector_reasoning: str,
    holdings_text: str,
    matched_text: str,
    news_summary: str,
) -> str:
    lines = [
        "你是基金投研审核员。根据买入逻辑、该基金的赛道归属和今日宏观数据，"
        "判定买入逻辑是否维持，并给出信号建议。",
        "",
        f"买入逻辑: {buy_reason}",
        f"该基金所属赛道: {sector}",
        "",
        "【今日宏观判定】",
        f"推荐赛道: {', '.join(recommended_sectors) or '无'}",
        f"回避赛道: {', '.join(risk_sectors) or '无'}",
        f"大盘判定: {regime_label}",
        f"赛道推论: {sector_reasoning or '无'}",
        "",
        f"【该基金当前重仓股】",
        holdings_text,
        "",
        f"【该基金持仓股在今日新闻中的提及】",
        matched_text,
        "",
        "【今日财经新闻全文】",
        news_summary,
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
        "判定规则：",
        "- 若该基金所属赛道出现在回避赛道中，或新闻对该赛道有明确利空 → 赛道风险",
        "- 若持仓股在今日新闻中有明确利空 → 持仓风险",
        "- 任一风险推断买入逻辑断裂 → 断裂",
        "- 若该基金赛道仍在推荐赛道中、持仓股有正面新闻 → BUY_MORE",
        "- 赛道方向中性但持仓无异常 → HOLD",
    ]
    return "\n".join(lines)


def evolution_analysis_prompt(
    successes: list[dict],
    failures: list[dict],
    neutrals: list[dict] | None = None,
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
    lines += [
        "",
        "对比成功和失败案例，提取 3-5 条可操作的洞察。每条洞察应：",
        "1. 具体到量化条件或模式特征",
        "2. 可被推荐/监控模块执行",
        "3. 标注洞察类型: sector(赛道选择)/position(基金选择)/timing(时机)/macro(宏观)",
        '输出 JSON 数组: [{"insight": "...", "type": "sector/position/timing/macro"}]',
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
        matched = c.get("matched_news", [])
        if matched:
            for m in matched:
                lines.append(
                    f"  ⚡ 新闻匹配: 持仓股 {m['stock_name']} 出现在今日新闻 "
                    f"[等级={m['level']}] \"{m['title'][:50]}\""
                )
        rd = c.get("report_date")
        months = c.get("holdings_months")
        smm = c.get("sector_median_mom")
        gap = c.get("mom_gap")
        if rd and months is not None:
            parts = [f"  持仓时效: {rd} (距今{months}个月)"]
            fund_mom = c.get("momentum_20d", 0) or 0
            parts.append(f"基金20日涨幅{fund_mom:+.1f}%")
            if smm is not None and gap is not None:
                parts.append(f"赛道同行中位数{smm:+.1f}%")
                parts.append(f"偏离{gap:+.1f}%")
                if abs(gap) > 5:
                    parts.append("(走势异常)")
            lines.append(" | ".join(parts))
    lines += [
        "",
        "【任务指令】",
        "基于以上所有信息（赛道推论、重仓股、新闻匹配、进化规则），",
        "从候选中选出最有潜力的一只基金。重点考虑：",
        "1. 基金重仓股与今日新闻的匹配程度（利好>利空）",
        "2. 基金所在赛道是否被宏观定论认可",
        "3. 赛道内相对强弱和量化指标",
        "4. 是否重复历史教训中提到的失败模式",
        "",
        "【严格要求】选定理由必须：",
        "1. 用普通投资者能看懂的中文，禁止使用任何英文术语、缩写、专业词汇",
        "2. 禁止出现：RBSA、Hurst、Calmar、回撤、波动率、动量、Beta、Alpha等术语",
        "3. 禁止出现类似 'RBSA行业::64.93' 这种格式",
        "4. 必须包含：今天宏观分析的结论（大盘走势、资金流向、板块轮动）",
        "5. 必须包含：这只基金重仓的行业和股票，以及为什么这些持仓是好的",
        "6. 必须包含：最近有什么利好消息或政策支持这个行业",
        "",
        "输出要求（务必严格遵守）：",
        "1. 只输出一个纯 JSON 对象，不要使用 markdown 代码块（禁止 ```json）。",
        "2. 所有指标数值必须严格使用上方候选名单中提供的真实数据。",
        "3. selected_code 必须是候选列表中真实存在的代码字符串：",
        "{",
        '  "selected_code": "选中的基金代码",',
        '  "selected_name": "选中的基金名称",',
        '  "reason": "用大白话说明推荐理由，包含今天分析结论，禁止专业术语",',
        '  "vetoed": [',
        '    {"code": "被否决代码", "name": "被否决名称", "reason": "否决理由"}',
        "  ]",
        "}",
    ]
    return "\n".join(lines)


def final_pick_system_prompt() -> str:
    return "你是量化基金推荐决策助手。必须只输出一个纯JSON对象，禁止使用markdown代码块，禁止任何前后说明文字。"
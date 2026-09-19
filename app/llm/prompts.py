"""LLM prompt 模板模块：所有 prompt 集中管理，业务逻辑不内嵌 prompt 字符串。"""

from typing import Any


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
            f"第{i}名: {c.get('code', '?')} {c.get('name', '?')} | 赛道: {sector} | "
            f"卡玛: {float(c.get('calmar', 0) or 0):.2f} | "
            f"Hurst: {float(c.get('hurst_60d', 0) or 0):.2f} | "
            f"组合分: {float(c.get('combo', 0) or 0):.3f}"
        )
        # 抗跌性白话展示（每个裸数字必须带口径与好坏方向，参考 fund-guy-skill 方法论）
        cu = c.get("capture_up")
        cd = c.get("capture_down")
        if cu is not None and cd is not None:
            lines.append(
                f"  抗跌性: 大盘涨1%时它涨{float(cu):.2f}%（上行捕获，越高越好） | "
                f"大盘跌1%时它跌{float(cd):.2f}%（下行捕获，越低越好，<1=比大盘抗跌）"
            )
        dvol = c.get("downside_vol")
        dd60 = c.get("drawdown_60d")
        risk_parts = []
        if dvol is not None:
            risk_parts.append(f"年化下行波动{float(dvol)*100:.1f}%")
        if dd60 is not None:
            risk_parts.append(f"近60日最大回撤{abs(float(dd60)):.1f}%（越小越好）")
        if risk_parts:
            lines.append(f"  风险面: {' | '.join(risk_parts)}")
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
            parts.append(f"基金20日动量{fund_mom:+.1f}%（量化参考，不作为绝对收益依据）")
            ps = c.get("purchase_status")
            if ps and ps != "normal":
                parts.append(f"⚠️ 申购状态: {ps}（限购/暂停——除非强理由否则不选）")
            if smm is not None and gap is not None:
                parts.append(f"赛道同行中位数{smm:+.1f}%")
                parts.append(f"偏离{gap:+.1f}%（基金相对赛道同行的滚动超额，α 质量的直接证据）")
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
        "4. 辨析收益来源（β/α）：候选的涨幅可能只是赛道整体上涨（行业β白送的），",
        "   也可能是它自己跑赢同行（真本事α）。decision_logic 必须明确区分两者：",
        "   引用「偏离」（基金20日动量−赛道同行中位数）判断——接近0说明它只是吃了行业行情，",
        "   明显为正才是相对同行的超额；再结合抗跌性/风险面判断上涨质量。禁止把行业β当成基金能力。",
        "5. 尊重量化排序：combo 排序前 30% 的候选优先；除非有强理由（如重仓股结构异常、",
        "   赛道归属可疑、偏离超出阈值），否则不得选择排序靠后的候选。",
        "",
        "【输出两个理由字段，职责分离（P2-7 决策与文案解耦）】",
        '  - "decision_logic"：内部决策依据（给系统审计用），可以用专业术语（RBSA/动量/卡玛等），'
        "要求具体到：选它的核心量化依据、持仓结构依据、与落选者的关键差异、本轮否决/风险点。",
        '  - "reason"：给普通投资者看的大白话（展示用），禁止专业术语，'
        "需包含：这只基金重仓的行业和股票、为什么这些持仓是好的、最近有什么利好或政策支持；",
        "reason 中提及收益时引用「偏离」（相对赛道同行超额的幅度）即可，",
        "严禁把「20日动量」表述为月涨幅、也不得引用已移出的「近1月涨幅」字段。",
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


def audit_system_prompt() -> str:
    """票 13 System prompt：资深风控合规官视角。

    职责是行使一票否决权（VETO）而非推销；发现严重隐患必须 VETO，
    模棱两可给 CONDITIONAL_PASS 并写明条件；绝不因为候选优秀就放松。
    """
    return (
        "你是资深公募基金风控合规官，对候选基金行使一票否决权。"
        "你的职责是排雷，不是推销：任何可能让持有人亏大钱的隐患都必须 VETO。"
        "四个审查维度：风格漂移（偏离自身定位追逐高危题材）、标的暴雷（重仓股"
        "立案/业绩预亏/质押风险/减持潮）、经理负荷（管理规模与基金数过多、精力"
        "分散）、流动性冲击（大额赎回/踩踏迹象）。"
        "判定原则：证据确凿的致命隐患 → VETO（必须给出具体理由）；有隐忧但非致命"
        " → CONDITIONAL_PASS（写明必须满足的条件）；干净 → PASS。"
        "你极度挑剔，宁可有条件通过被质疑，也不放过一个隐患。"
        "只输出一个纯 JSON 对象，禁止 markdown 代码块与任何前后说明文字。"
    )


def audit_user_prompt(fund: dict, slices: dict) -> str:
    """票 13 User prompt：候选基金画像 + 四组切片 + 审查维度 + JSON 输出约束。

    fund: {"code", "name", "quant_score", "industry"...}（量化分/基本面画像）。
    slices: 四组切片的素材文本，键为切片名（如 holdings_change / risk_radar /
            management / sentiment），由 llm/context.py 单一归属装配（ADR-0003）；
            缺失切片（数据源未就绪）传 None/空 → 明示"缺失"而非静默填空。
    """
    lines = [
        f"【候选基金】{fund.get('code', '?')} {fund.get('name', '?')}",
        f"量化分: {fund.get('quant_score', '?')} | 所属行业: {fund.get('industry', '?')}",
        "",
        "【四组切片】（缺失项明确标注，不得脑补）",
    ]
    for key, label in (("holdings_change", "持仓异动"), ("risk_radar", "重仓股风险雷达"),
                       ("management", "管理团队变动"), ("sentiment", "舆情争议")):
        txt = slices.get(key)
        lines.append(f"▸ {label}: {txt if txt else '【缺失——不得猜测，据此无法评估该维度】'}")
    # 活跃案例 few-shot 回流（票 17）：知识库案例表空时注入空串，等价无回流，不破坏现有审计
    try:
        from app.repo.knowledge import get_active_cases
        from app.engine.knowledge import retrieve_cases, assemble_few_shot
        _cases = retrieve_cases(get_active_cases(), case_type="bad")
        _few_shot = assemble_few_shot(_cases)
        if _few_shot:
            lines += ["", "【历史排雷案例（Bad-Case 回流，参考而非套用）】", _few_shot]
    except Exception:
        pass  # 知识库未就绪不阻断审计
    lines += [
        "",
        "【审查任务】按四个维度逐一评估并输出 JSON：",
        "1. audit_verdict: PASS / CONDITIONAL_PASS / VETO（枚举精确，禁止其他值）",
        "2. risk_score: 0–100 整数（越高越危险）",
        "3. veto_reasons: VETO 时必填非空字符串数组（具体到事件/个股）",
        "4. audit_details: 四维度的风险点对象（style_drift / stock_risk / manager_load / liquidity）",
        "5. recommendation_summary: 不超过 50 字",
        "",
        "输出示例：",
        "{",
        '  "audit_verdict": "PASS",',
        '  "risk_score": 25,',
        '  "veto_reasons": [],',
        '  "audit_details": {"style_drift": "无", "stock_risk": "无",',
        '                   "manager_load": "经理管理 8 只基金", "liquidity": "无"},',
        '  "recommendation_summary": "持仓稳健，估值合理，无重大隐患"',
        "}",
    ]
    return "\n".join(lines)

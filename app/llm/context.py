"""LLM 上下文构建：跨引擎共用的 prompt 素材单一来源（候选4 收敛）。

领域概念「素材装配」：把结构化数据（持仓/锚点/RBSA 分布/候选池/技术面）
转成 LLM 可读的中文段落。所有素材格式化集中于此——recommend/monitor/evolve
对同一数据源的素材口径必然一致（数据字段改名/权重取位/报告期文案只在
一处维护）。素材函数尽量接收基础数据（dict/list），不依赖引擎领域对象，
保持 llm 层无 engine 依赖。
"""

from app import domain, repo


def build_holdings_text(code: str, limit: int = 5) -> str:
    """持仓 → 单行文本（LLM 判断用；recommend/monitor 共用同一格式）。"""
    rows = repo.get_holdings(code, limit)
    if not rows:
        return "无持仓数据"
    return "；".join(
        f"{h['stock_name']}({h['industry'] or '其他'},{h['weight']:.1f}%)" for h in rows
    )


def rbsa_distribution(feat: dict | None) -> str:
    """基金 RBSA 行业暴露分布（如 '半导体(4.6%), 通信设备(4.1%), 电源设备(4.1%)'）。"""
    if not feat:
        return ""
    parts = []
    for i in range(1, 4):
        ind = feat.get(f"rbsa_industry_{i}")
        w = feat.get(f"rbsa_weight_{i}")
        if ind and w:
            parts.append(f"{ind}({w:.1f}%)")
    return ", ".join(parts)


def anchor_holdings_text(snapshot: dict, code: str | None = None) -> tuple[str, str, str]:
    """从推荐时 feature_snapshot 提取论点锚点：(核心行业, 重仓股文本, 报告期)。

    对称切片（R4）：报告期不同时锚点持仓取锚点报告期前 10 大（与最新前 10 大对称），
    避免"锚点前5 vs 最新前10"切片不对称、第 6-10 名被误判为新增偏离；
    历史报告期数据缺失时回退快照内 top_holdings（前 5）。
    兼容新落库的 top_holdings（[{stock_code, stock_name, weight}]）与旧快照（无该字段）。
    """
    if not snapshot:
        return "", "", ""
    core_sector = snapshot.get("rbsa_industry_1") or snapshot.get("sector") or ""
    report_date = snapshot.get("holdings_report_date") or ""
    if code and report_date:
        rows = repo.get_holdings_at_report(code, report_date, 10)
        if rows:
            parts = []
            for h in rows:
                name = h.get("stock_name") or h.get("stock_code") or ""
                w = h.get("weight")
                parts.append(f"{name}({w:.1f}%)" if w is not None else name)
            return core_sector, ", ".join(parts), report_date
    holdings = snapshot.get("top_holdings") or []
    if not holdings:
        return core_sector, "", report_date
    parts = []
    for h in holdings[:5]:
        name = h.get("stock_name") or h.get("stock_code") or ""
        w = h.get("weight")
        if name:
            parts.append(f"{name}({w:.1f}%)" if w is not None else name)
    return core_sector, ", ".join(parts), report_date


def market_technical_text(tech: dict) -> str:
    """沪深300 技术面快照 → prompt 中文段落（repo 只返回数据，文案归素材装配）。"""
    pos = "上方" if tech["close"] > tech["ema60"] else "下方"
    trend = " / ".join(f"{c:,.0f}" for c in tech["closes"])
    return (f"最新交易日 {tech['date']} 沪深300：收盘 {tech['close']:,.2f} 点"
            f"（较上交易日 {tech['chg_pct']:+.2f}%），EMA60={tech['ema60']:,.2f} 点，"
            f"收盘价位于 EMA60 {pos}；近6个交易日收盘点 {trend}")


def pool_text(rows: list[tuple]) -> str:
    """候选池 → 每赛道一行的 prompt 文本（含量化信号、趋势标签与资金流趋势）。

    rows: (sector, mom_5d, mom_20d, mom_60d, fund_count, flags[, trend_label, flow_5d])；
    兼容旧 6 元组（缺趋势标签/资金流时降级为原格式）。
    调用方（sector_pool 装配）负责把领域对象摊平成基础数据，避免 llm 层依赖 engine。
    """
    lines = []
    for row in rows:
        sector, m5, m20, m60, n, flags = row[0], row[1], row[2], row[3], row[4], row[5]
        trend_label = row[6] if len(row) > 6 else ""
        flow_5d = row[7] if len(row) > 7 else None
        parts = [f"{sector}(5日{m5:+.1f}%, 20日{m20:+.1f}%, 60日{m60:+.1f}%, 基金{n}只"]
        if flow_5d is not None:
            parts.append(f", 近5日资金{flow_5d:+.0f}万")
        if flags:
            parts.append("，" + ",".join(flags))
        if trend_label:
            parts.append(f"，{trend_label}")
        parts.append(")")
        lines.append("".join(parts))
    return "\n".join(lines)


def news_theme_summary(days: int = 7) -> str:
    """近 N 日财经要闻回顾（趋势视角素材）：每行 (日期) 首条标题（截断 80 字）。

    repo.get_recent_macro_news 为数据源（素材装配单一来源）；空历史返回空串。
    """
    rows = repo.get_recent_macro_news(days)
    lines = []
    for d, summary in rows:
        first = (summary.split("\n")[0] or "").strip()
        if not first:
            continue
        if len(first) > 80:
            first = first[:80] + "…"
        lines.append(f"({d}) {first}")
    return "\n".join(lines)


# ── 新闻主题聚合（#3 方向 A：政策持续性 vs 媒体情绪过热，不做热度排序） ──

# 政策驱动词（正面：产业逻辑强化，对应"刚启动"的潜在线索）
_POLICY_KEYWORDS = (
    "政策", "规划", "补贴", "扶持", "出台", "条例", "方案", "纲要",
    "部署", "试点", "批复", "加码", "专项行动", "指导意见", "落地",
)

# 媒体情绪词（风险：关注度过热，对应"追高"；仅作否决参考，不作选择依据）
_SENTIMENT_KEYWORDS = (
    "涨停", "大涨", "飙升", "暴涨", "热炒", "火爆", "疯涨", "抢筹",
    "新高", "连板",
)


def build_sector_vocab() -> dict[str, str]:
    """赛道词典：{别名或行业名 → 规范 RBSA 行业名}（新闻主题匹配单一来源）。

    包含 domain.SECTOR_ALIASES（LLM 自由名 → RBSA 名）与 RBSA 可用行业名
    （自映射），让新闻文本里的"光伏""芯片"与"电源设备""半导体"命中同一规范名。
    """
    vocab = dict(domain.SECTOR_ALIASES)
    for s in repo.get_available_sectors():
        vocab.setdefault(s, s)
    return vocab


def extract_sector_mentions(text: str, vocab: dict[str, str]) -> set[str]:
    """扫描文本命中赛道（纯词典子串匹配，无 LLM/无 NLP 库），返回规范赛道名集合。"""
    hits: set[str] = set()
    for alias, canonical in vocab.items():
        if alias and alias in text:
            hits.add(canonical)
    return hits


def sector_theme_summary(days: int = 7, min_days: int = 2) -> str:
    """近 N 日新闻主题聚合（方向 A）：政策持续性 vs 媒体情绪过热，不做热度排序。

    逐条快讯扫描赛道命中，按政策词/情绪词分类：
    - 政策词命中且持续 ≥min_days 天 → 政策持续主题（潜在启动线索，正面）；
    - 情绪词命中且持续 ≥min_days 天 → 媒体过热提示（追高风险，仅作否决参考）。
    政策词优先于情绪词（政策是根因、涨是结果）；两者都不命中为中性提及，忽略。
    无持续主题返回空串（调用方降级 news_theme_summary）。
    """
    vocab = build_sector_vocab()
    rows = repo.get_recent_macro_news(days)
    themes: dict[str, dict] = {}
    for date, text in rows:
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            for canonical in extract_sector_mentions(line, vocab):
                t = themes.setdefault(canonical, {
                    "policy_days": set(), "sentiment_days": set(),
                    "policy_count": 0, "sentiment_count": 0,
                })
                if any(kw in line for kw in _POLICY_KEYWORDS):
                    t["policy_days"].add(date)
                    t["policy_count"] += 1
                elif any(kw in line for kw in _SENTIMENT_KEYWORDS):
                    t["sentiment_days"].add(date)
                    t["sentiment_count"] += 1

    policy_lines, risk_lines = [], []
    for canonical, t in sorted(themes.items()):
        if len(t["policy_days"]) >= min_days:
            policy_lines.append(
                f"- {canonical}：近{days}日 {len(t['policy_days'])} 天有政策/产业事件"
                f"（累计 {t['policy_count']} 条，潜在启动线索）")
        if len(t["sentiment_days"]) >= min_days:
            risk_lines.append(
                f"- {canonical}：近{days}日 {len(t['sentiment_days'])} 天媒体情绪高关注"
                f"（累计 {t['sentiment_count']} 条，警惕追高）")

    parts = []
    if policy_lines:
        parts.append("【政策持续主题】（政策/产业事件持续出台 = 潜在启动线索）\n"
                     + "\n".join(policy_lines))
    if risk_lines:
        parts.append("【媒体过热提示】（关注度持续升温 = 追高风险，仅作否决参考，不作选择依据）\n"
                     + "\n".join(risk_lines))
    return "\n\n".join(parts)

"""LLM 上下文构建：跨引擎共用的 prompt 素材单一来源（候选4 收敛）。

领域概念「素材装配」：把结构化数据（持仓/锚点/RBSA 分布/候选池/技术面）
转成 LLM 可读的中文段落。所有素材格式化集中于此——recommend/monitor/evolve
对同一数据源的素材口径必然一致（数据字段改名/权重取位/报告期文案只在
一处维护）。素材函数尽量接收基础数据（dict/list），不依赖引擎领域对象，
保持 llm 层无 engine 依赖。
"""


def holdings_change_snapshot(current: list[dict], previous: list[dict]) -> str:
    """持仓异动切片（票 12 切片一）：最近两期 Top-N 对比，纯函数。

    current/previous 与 repo.get_holdings 同构：
    list[dict]（stock_code / stock_name / weight / industry）。
    输出：留存率、新进重仓（含行业）、集中度变化（pp）。
    缺失必须**显式可见**（不静默填空）：任一侧缺失 → 指明缺失侧。
    """
    if not previous:
        return "持仓异动：上期持仓缺失（无对比基准）"
    if not current:
        return "持仓异动：本期持仓缺失，无法对比"
    prev_codes = {h["stock_code"] for h in previous}
    cur_codes = {h["stock_code"] for h in current}
    retained = len(prev_codes & cur_codes)
    retention = retained / len(prev_codes)
    new_entries = [h for h in current if h["stock_code"] not in prev_codes]
    new_txt = (", ".join(f"{h['stock_name']}({h['industry'] or '其他'})" for h in new_entries)
               or "无")
    conc_cur = sum(h["weight"] for h in current)
    conc_prev = sum(h["weight"] for h in previous)
    parts = [f"持仓异动：两期重仓留存率 {retention * 100:.0f}%（前 {len(prev_codes)} 进 {retained}）",
             f"新进重仓：{new_txt}",
             f"集中度 {conc_prev:.1f}% → {conc_cur:.1f}%（{conc_cur - conc_prev:+.1f}pp）"]
    return "；".join(parts)


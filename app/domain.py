"""领域常量与纯函数单一来源：跨引擎/回测/Web 共用的口径与状态机。

避免同一领域事实（前向窗口、信号枚举、大盘状态判定、排序权重 schema）
在多处各自硬编码导致漂移。
"""

from dataclasses import dataclass
from typing import Any

# ── 推荐周期 ─────────────────────────────────────────────
# 一次推荐对应的预测/结算/质量度量共用的前向窗口（交易日）
FORWARD_DAYS = 20

# ── 收益判定阈值（单一来源） ──────────────────────────────
# 赚钱口径：绝对收益 > 1% 视为扣费后真赚钱（quality 度量 / GA fitness / 回测 / 结算共用）
PROFIT_THRESHOLD = 0.01

# ── 模型特征列（fund_features 表列名，单一来源） ──────────
# repo 拼 SQL / 特征计算 / 推荐排序 / 回测均从此导入，避免列名清单多处漂移
FEATURE_COLS = [
    "hurst_60d", "momentum_20d", "calmar", "downside_vol",
    "capture_up", "capture_down", "bias_60d",
    "drawdown_60d", "reversal_20d",
    "mom_5d", "mom_60d", "vol_20d",
]

# ── 市场状态列（R1 绝对收益目标配套） ──────────────────────
# 全基金共享的时变市场特征（指数 20 日动量/波动率），不进 fund_features 表；
# 训练/打分/回测时从指数数据现算注入，让模型感知 beta 分量才能预测绝对收益。
MARKET_COLS = ["idx_mom_20d", "idx_vol_20d"]

# ── LLM 行业名 → RBSA 行业名 映射（推荐/监控共用单一来源） ──
# LLM 选赛道输出自由行业名（如"光伏"），RBSA 行业名为映射后名（如"电源设备"）；
# 推荐引擎（赛道解析）与监控引擎（赛道锚点判定）必须用同一套映射，
# 否则推荐/监控赛道命名空间不一致会误报（历史遗留：映射曾是推荐引擎私有符号被监控跨模块 import）。
SECTOR_ALIASES: dict[str, str] = {
    "风电设备": "电源设备",
    "光伏": "电源设备",
    "光伏设备": "电源设备",
    "军工": "航空航天装备",
    "军工装备": "航空航天装备",
    "军工电子": "航空航天装备",
    "白酒": "饮料",
    "证券": "非银行金融",
    "券商": "非银行金融",
    "油气": "石油天然气",
    "工业金属": "基本金属",
    "电力设备": "输变电设备",
    "芯片": "半导体",
    "芯片设计": "半导体",
    "存储芯片": "半导体",
    "半导体设备": "半导体",
    "半导体材料": "半导体",
}


def resolve_sector_name(ideal: str, candidates: list[str]) -> str | None:
    """把 LLM 选的行业名匹配到 RBSA 行业名（精确匹配 + 别名 + 子串，不模糊匹配）。

    推荐引擎（赛道解析）与监控引擎（赛道锚点判定）共用，避免命名空间不一致误报。
    """
    normalized = SECTOR_ALIASES.get(ideal, ideal)
    ideal_lower = normalized.lower()
    # 1. 精确匹配
    for c in candidates:
        if c and c.lower() == ideal_lower:
            return c
    # 2. candidate 包含 ideal（如 ideal="食品" candidate="食品饮料"）
    for c in candidates:
        if c and ideal_lower in c.lower():
            return c
    # 3. ideal 包含 candidate（如 ideal="石油天然气" candidate="天然气"）
    for c in candidates:
        if c and c.lower() in ideal_lower:
            return c
    return None


# C4 赛道纯度门槛：第一行业暴露 <10% 的基金不视为赛道基金。
# 实测（2026-08）：2849 只"半导体"基金中 986 只（35%）暴露不足 10%——
# 多为全市场分散混合型基金（如 000011 第一重仓是宁德时代），
# 混入赛道会稀释赛道内排序；无纯度门槛则赛道聚焦名存实亡。
MIN_SECTOR_EXPOSURE = 10.0


class SectorPolicy:
    """赛道判定策略（解析 + 锚定口径），推荐/监控/宏观共用单一来源。

    架构深化候选 1：赛道名解析曾散落 recommend._resolve_sectors、
    monitor.check_sector_anchor、macro_agent._suggest_quant 三处各自实现，
    同一领域事实"基金以第一行业锚定赛道、命中回避即否决"各写一遍。
    收敛为本对象后，各引擎只消费判定结果，改解析/门槛口径只动此处。
    （macro_agent._suggest_llm_free 为 A/B 回滚策略：sector_strategy 配置
    切换，保留原样，仅 quant_pool 正式路径消费本对象）
    用法：一次构造（available 来自 repo.get_available_sectors），多次 resolve。
    """

    def __init__(self, available: list[str]):
        """available 为 RBSA 表中真实存在的行业名清单。"""
        self._available = list(available)

    def resolve(self, names: list[str]) -> list[str]:
        """LLM 自由行业名 → RBSA 行业名（别名+匹配），去重保序。"""
        resolved: list[str] = []
        for s in names:
            m = resolve_sector_name(s, self._available)
            if m and m not in resolved:
                resolved.append(m)
        return resolved

    def resolve_set(self, names: list[str]) -> set[str]:
        """resolve 的集合形态（锚定/回避成员判断用）。"""
        return set(self.resolve(names))


# ── 信号枚举（监控引擎/推荐引擎/质量度量共用） ──────────
SIGNAL_HOLD = "HOLD"
SIGNAL_BUY_MORE = "BUY_MORE"
SIGNAL_WARNING = "WARNING"
SIGNAL_EXIT = "EXIT"
SIGNAL_REJECT = "REJECT"

# 信号合并优先级（监控防线链聚合单一来源）：数值越大优先级越高。
# EXIT > WARNING > BUY_MORE > HOLD：HOLD 视为无信号基底（优先级最低），
# 保证警惕/离场恒优先于加仓（风控优先），同时加仓建议不被无信号压制。
SIGNAL_PRIORITY = {
    SIGNAL_HOLD: 1,
    SIGNAL_BUY_MORE: 2,
    SIGNAL_WARNING: 3,
    SIGNAL_EXIT: 4,
}
# 持仓状态集合（监控引擎与 repo 查询共用）
HOLDING_STATES = (SIGNAL_HOLD, SIGNAL_BUY_MORE, SIGNAL_WARNING)

# ── 模型预测 alpha 门槛（推荐/监控共用语义） ─────────
# 「模型看好」= 模型预测未来 FORWARD_DAYS 日超额收益为正。
# 推荐引擎以此硬条件过滤候选；监控引擎据此判断信号是否转负。
MIN_PREDICTED_ALPHA = 0.0

# ── 信号/大盘状态中文文案（Web 展示单一来源） ────────────
# 模板与前端 JS 均从此映射取文案，避免四处硬编码漂移（历史遗留 PASS/ADD/CAUTION 兼容映射）
SIGNAL_LABELS = {
    SIGNAL_HOLD: "持有",
    SIGNAL_BUY_MORE: "加仓",
    SIGNAL_WARNING: "警惕",
    SIGNAL_EXIT: "离场",
    SIGNAL_REJECT: "否决",
    "PASS": "持有",
    "ADD": "加仓",
    "CAUTION": "警惕",
}

# ── 大盘状态机 ──────────────────────────────────────────
REGIME_BULL = "BULL"
REGIME_BEAR = "BEAR"
REGIME_NEUTRAL = "NEUTRAL"


def regime_from_close_ma60(close, ma60) -> str:
    """沪深300 close vs MA60 → BULL/BEAR/NEUTRAL（回测/生产共用，避免两套判定漂移）。"""
    if close is None or ma60 is None or ma60 <= 0:
        return REGIME_NEUTRAL
    return REGIME_BULL if close > ma60 else REGIME_BEAR


# 大盘状态中文文案（Web 模板/前端展示单一来源）
REGIME_LABELS = {
    REGIME_BULL: "牛市",
    REGIME_BEAR: "熊市",
    REGIME_NEUTRAL: "中性",
}


def normalize_regime_label(label: str | None) -> str:
    """LLM 输出的大盘 regime label 归一化：bull*→BULL、bear*→BEAR，其余 NEUTRAL。"""
    raw = (label or "").strip().lower()
    if raw.startswith("bull"):
        return REGIME_BULL
    if raw.startswith("bear"):
        return REGIME_BEAR
    return REGIME_NEUTRAL


_REGIME_EN_TO_ZH = (("bullish", "牛市"), ("bearish", "熊市"), ("neutral", "中性"),
                    ("bull", "牛市"), ("bear", "熊市"))


def zh_regime(text: str | None) -> str | None:
    """LLM 赛道推论自由文本里的英文大盘状态词替换为中文（先长后短）。

    与 normalize_regime_label（结构化 label）互补：本函数处理嵌入文本的英文词。
    用 ASCII 字母边界 (?<![A-Za-z])(?![A-Za-z]) 而非 \\b：Python re 的 \\b 是
    Unicode 感知的（汉字属 \\w），'为bearish' 中 bearish 前不是单词边界。
    """
    import re as _re
    if not text:
        return text
    for en, zh in _REGIME_EN_TO_ZH:
        text = _re.sub(rf"(?<![A-Za-z]){en}(?![A-Za-z])", zh, text, flags=_re.IGNORECASE)
    return text


def display_score(combo: float | None, raw_score: float | None) -> int:
    """推荐分 → 0-100 展示分：有 combo 时 10 倍偏移，否则 500 倍偏移。"""
    if combo is not None:
        return min(max(int(combo * 10 + 50), 0), 100)
    return min(max(int((raw_score or 0) * 500 + 50), 0), 100) if raw_score else 0


# ── 排序权重默认 schema（repo 读取与进化自纠偏共用） ────
# 阶段2：模型主导排序（Q7 共识：弱化大路货动量，模型预测是核心 alpha 信号）
@dataclass(frozen=True)
class RankingConfig:
    """排序权重配置（不可变对象，meta 读 + 默认合并的单一入口）。

    架构深化候选 4：排序配置参数链收敛——recommend/evolve/ga/回测全部经
    repo.get_ranking_cfg() 获得本对象；dict 消费点（score_frame/回测 spread）
    通过 __getitem__/to_dict 兼容。
    承载风控参数保护语义：momentum_guard_pct 是风控防线参数，进化自纠偏与
    GA 寻优均不覆盖（写入侧强制沿用当前值）。
    动量护栏与候选质量门槛以显式方法/常量表达，各调用点消费同一判定。
    """

    model_weight: float = 0.7
    rel_strength_weight: float = 0.1
    calmar_weight: float = 0.08
    hurst_weight: float = 0.08
    momentum_guard_pct: float = -15.0

    # 候选质量门槛：赛道内候选相对全局最优组合分的比例阈值（LLM 定论兑底用）
    QUALITY_RATIO = 0.6

    def passes_momentum_guard(self, momentum_20d: float) -> bool:
        """动量护栏（标量判定）：动量不低于门槛才允许入选（recommend 纵深拦截用）。

        DataFrame 级过滤见 calculator.apply_momentum_guard（推荐/回测共用）。
        """
        return momentum_20d >= self.momentum_guard_pct

    def get(self, key: str, default: Any = None) -> Any:
        """dict 风格取值（GA/进化沿用）。"""
        return getattr(self, key, default)

    def __getitem__(self, key: str) -> Any:
        """dict 风格下标访问（calculator.score_frame 等消费方兼容）。"""
        return getattr(self, key)

    def to_dict(self) -> dict:
        """导出为 dict（回测 spread 覆盖、日志等需要 dict 的场景）。"""
        return {
            "model_weight": self.model_weight,
            "rel_strength_weight": self.rel_strength_weight,
            "calmar_weight": self.calmar_weight,
            "hurst_weight": self.hurst_weight,
            "momentum_guard_pct": self.momentum_guard_pct,
        }


def index_window_slice(pos: int, window: int = 60) -> slice:
    """宽基指数滚动窗口切片：取 pos 及其前 window-1 个交易日（回测/主路径共用）。"""
    return slice(pos - (window - 1), pos + 1)


# ── 宏观摘要解析（Web 展示数据来源：macro_news 行 → 展示结构） ────
def parse_macro_summary(mn: dict | None) -> dict:
    """把 macro_news 行解析为 Web 展示结构（新闻/领涨领跌/资金流/赛道/大盘状态）。

    纯函数，Web 层只负责渲染。返回 dict 与 Web 面板模板约定字段一致。
    """
    import re as _re

    news_items = [{"title": "暂无快讯", "summary": "暂无快讯"}]
    sector_gainers = sector_losers = []
    flow_inflows = []
    flow_outflows = []
    sector_reasoning = ""
    regime_label = REGIME_NEUTRAL
    if mn:
        text = mn.get("news_summary") or ""
        lines = text.split("\n")
        seen = set()
        items = []
        for seg in lines:
            seg = seg.strip()
            if not seg or len(seg) < 6 or seg.startswith(("http", "www")):
                continue
            # 标题提取：去掉行首时间戳 [HH:MM]；冒号前为标题；
            # 无冒号则截取第一句（。！？）为止，不超过 50 字符，避免标题=全文
            body_text = _re.sub(r"^\[\d{1,2}:\d{2}\]\s*", "", seg)
            if "：" in body_text:
                title = body_text.split("：", 1)[0].strip()
            else:
                sent = _re.search(r"^(.{1,60}?)[。！？]", body_text)
                title = (sent.group(1).strip() if sent else body_text[:50]).strip()
            if not title:
                continue
            dedup = title[:100] if len(title) > 100 else title
            if dedup in seen:
                continue
            seen.add(dedup)
            items.append({"title": title, "summary": seg})  # 摘要 = 完整快讯行
        if items:
            news_items = items
        top_gainers = mn.get("top_gainers") or ""
        top_losers = mn.get("top_losers") or ""
        # 领涨/领跌行业（各取前3，带幅度强度）
        if top_gainers:
            raw_g = _re.findall(r"([^(]+)\(([^)]+)\)", top_gainers)[:9]
            if raw_g:
                g = [(n.strip("、 "), float(p.replace("%", ""))) for n, p in raw_g]
                m = len(g)
                sector_gainers = [
                    {"name": n, "pct": f"{v:+.2f}%", "s": 1 - i / (m - 1) if m > 1 else 0.5}
                    for i, (n, v) in enumerate(g)
                ]
        if top_losers:
            raw_l = _re.findall(r"([^(]+)\(([^)]+)\)", top_losers)[:3]
            if raw_l:
                l = [(n.strip("、 "), float(p.replace("%", ""))) for n, p in raw_l]
                l.sort(key=lambda x: x[1])
                if l:
                    m = len(l)
                    sector_losers = [
                        {"name": n, "pct": f"{v:+.2f}%", "s": 1 - i / (m - 1) if m > 1 else 0.5}
                        for i, (n, v) in enumerate(l)
                    ]
                    sector_losers.reverse()  # 左浅右深：跌幅从小到大排列
        # 资金流向（flow_json 合并行）
        flow_inflows = mn.get("flow_inflows") or []
        flow_outflows = [
            {**s, "abs": abs(s.get("flow", 0) or 0)}
            for s in (mn.get("flow_outflows") or [])
        ]
        # 赛道分析（context_json 合并行）
        sector_reasoning = zh_regime(mn.get("sector_reasoning") or "")
        # 大盘状态（LLM 可能输出 bullish/bearish/bull/bear 等变体，统一归一）
        regime_label = normalize_regime_label(mn.get("regime_label"))
    macro_data = {
        "news": "；".join(it["title"] for it in news_items),
        "news_items": news_items,
        "top_gainers": [],
        "top_losers": [],
        "etf_net_flow": "",
    }
    max_inflow = max((s.get("flow", 0) or 0 for s in flow_inflows), default=0)
    max_outflow = max((abs(s.get("flow", 0) or 0) for s in flow_outflows), default=0)
    return {
        "macro": macro_data,
        "sector_gainers": sector_gainers,
        "sector_losers": sector_losers,
        "flow_inflows": flow_inflows,
        "flow_outflows": flow_outflows,
        "flow_net_total": mn.get("flow_net_total") if mn else None,
        "max_inflow": max_inflow,
        "max_outflow": max_outflow,
        "sector_reasoning": sector_reasoning,
        "regime_label": regime_label,
        "macro_date": (mn or {}).get("date") or "",
    }

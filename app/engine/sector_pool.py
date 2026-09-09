"""量化定池（D4 定案）：纯量化代码筛选赛道候选池，LLM 只在池内行使否决权。

回测证据（data/sector_signal_backtest.json，2019-2026 共 42 决策日）：
- 5 日动量是唯一正区分信号（高十分位未来20日 +1.88%，胜率 52.6%）
- 60 日高动量反转（高十分位胜率 39.7% < 低 47.5%）→ 长期延长是过热信号
- "启动"象限（5d 强 + 20d 弱）胜率 58.9% vs "追高"（双强）45.2%
- 牛市追热度未来 ≈ 0（-0.1%），熊市 +3.72% → regime 交互

规则（全部纯量化，不喂 LLM；阈值用分位自适应避免拍死绝对值）：
1. 候选门槛：5 日动量中位数 > 0 且成员 ≥3
2. 过热剔除：60 日动量在全赛道前 25%（长期延长）
3. 追高降权：5d/20d 双强（追高组合）池内排序降权
4. 牛市热度降权：regime=BULL 时 5d 动量前 25% 降权
"""

from dataclasses import dataclass, field

from app.repo import base as repo

# 候选池目标大小（D5 定案：10-15 个）
POOL_TARGET = 12
# 赛道成员最少数量（少于 3 只基金无法形成有意义的赛道动量）
MIN_MEMBERS = 3
# 候选门槛：5 日动量中位数必须 > 0
MOM_5D_MIN = 0.0
# 过热定义：60 日动量高于全赛道 P75（长期延长）
OVERHEAT_60D_PCT = 75.0
# 追高定义：5 日动量前 P50 且 20 日动量前 P50（双强）
CHASE_5D_PCT = 50.0
CHASE_20D_PCT = 50.0
# 牛市热度定义：5 日动量前 P25
HOT_5D_PCT = 25.0
# 降权乘数（追高 0.8、牛市热度 0.7，可叠加）
CHASE_DOWNWEIGHT = 0.8
HOT_DOWNWEIGHT = 0.7
# Ticket 04：中长高位温和降权（40/60 日窗口验证反转显著减弱，不再整池剔除）
MID_HIGH_DOWNWEIGHT = 0.8
# Ticket 04：资金流出降权（近 5 日主力净流入累计为负 → 趋势走弱）
FLOW_DOWNWEIGHT = 0.7
# Ticket 04：极端高波动剔除分位（vol_20d 截面后 10%；风控过滤，非选基主因子）
EXTREME_VOL_PCT = 90.0
# T06 熊市分支（2026-09-05 熊市研究：24089 熊市样本中 vol 低分位胜率 60.1% vs 高分位 29.8%）
# 熊市低波动防守有效——高波动赛道额外降权（非剔除；与 EXTREME_VOL_PCT 并存）
BEAR_HIGH_VOL_PCT = 70.0
BEAR_HIGH_VOL_DOWNWEIGHT = 0.8


@dataclass
class SectorSignal:
    """单个赛道的量化信号与定池结果。"""
    sector: str
    mom_5d: float
    mom_20d: float
    mom_60d: float
    n: int
    score: float
    flags: list[str] = field(default_factory=list)
    # Ticket 04：趋势质量标签（聚合层单一来源：资金流出/短期走弱/高位降权/趋势健康）
    trend_label: str = ""
    # Ticket 07：近 5 日主力净流入累计（万元；素材/定池共用，None=板块快照缺失）
    flow_5d: float | None = None


@dataclass
class SectorPool:
    """量化定池结果：候选池 + 剔除记录（可度量/审计）。"""
    date: str
    regime: str
    candidates: list[SectorSignal] = field(default_factory=list)
    excluded: list[dict] = field(default_factory=list)
    reasoning: str = ""

    @property
    def candidate_names(self) -> list[str]:
        return [c.sector for c in self.candidates]


def _percentile(values: list[float], pct: float) -> float:
    """线性插值分位数（pct ∈ [0,100]），与 numpy 的 percentile 口径一致。"""
    if not values:
        return 0.0
    s = sorted(values)
    pos = (len(s) - 1) * pct / 100.0
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _signal_of(sector: str, stats: dict) -> SectorSignal:
    return SectorSignal(
        sector=sector,
        mom_5d=stats["mom_5d"],
        mom_20d=stats["mom_20d"],
        mom_60d=stats["mom_60d"],
        n=stats["n"],
        score=stats["mom_5d"],
    )


def build_sector_pool(date_str: str, available: list[str] | None = None) -> SectorPool:
    """量化定池入口：从可用赛道清单中筛出候选池（纯量化，无 LLM）。

    date_str 为决策日；若该日无特征数据（跨日/盘前运行），回退到
    <= 决策日的最近特征日（避免空池误判，池空即空推荐日）。
    """
    available = available if available is not None else repo.get_available_sectors()
    # 跨日/盘前回退：定池查询用最近有特征数据的日期
    eff_date = repo.get_latest_feature_date_before(date_str) or date_str
    regime = repo.get_market_regime()
    pool = SectorPool(date=eff_date, regime=regime)

    # Ticket 04 信号数据源：赛道多周期趋势特征（资金流趋势/波动率/20日高位标签，
    # 聚合层单一来源；定池/LLM 素材消费同一份数据）
    trend = repo.get_sector_trend_features(eff_date)

    signals: list[SectorSignal] = []
    for sector in available:
        stats = repo.get_sector_momentum_medians(sector, eff_date)
        if stats is None:
            pool.excluded.append({"sector": sector, "reason": "成员不足或无特征数据"})
            continue
        sig = _signal_of(sector, stats)
        sig.trend_label = (trend.get(sector) or {}).get("label", "")
        sig.flow_5d = (trend.get(sector) or {}).get("flow_5d")
        if sig.mom_5d <= MOM_5D_MIN:
            pool.excluded.append({"sector": sector, "reason": f"5日动量不足({sig.mom_5d:.1f}% ≤ {MOM_5D_MIN:.0f}%)"})
            continue
        signals.append(sig)

    if not signals:
        pool.reasoning = f"量化定池: 无满足门槛的赛道（可用 {len(available)} 个）"
        return pool

    # Ticket 04：极端高波动剔除（vol_20d 截面 P90+）——低波动主因子已撤销
    # （多窗口验证无区分度），仅保留极端波动作为风控过滤。
    vols = [float(t["vol_20d"]) for t in trend.values() if t.get("vol_20d") is not None]
    vol_p90 = _percentile(vols, EXTREME_VOL_PCT) if len(vols) >= 4 else None
    # T06 熊市分支：高波动降权阈值（vol_20d 截面 P70）
    vol_bear_th = _percentile(vols, BEAR_HIGH_VOL_PCT) if len(vols) >= 4 else None
    # 中长高位（60 日 P75）：由整池剔除改为温和降权（2026-09 多窗口验证：
    # 40/60 日持有视野下反转显著减弱，硬剔除会错杀刚启动的强势赛道）
    mom60 = [s.mom_60d for s in signals]
    overheat_th = _percentile(mom60, OVERHEAT_60D_PCT)
    keep: list[SectorSignal] = []
    for s in signals:
        tf = trend.get(s.sector) or {}
        vol = tf.get("vol_20d")
        if vol_p90 is not None and vol is not None and vol > vol_p90:
            pool.excluded.append({"sector": s.sector, "reason": f"极端高波动(vol {vol:.2f} > P90 {vol_p90:.2f})"})
            continue
        if overheat_th is not None and s.mom_60d > overheat_th:
            s.score *= MID_HIGH_DOWNWEIGHT
            s.flags.append("中长高位降权")
        if tf.get("label") == "高位降权":
            # 20 日动量截面高位（聚合层 P75 标签）→ 温和降权，可叠加
            s.score *= MID_HIGH_DOWNWEIGHT
            s.flags.append("20日高位降权")
        if tf.get("label") == "资金流出":
            # 近 5 日主力净流入累计为负 → 趋势走弱降权
            s.score *= FLOW_DOWNWEIGHT
            s.flags.append("资金流出")
        if regime == "BEAR" and vol_bear_th is not None and vol is not None and vol > vol_bear_th:
            # T06 熊市低波动防守：熊市高波动赛道额外降权（vol P70+）
            s.score *= BEAR_HIGH_VOL_DOWNWEIGHT
            s.flags.append("熊市高波动降权")
        keep.append(s)

    # 池内排序与降权：追高组合（5d/20d 双强）与牛市热度（BULL 下 5d 前 P25）
    if keep:
        mom5 = [s.mom_5d for s in keep]
        mom20 = [s.mom_20d for s in keep]
        chase_th5 = _percentile(mom5, CHASE_5D_PCT)
        chase_th20 = _percentile(mom20, CHASE_20D_PCT)
        hot_th5 = _percentile(mom5, HOT_5D_PCT)
        for s in keep:
            if s.mom_5d >= chase_th5 and s.mom_20d >= chase_th20:
                s.score *= CHASE_DOWNWEIGHT
                s.flags.append("追高降权")
            if regime == "BULL" and s.mom_5d >= hot_th5:
                s.score *= HOT_DOWNWEIGHT
                s.flags.append("牛市热度降权")

    keep.sort(key=lambda s: s.score, reverse=True)
    pool.candidates = keep[:POOL_TARGET]
    pool.reasoning = (
        f"量化定池: 可用{len(available)}个, 剔除{len(pool.excluded)}个, "
        f"候选{len(pool.candidates)}个, regime={regime}"
    )
    return pool

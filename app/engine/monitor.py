"""虚拟机监控引擎：规则层 + 复核层 → 四类信号（监控重构阶段一）。

规则层（纯函数、全量扫描）:
  R1    EMA60 趋势退出：NAV 连续 2 日 < EMA60（替代 2×ATR 追踪止损；回测验证回撤保护）
  R3b   风格漂移：买入时RBSA第一行业权重 - 当前 > 15% 或行业切换
  R3a   赛道锚点：当前 RBSA 行业 vs 推荐时 LLM 赛道判断（sector_selections 持久化值）
  R5    赛道优势：基金动量落后赛道中位数 → WARNING
  R2c   模型信号：预测 20 日绝对收益转负 → WARNING（阶段二升级为序列 EXIT）
复核层:
  R4    逻辑证伪：LLM 综合判断赛道方向+持仓匹配是否破裂（仅复核，不能推翻规则）

信号合并优先级: EXIT(离场) > WARNING(警惕) > BUY_MORE(加仓) > HOLD(无信号)

运行：uv run python monitor.py
"""

from datetime import datetime
from dataclasses import dataclass

from app.utils.log import get_logger

import numpy as np

from app.repo import (nav, get_latest_features,
                      get_sector_momentum_median, get_entry, get_entry_score,
                      get_holding_codes, update_status,
                      get_rbsa_at_date, get_first_rbsa_after,
                      get_holding_log_id, insert_monitor_event, exit_position,
                      get_entry_sector_anchor, get_entry_feature_snapshot,
                      get_latest_holdings_date, get_holdings_at_report,
                      get_index_rows,
                      get_available_sectors, insert_monitor_score,
                      get_recent_scores, get_recent_monitor_signals)
from app.model import score as model_score, latest_market_state, model_version
from app.features.calculator import ema60_exit  # R1 判定单一来源（回测模拟共用）
from app.llm.client import call_llm_json, LLMError
from app.llm.context import build_holdings_text
from app.llm.prompts import monitor_logic_prompt
from app import domain

logger = get_logger("monitor")

_DRIFT_THRESHOLD = 15.0
_NAV_STALE_TRADE_DAYS = 3  # 净值落后最近交易日超过该数视为陈旧
_MODEL_EXIT_CONFIRM = 3    # R2c 模型序列：连续 N 个交易日 score<0 才 EXIT（跨过 7 日惩罚赎回费率带）
_MODEL_SERIES_N = 5        # 确认期查询的序列长度上限
_WARNING_ESCALATE_DAYS = 20  # 阶段三：WARNING 持续 N 个监控日未缓解 → 升级 EXIT
_HOLD_STATES = domain.HOLDING_STATES


def _first_industry(feat: dict) -> str:
    """特征中 RBSA 第一行业名（去掉首尾空白）；无则空串。"""
    return (feat.get("rbsa_industry_1") or "").strip()


# ───────────────────────────────────────────
# 防线 Rule 类型：统一 interface
# ───────────────────────────────────────────

@dataclass
class DefenseResult:
    """防线检测结果。"""
    signal: str = "HOLD"     # EXIT / WARNING / HOLD / BUY_MORE
    reason: str = ""
    trailing: bool = False
    drift: bool = False
    sector_adv: bool = False


class DefenseContext:
    """防线检测上下文：run_monitor 一次性装配的基金快照，防线 check 只消费它（真纯函数）。

    架构深化候选 3：原 check_* 函数声称纯函数但逐个直读 DB（get_latest_features /
    get_entry_sector_anchor / get_sector_momentum_median 等），无法脱离真实库测试；
    现收敛为上下文装配——run_monitor 把该基金所需的锚点/特征/信号序列/净值全部取出，
    防线判定不再触碰 DB，可直接用构造的上下文做单元测试。
    """

    def __init__(self, code: str, buy_reason: str = "", sector: str = "",
                 navs: list[float] | None = None,
                 cur_feat: dict | None = None,
                 entry_rbsa: tuple | None = None, anchor: tuple | None = None,
                 entry_score: float | None = None,
                 scores_series: list | None = None,
                 entry_snapshot: dict | None = None,
                 sector_median: float | None = None,
                 holdings_text: str = "", rbsa_distribution: str = "",
                 available_sectors: list[str] | None = None,
                 latest_report_date: str | None = None,
                 r4_no_new_data: bool = False):
        self.code = code
        self.buy_reason = buy_reason
        self.sector = sector
        self.navs = navs or []
        # 预装配快照：防线判定只读这些字段，不再直读 DB
        self.cur_feat = cur_feat or {}          # 最新特征（含 rbsa_industry_1/weight_1、momentum_20d 等）
        self.entry_rbsa = entry_rbsa            # (买入时第一行业, 权重)，_entry_rbsa 三级回退结果
        self.anchor = anchor                    # (recommended, risk, reasoning) 推荐时赛道锚点
        self.entry_score = entry_score          # 买入时模型分
        self.scores_series = scores_series or []  # 最近 N 日 (date, score, version) 序列（倒序）
        self.entry_snapshot = entry_snapshot or {}  # 推荐时 feature_snapshot
        self.sector_median = sector_median      # 赛道动量中位数（None=赛道成员不足）
        self.holdings_text = holdings_text      # 最新持仓文本（R4 论点证伪用）
        self.rbsa_distribution = rbsa_distribution  # 当前 RBSA 行业暴露分布文本
        self.available_sectors = available_sectors or []  # RBSA 可用行业清单（锚点解析用）
        # R4 报告期守卫：最新持仓报告期与锚点报告期一致 → 无新披露数据，证伪无信息增量
        self.latest_report_date = latest_report_date
        self.r4_no_new_data = r4_no_new_data
        # P0-1：R4 逻辑证伪本轮是否因 LLM 不可用/解析失败而跳过（规则层信号不受影响）
        self.r4_skipped = False
        # P2-9：R4 预计算结果（run_monitor 并发装配阶段写入），链执行阶段直接消费，不再重复调用 LLM
        self.r4_logic: dict | None = None
        # P2-9：是否已预计算（区分"未预计算（需现场调用）"与"预计算失败（r4_logic=None）"）
        self.r4_precomputed: bool = False


class DefenseRule:
    """防线规则基类——每条规则声明优先级（severity）与是否短路，返回信号或 None 表示不触发。

    链按 severity 升序执行；short_circuit=True 的规则触发 EXIT 时立即中止。
    新增防线只需实现 check 并声明两个类属性，无需改动链本身。
    """

    severity: int = 0
    short_circuit: bool = False

    def check(self, ctx: DefenseContext) -> DefenseResult | None:
        raise NotImplementedError


class EmaTrendRule(DefenseRule):
    """R1：EMA60 趋势退出（替代 2×ATR 追踪止损）

    回测验证（全池 10 买点 63,698 样本，无前视）：最大回撤 14.6%→2.1%，
    熊市少亏 4.3pct；动量池收益 -3.10%→+1.37%、胜率 36.7%→72.7%。
    定位：被动回撤保护（趋势信号，牛市回调会触发），模型信号失灵时的兜底。
    """

    severity = 15
    short_circuit = True

    def check(self, ctx: DefenseContext) -> DefenseResult | None:
        exit_triggered, reason = ema60_exit(ctx.navs)
        return DefenseResult(signal=domain.SIGNAL_EXIT, reason=reason, trailing=True) if exit_triggered else None


class StyleDriftRule(DefenseRule):
    """防线2a：风格漂移——买入第一行业 ≠ 当前第一行业（行业切换）OR 买入权重下降 > 15%。

    行业切换检测修复：旧逻辑只比权重差，基金整体更换第一行业（权重不变）会漏检；
    买入基准三级回退由装配层 _entry_rbsa 完成，此处只消费 ctx 预装配结果。
    """

    severity = 20
    short_circuit = True

    def check(self, ctx: DefenseContext) -> DefenseResult | None:
        cur_feat = ctx.cur_feat
        cur_ind = _first_industry(cur_feat)
        cur_w = cur_feat.get("rbsa_weight_1")
        if cur_w is None:
            return None
        init_ind, init_w = ctx.entry_rbsa or (None, None)
        if init_ind is None and init_w is None:
            return None
        # 双检 1：行业切换（权重相同但第一行业更换）
        if init_ind and cur_ind and init_ind != cur_ind:
            return DefenseResult(
                signal=domain.SIGNAL_EXIT, drift=True,
                reason=f"风格漂移: 第一行业 {init_ind} → {cur_ind}"
                       + (f"（买入权重{init_w:.2f}）" if init_w is not None else ""),
            )
        # 双检 2：同一行业权重下降超过阈值
        if init_w is not None and (init_w - cur_w) > _DRIFT_THRESHOLD:
            return DefenseResult(
                signal=domain.SIGNAL_EXIT, drift=True,
                reason=(f"风格漂移: 买入权重{init_w:.2f} - 当前{cur_w:.2f}"
                        f"={init_w - cur_w:.2f} > 阈值{_DRIFT_THRESHOLD}"),
            )
        return None


class SectorAnchorRule(DefenseRule):
    """R3a：LLM 赛道锚点——当前 RBSA 行业 vs 推荐时 LLM 赛道判断。

    推荐时 LLM 选赛道并持久化到 sector_selections（recommended/risk_sectors）；
    监控读取该持久化值（非实时重建，避免上下文漂移）：
      - 当前第一行业 ∈ risk_sectors（推荐时明确规避）→ WARNING
      - 当前第一行业 ∉ recommended_sectors（离开推荐赛道）→ WARNING
    季度低频（RBSA 来自季报持仓聚合），EXIT 升级由状态机按确认期处理。
    """

    severity = 25
    short_circuit = False

    def check(self, ctx: DefenseContext) -> DefenseResult | None:
        anchor = ctx.anchor
        if not anchor:
            return None
        recommended, risk, _reasoning = anchor
        if not recommended and not risk:
            return None
        cur_ind = _first_industry(ctx.cur_feat)
        if not cur_ind:
            return None
        # 赛道解析与锚定收敛为 domain.SectorPolicy（推荐/监控共用单一来源）：
        # 锚点赛道名经同一套别名映射到 RBSA 行业名后做成员判断，避免命名空间不一致误报。
        policy = domain.SectorPolicy(ctx.available_sectors)
        risk_resolved = policy.resolve_set(risk)
        if cur_ind in risk_resolved:
            return DefenseResult(
                signal=domain.SIGNAL_WARNING,
                reason=f"赛道锚点: 当前第一行业[{cur_ind}]命中推荐时规避赛道{risk}",
            )
        if recommended:
            rec_resolved = policy.resolve_set(recommended)
            if rec_resolved and cur_ind not in rec_resolved:
                return DefenseResult(
                    signal=domain.SIGNAL_WARNING,
                    reason=f"赛道锚点: 当前第一行业[{cur_ind}]已离开推荐赛道{recommended}",
                )
        return None


class SectorAdvantageRule(DefenseRule):
    """防线2b：赛道优势丧失——输出 WARNING 而非 EXIT"""

    severity = 30
    short_circuit = False

    def check(self, ctx: DefenseContext) -> DefenseResult | None:
        feat = ctx.cur_feat
        fund_mom = feat.get("momentum_20d")
        latest_date = feat.get("date")
        if fund_mom is None or not latest_date:
            return None
        median = ctx.sector_median
        if median is None:
            logger.info("赛道 %s 基金不足 3 只，跳过赛道优势检测", ctx.sector or "未知")
            return None
        if fund_mom < median:
            reversal = feat.get("reversal_20d")
            if reversal is not None and reversal > 0:
                logger.info("基金 %s 动量落后赛道但已企稳(reversal=%.1f>0)，保留观察",
                            ctx.code, reversal)
                return None
            return DefenseResult(
                signal=domain.SIGNAL_WARNING, sector_adv=True,
                reason=(f"赛道优势丧失: 动量{fund_mom:.1f}% < 赛道中位数{median:.1f}%"
                        f" 且未企稳(reversal={reversal if reversal is not None else 0:.1f})"),
            )
        return None


class ModelSignalRule(DefenseRule):
    """R2c：模型信号序列退出（阶段二）——预测 20 日绝对收益转负确认后 EXIT。

    与推荐闭环：推荐时硬条件 score>0；监控每日用模型重打分并落库 monitor_scores。
    确认期（monitor_scores 序列，跨日状态）:
      - 连续 _MODEL_EXIT_CONFIRM 日 score<0 → EXIT（跨过 7 日惩罚赎回费率带）
      - 单日转负 → WARNING；相对买入分下降 >50% → WARNING
    模型版本边界：确认期内模型重训（版本变化）则重置连续计数（跨版本分数不可比）。
    """

    severity = 35
    short_circuit = False

    def check(self, ctx: DefenseContext) -> DefenseResult | None:
        series = ctx.scores_series
        if not series:
            return None
        today_score = float(series[0][1])
        buy_score = ctx.entry_score

        # 同版本连续段计数（版本跳变即重置）
        neg_run = 0
        cur_ver = series[0][2]
        for _, s, v in series:
            if v != cur_ver:
                break
            if s < 0:
                neg_run += 1
            else:
                break
        if neg_run >= _MODEL_EXIT_CONFIRM:
            return DefenseResult(
                signal=domain.SIGNAL_EXIT,
                reason=f"模型信号连续{neg_run}日转负: 当前预测收益 {today_score:.4f}",
            )
        if today_score < domain.MIN_PREDICTED_ALPHA:
            detail = f"模型信号转负: 当前预测收益 {today_score:.4f} < 0"
            if buy_score is not None:
                detail += f"（买入时 {buy_score:.4f}）"
            return DefenseResult(signal=domain.SIGNAL_WARNING, reason=detail)
        if buy_score is not None and today_score < 0.5 * buy_score:
            return DefenseResult(
                signal=domain.SIGNAL_WARNING,
                reason=f"模型信号相对买入分下降: 当前{today_score:.4f} < 买入{buy_score:.4f}的50%",
            )
        return None


class LogicVerificationRule(DefenseRule):
    """防线3：LLM 论点证伪（Q2 定案：只对比推荐时锚点 vs 最新持仓结构）"""

    severity = 40
    short_circuit = False

    def check(self, ctx: DefenseContext) -> DefenseResult | None:
        # R4 报告期守卫：最新持仓报告期 == 推荐时锚点报告期说明持仓数据未更新
        # （推荐当天必然成立）——同报告期不同切片（锚点前5 vs 最新前10）的对比
        # 无信息增量，只会把第 6-10 名重仓当"新增偏离"产出与推荐理由矛盾的噪声；
        # 新报告期数据出现前跳过本防线（规则层信号照常）。
        if ctx.r4_no_new_data:
            logger.info("R4 跳过（持仓报告期 %s 未更新，无新披露数据）: %s",
                        ctx.latest_report_date or "未知", ctx.code)
            return None
        # 论点锚点从推荐时 feature_snapshot 读取（核心行业 + 前N大重仓股 + 报告期），
        # 持仓文本与 RBSA 分布由装配层预取（ctx），判定本身不直读 DB。
        # P2-9：run_monitor 已并发预计算 R4 结果（r4_precomputed=True 时消费 r4_logic）；
        # 测试/单规则调用（未预计算）时兜底现场调用。
        if ctx.r4_precomputed:
            logic = ctx.r4_logic
        else:
            logic = _check_logic_enhanced(ctx)
        # P0-1：LLM 技术失败/解析失败返回 None → 跳过本防线（记录标志），
        # 规则层（R1/R2c/R3 等）信号照常产出——复核层失败不应拖死规则层。
        if logic is None:
            ctx.r4_skipped = True
            return None
        if logic["logic_verdict"] == "断裂":
            return DefenseResult(signal=domain.SIGNAL_EXIT, reason=f"LLM逻辑证伪: {logic['reason']}")
        if logic["signal_hint"] == domain.SIGNAL_BUY_MORE:
            return DefenseResult(signal=domain.SIGNAL_BUY_MORE, reason=logic.get("reason", ""),
                                 sector_adv=bool(logic.get("sector_risk")),
                                 drift=bool(logic.get("holding_risk")))
        if logic["signal_hint"] == domain.SIGNAL_WARNING or bool(logic.get("sector_risk")):
            return DefenseResult(signal=domain.SIGNAL_WARNING, reason=logic.get("reason", ""),
                                 sector_adv=bool(logic.get("sector_risk")))
        return DefenseResult(signal=domain.SIGNAL_HOLD, reason=logic.get("reason", ""))


# ───────────────────────────────────────────
# 防线函数（纯函数，可独立测试）
# ───────────────────────────────────────────


def _nav_since(code: str, since_date: str) -> list[float]:
    return [r[1] for r in nav.series(code, since=since_date)]


# ── 净值新鲜度护栏 ──

def _check_nav_freshness(code: str, trade_dates: list[str]) -> tuple[bool, str]:
    """净值新鲜度：最新净值日期落后最近交易日超过阈值 → (True, 原因)。

    净值陈旧时基金可能停牌/清盘/数据断裂，跳过防线链并告警，不产出信号。
    """
    rows = nav.series(code, limit=5)
    if not rows:
        return True, "净值数据缺失"
    latest_nav_date = rows[-1][0]
    if not trade_dates:
        return False, ""
    pos = np.searchsorted(trade_dates, latest_nav_date)
    if pos >= len(trade_dates) - _NAV_STALE_TRADE_DAYS:
        return False, ""
    return True, (
        f"净值陈旧: 最新净值 {latest_nav_date} 落后最近交易日 {trade_dates[-1]}"
        f"（>{_NAV_STALE_TRADE_DAYS} 个交易日）"
    )


# ── 模型信号防线 ──

def _current_model_score(feat: dict | None) -> float | None:
    """用当前模型对基金最新特征打分，返回预测 20 日绝对收益；无特征/无模型返回 None。

    特征由装配层传入（run_monitor 已取 get_latest_features），避免重复查询；
    市场状态列由调用方显式注入（最新指数状态），score 保持纯函数。
    """
    if not feat:
        return None
    return model_score(feat, market_state=latest_market_state())


def _entry_rbsa(code: str, reco_date: str | None,
                 snap: dict | None = None) -> tuple[str | None, float | None]:
    """买入时 (rbsa_industry_1, rbsa_weight_1)，三级回退。

    1. feature_snapshot（推荐时持久化的完整 RBSA，最可靠）——由装配层预取传入，
       避免与 run_monitor 的 entry_snapshot 重复查询；
    2. 买入日 fund_features 快照；
    3. 买入日之后首个非空快照（持仓报告期空窗兑底）。
    """
    if snap:
        ind = (snap.get("rbsa_industry_1") or "").strip()
        w = snap.get("rbsa_weight_1")
        if ind or w:
            return (ind or None), (float(w) if w is not None else None)
    if reco_date:
        row = get_rbsa_at_date(code, reco_date)
        if row and (row[0] or row[1]):
            return (row[0] or None), float(row[1]) if row[1] is not None else None
        row = get_first_rbsa_after(code, reco_date)
        if row and (row[0] or row[1]):
            return (row[0] or None), float(row[1]) if row[1] is not None else None
    return None, None


def _parse_logic_result(parsed) -> dict | None:
    """监控 LLM 判定解析校验：非 dict 视为无效（call_llm_json 的 per-prompt validator）。"""
    if not isinstance(parsed, dict):
        return None
    return parsed


def _rbsa_distribution(feat: dict | None) -> str:
    """基金 RBSA 行业暴露分布（如 '半导体(4.6%), 通信设备(4.1%), 电源设备(4.1%)'）。

    特征由装配层传入（run_monitor 已取 get_latest_features），判定侧不直读 DB。
    """
    if not feat:
        return ""
    parts = []
    for i in range(1, 4):
        ind = feat.get(f"rbsa_industry_{i}")
        w = feat.get(f"rbsa_weight_{i}")
        if ind and w:
            parts.append(f"{ind}({w:.1f}%)")
    return ", ".join(parts)


def _format_anchor_holdings(snapshot: dict, code: str | None = None) -> tuple[str, str, str]:
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
        rows = get_holdings_at_report(code, report_date, 10)
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


def _check_logic_enhanced(ctx: DefenseContext) -> dict | None:
    """R4 论点证伪（Q2 定案）：只对比"推荐时论点锚点 vs 最新持仓结构"。

    不再读取今日宏观/资金流/新闻——单日宏观翻转不构成离场依据。
    锚点/持仓文本/RBSA 分布均由装配层预取到 ctx，判定侧不直读 DB。

    返回 None 表示本轮无法判定（LLM 技术失败或解析失败）：调用方（LogicVerificationRule）
    跳过该防线，规则层信号照常——P0-1：复核层失败不拖死规则层。
    """
    entry_snapshot = ctx.entry_snapshot or {}
    anchor_sector, anchor_holdings, anchor_report_date = _format_anchor_holdings(entry_snapshot, ctx.code)

    prompt = monitor_logic_prompt(
        buy_reason=ctx.buy_reason,
        sector=ctx.sector,
        anchor_sector=anchor_sector or ctx.sector,
        anchor_report_date=anchor_report_date,
        anchor_holdings_text=anchor_holdings,
        holdings_text=ctx.holdings_text,
        rbsa_distribution=ctx.rbsa_distribution,
    )

    try:
        result = call_llm_json(
            prompt, temperature=0.1, max_tokens=16384,
            fallback=None,
            validator=_parse_logic_result,
            caller="monitor_r4",
        )
    except LLMError as e:
        logger.warning("R4 逻辑证伪 LLM 技术失败，跳过该防线（规则层照常）: %s | %s",
                       ctx.code, str(e)[:120])
        return None
    if result is None:
        # P0-1 降级（原为 raise 拒绝兑底）：解析失败/validator 拒绝时跳过 R4，
        # 规则层信号照常；日志与 Web 通过 ctx.r4_skipped 可见本轮证伪未执行。
        logger.warning("R4 逻辑证伪解析失败，跳过该防线（规则层照常）: %s", ctx.code)
        return None
    return result


def _log_monitor_event(code: str, signal: str, logic: dict,
                       trailing: bool, drift: bool, sector_adv: bool,
                       detail: str, is_stale: bool = False) -> None:
    log_id = get_holding_log_id(code, _HOLD_STATES)
    insert_monitor_event(
        code, datetime.now().strftime("%Y-%m-%d"), signal,
        trailing, drift, sector_adv,
        logic.get("logic_verdict", ""), logic.get("sector_risk", False),
        logic.get("holding_risk", False), detail, log_id, is_stale=is_stale,
    )


def _exit_position(code: str, sell_reason: str) -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    entry = get_entry(code, _HOLD_STATES)
    return_rate = None
    if entry:
        entry_nav = entry.get("entry_nav")
        latest_nav = nav.latest(code)
        if entry_nav and latest_nav:
            return_rate = latest_nav / entry_nav - 1.0
    exit_position(code, sell_reason, return_rate, _HOLD_STATES, today)
    logger.info("平仓 EXIT: %s | %s | 收益: %s", code, sell_reason,
                f"{return_rate*100:+.2f}%" if return_rate is not None else "未知")


def _update_signal(code: str, signal: str) -> None:
    update_status(code, signal, _HOLD_STATES)


def _warning_escalate(code: str) -> bool:
    """阶段三：WARNING 持续 _WARNING_ESCALATE_DAYS 个监控日且无缓解 → 升级 EXIT。

    缓解 = 任意非 WARNING 信号（HOLD/EXIT）中断连续序列；序列不足天数不升级。
    """
    rows = get_recent_monitor_signals(code, _WARNING_ESCALATE_DAYS)
    if len(rows) < _WARNING_ESCALATE_DAYS:
        return False
    return all(s == domain.SIGNAL_WARNING for _, s in rows[: _WARNING_ESCALATE_DAYS])


def _apply_defense_chain(ctx: DefenseContext,
                         rules: list[DefenseRule] | None = None) -> tuple[str, str, bool, bool, bool]:
    """防线链：按规则声明的 severity 升序执行，short_circuit 规则触发 EXIT 立即返回。

    rules 可注入（测试用），默认使用四条生产防线。
    """
    if rules is None:
        rules = [
            EmaTrendRule(),
            StyleDriftRule(),
            SectorAnchorRule(),
            SectorAdvantageRule(),
            ModelSignalRule(),
            LogicVerificationRule(),
        ]
    rules = sorted(rules, key=lambda r: r.severity)

    final_signal = "HOLD"
    reasons = []
    trailing = drift = sector_adv = False

    for rule in rules:
        result = rule.check(ctx)
        if result is None:
            continue
        reasons.append(result.reason)
        trailing = trailing or result.trailing
        drift = drift or result.drift
        sector_adv = sector_adv or result.sector_adv

        if rule.short_circuit and result.signal == domain.SIGNAL_EXIT:
            return (domain.SIGNAL_EXIT, result.reason, trailing, drift, sector_adv)
        # 显式信号优先级合并（EXIT > WARNING > BUY_MORE > HOLD），替代 last-wins：
        # 高 severity 规则的加仓建议不再误覆盖低 severity 规则的警惕信号
        if domain.SIGNAL_PRIORITY[result.signal] > domain.SIGNAL_PRIORITY[final_signal]:
            final_signal = result.signal

    detail = "; ".join(filter(None, reasons))
    return (final_signal, detail, trailing, drift, sector_adv)


def _run_r4_batch(ctxs: list[DefenseContext]) -> None:
    """P2-9：并发执行全部持仓的 R4 逻辑证伪（LLM 调用），结果写回 ctx.r4_logic。

    规则层（R1/R2c/R3 等）与 R4 无关且不依赖 LLM，先并发预取 R4 结果，
    链执行阶段不再逐持仓串行等待 LLM——持仓增多时监控槽位不被 LLM 延迟线性拖长。
    单持仓 R4 内部失败（LLMError/解析失败）已由 _check_logic_enhanced 降级为 None。
    """
    from concurrent.futures import ThreadPoolExecutor

    pending = [c for c in ctxs if not c.r4_no_new_data]
    if not pending:
        return
    workers = min(4, len(pending))

    def _verify(ctx: DefenseContext) -> dict | None:
        if ctx.r4_no_new_data:
            return None
        try:
            return _check_logic_enhanced(ctx)
        except Exception as e:
            # 防御兜底：R4 任何未预期异常不拖死批量（记录跳过）
            logger.warning("R4 并发调用异常，跳过该防线: %s | %s", ctx.code, str(e)[:120])
            return None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(_verify, pending))
    for ctx, logic in zip(pending, results):
        ctx.r4_logic = logic
        ctx.r4_precomputed = True
        ctx.r4_skipped = logic is None


def _build_defense_context(row: dict, date_str: str, trade_dates: list[str],
                           available: list[str]) -> "DefenseContext | None":
    """单只持仓的防线上下文装配：净值新鲜度护栏 + 快照 + 打分落库时序。

    架构深化 B：装配器独立可测（输入结构化持仓行，helper 依赖可注入），
    真 bug 曾栖息于 run_monitor 的位置解包与落库时序——现收敛为单一深模块；
    净值陈旧返回 None（数据告警已记，不参与防线链）。
    """
    code_str, name = row["code"], row["name"]
    reco_date, buy_reason, sector = row["reco_date"], row["buy_reason"], row["sector"]

    # 净值新鲜度护栏：净值陈旧（停牌/数据断裂）→ 记录数据告警事件（is_stale=1），
    # 不改变持仓状态、不参与防线链、不计入 WARNING 升级序列——
    # 数据问题与持仓信号语义分离（C5），避免净值停更 20 日被误升级为离场。
    stale, stale_reason = _check_nav_freshness(code_str, trade_dates)
    if stale:
        _log_monitor_event(code_str, domain.SIGNAL_WARNING,
            {"logic_verdict": "维持", "sector_risk": False, "holding_risk": False},
            False, False, False, stale_reason, is_stale=True)
        logger.warning("  %s（数据告警，不改持仓状态，不计入信号升级）", stale_reason)
        return None

    navs = _nav_since(code_str, reco_date)
    # 一次性装配基金快照：防线 check 只消费 ctx（真纯函数），不再各自直读 DB
    cur_feat = get_latest_features(code_str)
    entry_snapshot = get_entry_feature_snapshot(code_str)
    # R4 报告期守卫：最新持仓报告期 == 推荐时锚点报告期 → 持仓无新披露数据，
    # 证伪无信息增量（同报告期不同切片对比只会产出噪声，如推荐当天前5 vs 前10 的矛盾）；
    # 数据基座按报告期增量追加，报告期变化才是"结构可能变化"的信号。
    latest_report_date = get_latest_holdings_date(code_str)
    anchor_report_date = (entry_snapshot or {}).get("holdings_report_date") or ""
    r4_no_new_data = bool(latest_report_date and anchor_report_date
                          and latest_report_date == anchor_report_date)
    entry_rbsa = _entry_rbsa(code_str, reco_date, entry_snapshot)
    anchor = get_entry_sector_anchor(code_str, _HOLD_STATES)
    entry_score = get_entry_score(code_str)
    sector_median = None
    if sector and cur_feat:
        feat_date = cur_feat.get("date")
        if feat_date:
            sector_median = get_sector_momentum_median(sector, feat_date)

    # 模型预测分落库（R2c 确认期数据源）：当日打分先入库，再取序列——
    # 序列首位必须是当日分（ModelSignalRule 据此判定连续转负确认期）
    score_today = _current_model_score(cur_feat)
    if score_today is not None:
        insert_monitor_score(code_str, date_str, score_today, model_version())
    scores_series = get_recent_scores(code_str, _MODEL_SERIES_N)

    return DefenseContext(
        code=code_str, buy_reason=buy_reason or "", sector=sector or "",
        navs=navs, cur_feat=cur_feat, entry_rbsa=entry_rbsa, anchor=anchor,
        entry_score=entry_score, scores_series=scores_series,
        entry_snapshot=entry_snapshot, sector_median=sector_median,
        holdings_text=build_holdings_text(code_str, 10),
        rbsa_distribution=_rbsa_distribution(cur_feat),
        available_sectors=available,
        latest_report_date=latest_report_date,
        r4_no_new_data=r4_no_new_data,
    )


def run_monitor() -> None:
    rows = get_holding_codes(_HOLD_STATES)
    if not rows:
        logger.info("无持仓，监控结束")
        return

    date_str = datetime.now().strftime("%Y-%m-%d")
    trade_dates = [r[0] for r in get_index_rows(code="sh000300")]
    available = get_available_sectors()

    # 阶段 A：一次性装配全部持仓的防线上下文（DB 读取，串行）；净值陈旧直接记数据告警
    ctxs: list[DefenseContext] = []
    for row in rows:
        logger.info("=== 监控 %s %s [赛道:%s] ===", row["code"], row["name"], row["sector"] or "未知")
        ctx = _build_defense_context(row, date_str, trade_dates, available)
        if ctx is not None:
            ctxs.append(ctx)

    # 阶段 B：并发执行全部持仓的 R4 逻辑证伪（LLM 调用，唯一耗时环节）
    _run_r4_batch(ctxs)

    # 阶段 C：串行执行防线链 + 信号处理 + 写库（规则层纯函数，结果由阶段 B 预取）
    for ctx in ctxs:
        code_str = ctx.code
        signal, detail, trailing, drift, sector_adv = _apply_defense_chain(ctx)
        # P0-1：R4 证伪跳过（LLM 不可用/解析失败）时显式记录，信号仍由规则层产出
        if ctx.r4_skipped:
            logger.warning("  R4 逻辑证伪未执行（LLM 不可用），规则层信号照常: %s", code_str)

        if signal == domain.SIGNAL_WARNING and _warning_escalate(code_str):
            signal = domain.SIGNAL_EXIT
            detail = (f"WARNING持续{_WARNING_ESCALATE_DAYS}个监控日未缓解，升级EXIT | "
                      + (detail or ""))

        if signal == domain.SIGNAL_EXIT:
            _log_monitor_event(code_str, domain.SIGNAL_EXIT,
                {"logic_verdict": "", "sector_risk": False, "holding_risk": False, "reason": ""},
                trailing, drift, sector_adv, detail)
            _exit_position(code_str, detail)
            logger.info("  EXIT: %s", detail)
        else:
            _update_signal(code_str, signal)
            _log_monitor_event(code_str, signal,
                {"logic_verdict": "维持", "sector_risk": sector_adv, "holding_risk": drift},
                trailing, drift, sector_adv, detail)
            logger.info("  %s | %s", signal, detail)

    logger.info("监控完成: 扫描 %d 只", len(rows))


if __name__ == "__main__":
    run_monitor()

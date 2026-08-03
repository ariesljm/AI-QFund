"""虚拟机监控引擎：三道防线 → 四类信号（Phase 3 重构）。

防线1    追踪止损：highest_nav - current_nav > 2 × ATR(14)
防线2a   风格漂移：买入时RBSA第一行业权重 - 当前 > 15%
防线2b   赛道优势：基金动量落后赛道中位数 → WARNING
防线3    逻辑证伪：LLM 综合判断赛道方向+持仓匹配是否破裂

信号: EXIT(离场) > WARNING(警惕) > HOLD(持有) > BUY_MORE(加仓)

运行：uv run python monitor.py
"""

import time
from datetime import datetime
from dataclasses import dataclass

from app.utils.log import get_logger

import numpy as np

from app.repo import (get_nav_since, get_latest_nav, get_latest_features,
                      get_momentum_in_sector, get_reco_date_of, get_entry,
                      get_holding_codes, update_status, update_highest_nav,
                      get_rbsa_weight_at_date,
                      get_holding_log_id, insert_monitor_event, exit_position)
from app.llm.macro_agent import build_macro_context
from app.llm.client import call_llm_json
from app.llm.context import build_holdings_text
from app.llm.prompts import monitor_logic_prompt
from app import domain

logger = get_logger("monitor")

_DRIFT_THRESHOLD = 0.15
_ATR_MULTIPLE = 2.0
_HOLD_STATES = domain.HOLDING_STATES


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
    """防线检测上下文：收纳规则所需的全部输入，替代 8 参宽接口。"""

    def __init__(self, code: str, buy_reason: str = "", sector: str = "",
                 ctx=None, highest: float | None = None,
                 navs: list[float] | None = None, atr: float = 0.0,
                 reco_date: str = ""):
        self.code = code
        self.buy_reason = buy_reason
        self.sector = sector
        self.ctx = ctx  # MacroContext
        self.highest = highest
        self.navs = navs or []
        self.atr = atr
        self.reco_date = reco_date


class DefenseRule:
    """防线规则基类——每条规则声明优先级（severity）与是否短路，返回信号或 None 表示不触发。

    链按 severity 升序执行；short_circuit=True 的规则触发 EXIT 时立即中止。
    新增防线只需实现 check 并声明两个类属性，无需改动链本身。
    """

    severity: int = 0
    short_circuit: bool = False

    def check(self, ctx: DefenseContext) -> DefenseResult | None:
        raise NotImplementedError


class TrailingStopRule(DefenseRule):
    """防线1：追踪止损"""

    severity = 10
    short_circuit = True

    def check(self, ctx: DefenseContext):
        exit_triggered, reason = check_trailing_stop(ctx.code, ctx.highest, ctx.atr, ctx.navs)
        return DefenseResult(signal=domain.SIGNAL_EXIT, reason=reason, trailing=True) if exit_triggered else None


class StyleDriftRule(DefenseRule):
    """防线2a：风格漂移"""

    severity = 20
    short_circuit = True

    def check(self, ctx: DefenseContext):
        exit_triggered, reason = check_style_drift(ctx.code)
        return DefenseResult(signal=domain.SIGNAL_EXIT, reason=reason, drift=True) if exit_triggered else None


class SectorAdvantageRule(DefenseRule):
    """防线2b：赛道优势丧失——输出 WARNING 而非 EXIT"""

    severity = 30
    short_circuit = False

    def check(self, ctx: DefenseContext):
        lost, reason = check_sector_advantage(ctx.code, ctx.sector)
        return DefenseResult(signal=domain.SIGNAL_WARNING, reason=reason, sector_adv=True) if lost else None


class LogicVerificationRule(DefenseRule):
    """防线3：LLM 逻辑证伪"""

    severity = 40
    short_circuit = False

    def check(self, ctx: DefenseContext):
        logic = _check_logic_enhanced(ctx.code, ctx.buy_reason or "", ctx.sector or "", ctx.ctx)
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
    return get_nav_since(code, since_date)


def _calc_highest_nav(code: str, since_date: str) -> float | None:
    navs = _nav_since(code, since_date)
    return float(max(navs)) if navs else None


def calc_atr(navs: list[float], period: int = 14) -> float:
    """在收益率序列上计算 ATR，避免低净值基金被过早止损。"""
    if len(navs) < 2:
        return 0.0
    rets = [navs[i] / navs[i - 1] - 1.0 for i in range(1, len(navs))]
    if len(rets) < period:
        return float(np.mean(np.abs(rets))) if rets else 0.0
    return float(np.mean(np.abs(rets[-period:])))


def check_trailing_stop(code: str, highest_nav: float, atr: float,
                        navs: list[float] | None = None) -> tuple[bool, str]:
    if navs is None:
        navs = _nav_since(code, _reco_date_of(code))
    if not navs:
        return False, ""
    current = navs[-1]
    if highest_nav is None or highest_nav <= 0 or atr <= 0:
        return False, ""
    drawdown_pct = (highest_nav - current) / highest_nav
    if drawdown_pct > _ATR_MULTIPLE * atr:
        return True, (
            f"追踪止损: 回撤{drawdown_pct:.2%} > 2×ATR({atr:.4f})"
        )
    return False, ""


def _reco_date_of(code: str) -> str:
    return get_reco_date_of(code, _HOLD_STATES) or ""


def check_style_drift(code: str) -> tuple[bool, str]:
    reco_date = _reco_date_of(code)
    cur_feat = get_latest_features(code)

    init_w = None
    if reco_date:
        raw = get_rbsa_weight_at_date(code, reco_date)
        if raw is not None:
            init_w = float(raw)
    if init_w is None:
        return False, ""
    if not cur_feat:
        return False, ""
    cur_w = cur_feat.get("rbsa_weight_1")
    if cur_w is None:
        return False, ""
    drop = init_w - cur_w
    if drop > _DRIFT_THRESHOLD:
        return True, (
            f"风格漂移: 买入权重{init_w:.2f} - 当前{cur_w:.2f}"
            f"={drop:.2f} > 阈值{_DRIFT_THRESHOLD}"
        )
    return False, ""


def check_sector_advantage(code: str, sector: str) -> tuple[bool, str]:
    feat = get_latest_features(code)
    if not feat:
        return False, ""
    fund_mom = feat.get("momentum_20d")
    latest_date = feat.get("date")
    if fund_mom is None or not latest_date:
        return False, ""

    sector_moms = get_momentum_in_sector(sector, latest_date) if sector else []

    if len(sector_moms) < 3:
        logger.info("赛道 %s 基金不足 3 只，跳过赛道优势检测", sector or "未知")
        return False, ""

    values = sorted(sector_moms)
    n = len(values)
    median = values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2

    if fund_mom < median:
        return True, (
            f"赛道优势丧失: 动量{fund_mom:.1f}% < 赛道中位数{median:.1f}%"
        )
    return False, ""


def _parse_logic_result(parsed) -> dict | None:
    """监控 LLM 判定解析校验：非 dict 视为无效（call_llm_json 的 per-prompt validator）。"""
    if not isinstance(parsed, dict):
        return None
    return parsed


def _rbsa_distribution(code: str) -> str:
    """基金 RBSA 行业暴露分布（如 '半导体(4.6%), 通信设备(4.1%), 电源设备(4.1%)'）。"""
    feat = get_latest_features(code)
    if not feat:
        return ""
    parts = []
    for i in range(1, 4):
        ind = feat.get(f"rbsa_industry_{i}")
        w = feat.get(f"rbsa_weight_{i}")
        if ind and w:
            parts.append(f"{ind}({w:.1f}%)")
    return ", ".join(parts)


def _check_logic_enhanced(code: str, buy_reason: str, sector: str,
                          ctx) -> dict:
    holdings_text = build_holdings_text(code, 5)

    prompt = monitor_logic_prompt(
        buy_reason=buy_reason,
        sector=sector,
        recommended_sectors=ctx.recommended_sectors,
        risk_sectors=ctx.risk_sectors,
        regime_label=ctx.regime_label,
        sector_reasoning=ctx.sector_reasoning,
        holdings_text=holdings_text,
        news_summary=ctx.news_brief or ctx.news_summary,
        rbsa_distribution=_rbsa_distribution(code),
    )

    return call_llm_json(
        prompt, temperature=0.1, max_tokens=512,
        fallback={
            "logic_verdict": "维持", "signal_hint": "HOLD",
            "sector_risk": False, "holding_risk": False,
            "reason": "LLM 调用或解析失败，保守维持",
        },
        validator=_parse_logic_result,
    )


def _log_monitor_event(code: str, signal: str, logic: dict,
                       trailing: bool, drift: bool, sector_adv: bool,
                       detail: str) -> None:
    log_id = get_holding_log_id(code, _HOLD_STATES)
    insert_monitor_event(
        code, datetime.now().strftime("%Y-%m-%d"), signal,
        trailing, drift, sector_adv,
        logic.get("logic_verdict", ""), logic.get("sector_risk", False),
        logic.get("holding_risk", False), detail, log_id,
    )


def _exit_position(code: str, sell_reason: str) -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    entry = get_entry(code, _HOLD_STATES)
    return_rate = None
    if entry:
        entry_nav = entry.get("entry_nav")
        latest_nav = get_latest_nav(code)
        if entry_nav and latest_nav:
            return_rate = latest_nav / entry_nav - 1.0
    exit_position(code, sell_reason, return_rate, _HOLD_STATES, today)
    logger.info("平仓 EXIT: %s | %s | 收益: %s", code, sell_reason,
                f"{return_rate*100:+.2f}%" if return_rate is not None else "未知")


def _update_signal(code: str, signal: str) -> None:
    update_status(code, signal, _HOLD_STATES)


def _apply_defense_chain(ctx: DefenseContext,
                         rules: list[DefenseRule] | None = None) -> tuple[str, str, bool, bool, bool]:
    """防线链：按规则声明的 severity 升序执行，short_circuit 规则触发 EXIT 立即返回。

    rules 可注入（测试用），默认使用四条生产防线。
    """
    if rules is None:
        rules = [
            TrailingStopRule(),
            StyleDriftRule(),
            SectorAdvantageRule(),
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
        if result.signal != "HOLD":
            final_signal = result.signal

    detail = "; ".join(filter(None, reasons))
    return (final_signal, detail, trailing, drift, sector_adv)


def run_monitor() -> None:
    rows = get_holding_codes(_HOLD_STATES)
    if not rows:
        logger.info("无持仓，监控结束")
        return

    date_str = datetime.now().strftime("%Y-%m-%d")
    ctx = build_macro_context(date_str)

    for code in rows:
        code_str, name, reco_date, buy_reason, sector = code[0], code[1], code[2], code[3], code[4]
        logger.info("=== 监控 %s %s [赛道:%s] ===", code_str, name, sector or "未知")

        highest = _calc_highest_nav(code_str, reco_date)
        if highest is not None:
            update_highest_nav(code_str, highest, _HOLD_STATES)
        navs = _nav_since(code_str, reco_date)
        atr = calc_atr(navs)

        signal, detail, trailing, drift, sector_adv = _apply_defense_chain(
            DefenseContext(
                code=code_str, buy_reason=buy_reason or "", sector=sector or "",
                ctx=ctx, highest=highest, navs=navs, atr=atr, reco_date=reco_date,
            )
        )

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

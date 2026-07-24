"""虚拟机监控引擎：三道防线 → 四类信号（Phase 3 重构）。

防线1    追踪止损：highest_nav - current_nav > 2 × ATR(14)
防线2a   风格漂移：买入时RBSA第一行业权重 - 当前 > 15%
防线2b   赛道优势：基金动量落后赛道中位数 → WARNING
防线3    逻辑证伪：LLM 综合判断赛道方向+持仓匹配是否破裂

信号: EXIT(离场) > WARNING(警惕) > HOLD(持有) > BUY_MORE(加仓)

运行：uv run python monitor.py
"""

import json
import logging
import time
from datetime import datetime

import numpy as np

from data_foundation import _get_db
from macro_agent import build_macro_context

logger = logging.getLogger("monitor")

_DRIFT_THRESHOLD = 0.15
_ATR_MULTIPLE = 2.0
# 持有中的状态值
_HOLD_STATES = ("HOLD", "BUY_MORE", "WARNING")


def _nav_since(code: str, since_date: str) -> list[float]:
    conn = _get_db()
    rows = conn.execute(
        "SELECT cum_nav FROM fund_nav WHERE code = ? AND date >= ? ORDER BY date ASC",
        (code, since_date),
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def update_highest_nav(code: str, since_date: str) -> float | None:
    navs = _nav_since(code, since_date)
    return float(max(navs)) if navs else None


def calc_atr(navs: list[float], period: int = 14) -> float:
    if len(navs) < 2:
        return 0.0
    tr = [abs(navs[i] - navs[i - 1]) for i in range(1, len(navs))]
    if len(tr) < period:
        return float(np.mean(tr)) if tr else 0.0
    return float(np.mean(tr[-period:]))


def check_trailing_stop(code: str, highest_nav: float, atr: float,
                        navs: list[float] | None = None) -> tuple[bool, str]:
    if navs is None:
        navs = _nav_since(code, _reco_date_of(code))
    if not navs:
        return False, ""
    current = navs[-1]
    if highest_nav is None or highest_nav <= 0 or atr <= 0:
        return False, ""
    if highest_nav - current > _ATR_MULTIPLE * atr:
        return True, (
            f"追踪止损: 最高{highest_nav:.4f} - 当前{current:.4f}"
            f"={highest_nav - current:.4f} > 2×ATR({atr:.4f})"
        )
    return False, ""


def _reco_date_of(code: str) -> str:
    conn = _get_db()
    row = conn.execute(
        f"SELECT recommend_date FROM recommend_log "
        f"WHERE code = ? AND status IN ({','.join('?' * len(_HOLD_STATES))}) "
        "ORDER BY id DESC LIMIT 1",
        (code, *_HOLD_STATES),
    ).fetchone()
    conn.close()
    return row[0] if row else ""


def check_style_drift(code: str) -> tuple[bool, str]:
    conn = _get_db()
    reco_date = _reco_date_of(code)
    init_row = conn.execute(
        "SELECT rbsa_weight_1 FROM fund_features WHERE code = ? AND date = ?",
        (code, reco_date),
    ).fetchone()
    cur_row = conn.execute(
        "SELECT rbsa_weight_1 FROM fund_features WHERE code = ? "
        "ORDER BY date DESC LIMIT 1", (code,)
    ).fetchone()
    conn.close()
    if not init_row or not cur_row or init_row[0] is None or cur_row[0] is None:
        return False, ""
    init_w, cur_w = float(init_row[0]), float(cur_row[0])
    drop = init_w - cur_w
    if drop > _DRIFT_THRESHOLD:
        return True, (
            f"风格漂移: 买入权重{init_w:.2f} - 当前{cur_w:.2f}"
            f"={drop:.2f} > 阈值{_DRIFT_THRESHOLD}"
        )
    return False, ""


def check_sector_advantage(code: str, sector: str) -> tuple[bool, str]:
    """检查基金是否落后于赛道中位数 → 赛道优势丧失预警。"""
    conn = _get_db()
    row = conn.execute(
        "SELECT momentum_20d FROM fund_features WHERE code=? ORDER BY date DESC LIMIT 1",
        (code,),
    ).fetchone()
    if not row:
        conn.close()
        return False, ""
    fund_mom = row[0]

    latest_date = conn.execute(
        "SELECT date FROM fund_features WHERE code=? ORDER BY date DESC LIMIT 1",
        (code,),
    ).fetchone()
    if not latest_date:
        conn.close()
        return False, ""

    rows = conn.execute(
        "SELECT momentum_20d FROM fund_features "
        "WHERE rbsa_industry_1 = ? AND date = ? AND momentum_20d IS NOT NULL",
        (sector, latest_date[0]),
    ).fetchall()
    conn.close()

    if len(rows) < 3:
        logger.info("赛道 %s 基金不足 3 只，跳过赛道优势检测", sector or "未知")
        return False, ""

    values = sorted(r[0] for r in rows)
    n = len(values)
    median = values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2

    if fund_mom < median:
        return True, (
            f"赛道优势丧失: 动量{fund_mom:.1f}% < 赛道中位数{median:.1f}%"
        )
    return False, ""


def _check_logic_enhanced(code: str, buy_reason: str, sector: str,
                          ctx, conn) -> dict:
    """增强版 LLM 逻辑证伪（赛道方向+持仓匹配合并为一次调用）。

    返回: {logic_verdict, signal_hint, sector_risk, holding_risk, reason}
    """
    from data_foundation import _call_llm

    # 基金当前重仓股
    hold_rows = conn.execute(
        "SELECT h.stock_name, h.weight, COALESCE(s.industry_name, '其他') "
        "FROM fund_holdings h "
        "LEFT JOIN stock_industry_map s ON h.stock_code = s.stock_code "
        "WHERE h.code = ? "
        "AND h.report_date = (SELECT MAX(report_date) FROM fund_holdings WHERE code = ?) "
        "ORDER BY h.weight DESC LIMIT 5",
        (code, code),
    ).fetchall()
    holdings_text = "；".join(
        f"{r[0]}({r[2]},{r[1]:.1f}%)" for r in hold_rows
    ) if hold_rows else "无持仓数据"

    # CLS 新闻匹配：该基金持仓股中有哪些在今日新闻中被提及
    matched_lines = []
    for r in hold_rows:
        stock_name = r[0]
        for s in ctx.cls_stock_mentions:
            if s["name"] == stock_name:
                matched_lines.append(
                    f"  {stock_name}: 等级={s['level']} \"{s['title'][:60]}\""
                )
                break
    matched_text = "\n".join(matched_lines) if matched_lines else "无匹配"

    prompt = (
        "你是基金投研审核员。根据买入逻辑、该基金的赛道归属和今日宏观数据，"
        "判定买入逻辑是否维持，并给出信号建议。\n\n"
        f"买入逻辑: {buy_reason}\n"
        f"该基金所属赛道: {sector}\n\n"
        "【今日宏观判定】\n"
        f"推荐赛道: {', '.join(ctx.recommended_sectors) or '无'}\n"
        f"回避赛道: {', '.join(ctx.risk_sectors) or '无'}\n"
        f"大盘判定: {ctx.regime_label}\n"
        f"赛道推论: {ctx.sector_reasoning or '无'}\n\n"
        f"【该基金当前重仓股】\n{holdings_text}\n\n"
        f"【该基金持仓股在今日新闻中的提及】\n{matched_text}\n\n"
        "【今日财经新闻全文】\n"
        f"{ctx.news_summary}\n\n"
        "输出纯 JSON：\n"
        "{\n"
        '  "logic_verdict": "维持/断裂",\n'
        '  "signal_hint": "HOLD/BUY_MORE/WARNING",\n'
        '  "sector_risk": true/false,\n'
        '  "holding_risk": true/false,\n'
        '  "reason": "说明"\n'
        "}\n\n"
        "判定规则：\n"
        "- 若该基金所属赛道出现在回避赛道中，或新闻对该赛道有明确利空 → 赛道风险\n"
        "- 若持仓股在今日新闻中有明确利空 → 持仓风险\n"
        "- 任一风险推断买入逻辑断裂 → 断裂\n"
        "- 若该基金赛道仍在推荐赛道中、持仓股有正面新闻 → BUY_MORE\n"
        "- 赛道方向中性但持仓无异常 → HOLD"
    )

    content = _call_llm(prompt, temperature=0.1, max_tokens=512)
    if content is None:
        return {
            "logic_verdict": "维持", "signal_hint": "HOLD",
            "sector_risk": False, "holding_risk": False,
            "reason": "LLM 未配置或调用失败，保守维持",
        }
    try:
        result = json.loads(content)
        return {
            "logic_verdict": result.get("logic_verdict", "维持"),
            "signal_hint": result.get("signal_hint", "HOLD"),
            "sector_risk": bool(result.get("sector_risk", False)),
            "holding_risk": bool(result.get("holding_risk", False)),
            "reason": result.get("reason", ""),
        }
    except Exception as e:
        return {
            "logic_verdict": "维持", "signal_hint": "HOLD",
            "sector_risk": False, "holding_risk": False,
            "reason": f"LLM 解析失败({e})，保守维持",
        }


def _log_monitor_event(code: str, signal: str, logic: dict,
                       trailing: bool, drift: bool, sector_adv: bool,
                       detail: str) -> None:
    conn = _get_db()
    log_id = conn.execute(
        f"SELECT id FROM recommend_log WHERE code = ? AND status IN "
        f"({','.join('?'*len(_HOLD_STATES))}) ORDER BY id DESC LIMIT 1",
        (code, *_HOLD_STATES),
    ).fetchone()
    conn.execute(
        "INSERT INTO monitor_events "
        "(code, date, signal, trigger_trailing, trigger_drift, trigger_sector_adv, "
        "logic_verdict, sector_risk, holding_risk, detail, recommend_log_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (code, datetime.now().strftime("%Y-%m-%d"), signal,
         trailing, drift, sector_adv,
         logic.get("logic_verdict", ""), logic.get("sector_risk", False),
         logic.get("holding_risk", False), detail,
         log_id[0] if log_id else None),
    )
    conn.commit()
    conn.close()


def _exit_position(code: str, sell_reason: str) -> None:
    conn = _get_db()
    today = datetime.now().strftime("%Y-%m-%d")
    reco = conn.execute(
        f"SELECT recommend_date FROM recommend_log "
        f"WHERE code=? AND status IN ({','.join('?'*len(_HOLD_STATES))}) "
        "ORDER BY id DESC LIMIT 1",
        (code, *_HOLD_STATES),
    ).fetchone()
    return_rate = None
    if reco:
        nav_r = conn.execute(
            "SELECT cum_nav FROM fund_nav WHERE code=? AND date=?", (code, reco[0])
        ).fetchone()
        nav_e = conn.execute(
            "SELECT cum_nav FROM fund_nav WHERE code=? AND date=?", (code, today)
        ).fetchone()
        if nav_r and nav_e and nav_r[0] and nav_e[0]:
            return_rate = nav_e[0] / nav_r[0] - 1.0
    conn.execute(
        "UPDATE recommend_log SET status='EXIT', sell_reason=?, exit_date=?, return_rate=? "
        f"WHERE code=? AND status IN ({','.join('?'*len(_HOLD_STATES))})",
        (sell_reason, today, return_rate, code, *_HOLD_STATES),
    )
    conn.commit()
    conn.close()
    logger.info("平仓 EXIT: %s | %s | 收益: %s", code, sell_reason,
                f"{return_rate*100:+.2f}%" if return_rate is not None else "未知")


def _update_signal(code: str, signal: str) -> None:
    """更新非 EXIT 状态信号（HOLD/BUY_MORE/WARNING）。"""
    conn = _get_db()
    conn.execute(
        f"UPDATE recommend_log SET status = ? "
        f"WHERE code = ? AND status IN ({','.join('?'*len(_HOLD_STATES))})",
        (signal, code, *_HOLD_STATES),
    )
    conn.commit()
    conn.close()


def run_monitor() -> None:
    """遍历所有 HOLD 基金，执行三道防线，输出四类信号。"""
    conn = _get_db()
    rows = conn.execute(
        f"SELECT code, name, recommend_date, buy_reason, "
        "(SELECT rbsa_industry_1 FROM fund_features ff "
        " WHERE ff.code = r.code ORDER BY ff.date DESC LIMIT 1) as sector "
        "FROM recommend_log r "
        f"WHERE r.status IN ({','.join('?'*len(_HOLD_STATES))})",
        _HOLD_STATES,
    ).fetchall()
    if not rows:
        logger.info("无持仓，监控结束")
        conn.close()
        return

    date_str = datetime.now().strftime("%Y-%m-%d")
    ctx = build_macro_context(date_str)

    for code, name, reco_date, buy_reason, sector in rows:
        logger.info("=== 监控 %s %s [赛道:%s] ===", code, name, sector or "未知")

        # 防线1：追踪止损
        highest = update_highest_nav(code, reco_date)
        if highest is not None:
            conn.execute(
                f"UPDATE recommend_log SET highest_nav = ? "
                f"WHERE code = ? AND status IN ({','.join('?'*len(_HOLD_STATES))})",
                (highest, code, *_HOLD_STATES),
            )
            conn.commit()
        navs = _nav_since(code, reco_date)
        atr = calc_atr(navs)

        exit_triggered, exit_reason = check_trailing_stop(code, highest, atr, navs)
        trail_hit = exit_triggered
        drift_hit = False
        if not exit_triggered:
            exit_triggered, exit_reason = check_style_drift(code)
            drift_hit = exit_triggered

        # 防线2b：赛道优势检测（无论是否已触发EXIT，都记录完整信息供进化分析）
        advantage_lost, advantage_reason = check_sector_advantage(code, sector)

        if exit_triggered:
            _log_monitor_event(code, "EXIT",
                {"logic_verdict": "", "sector_risk": False, "holding_risk": False, "reason": ""},
                trail_hit, drift_hit, advantage_lost, exit_reason)
            _exit_position(code, exit_reason)
            logger.info("  EXIT: %s", exit_reason)
            continue

        # 防线3：增强版 LLM 逻辑证伪
        logic = _check_logic_enhanced(code, buy_reason or "", sector or "", ctx, conn)

        if logic["logic_verdict"] == "断裂":
            _log_monitor_event(code, "EXIT", logic, trail_hit, drift_hit,
                               advantage_lost, f"LLM逻辑证伪: {logic['reason']}")
            _exit_position(code, f"LLM逻辑证伪: {logic['reason']}")
            logger.info("  EXIT: %s", logic['reason'])
            continue

        # 信号判定
        if logic["signal_hint"] == "BUY_MORE" and not advantage_lost:
            signal = "BUY_MORE"
        elif advantage_lost or logic["signal_hint"] == "WARNING":
            signal = "WARNING"
        else:
            signal = "HOLD"

        _update_signal(code, signal)
        detail = "; ".join(filter(None, [advantage_reason, logic["reason"]]))
        _log_monitor_event(code, signal, logic, trail_hit, drift_hit,
                           advantage_lost, detail)
        logger.info("  %s | 赛道风险=%s 持仓风险=%s | %s",
                    signal, logic["sector_risk"], logic["holding_risk"], detail)

    conn.close()
    logger.info("监控完成: 扫描 %d 只", len(rows))


if __name__ == "__main__":
    run_monitor()

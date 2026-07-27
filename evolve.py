"""进化引擎：月度结算 → 批量 LLM 元分析 → 教训入库（Phase 4 重构）。

闭环：sector_selections(趋势) + monitor_events(信号链)
      → LLM 对比成功/失败模式 → evolution_insights
      → 回流 选赛道 LLM + 定论 LLM。

运行：uv run python evolve.py [2026-07]
"""

import json
import logging
from log_utils import get_logger
import re
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from data_store import _get_db, _load_settings
from data_store import _db_conn

logger = get_logger("evolve")

_RANKING_CFG_PATH = Path("config/settings.toml")
_OUTCOME_DAYS_THRESHOLD = 20


# ── 排序自纠偏（保留）──────────────────────────────────────

def _apply_ranking_weights(weights: dict) -> bool:
    """将排序权重写入 meta 表，供 recommend._load_ranking_cfg 读取。"""
    try:
        with _db_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('ranking_cfg', ?)",
                (json.dumps(weights),),
            )
        return True
    except Exception as e:
        logger.warning("写入排序权重失败: %s", str(e)[:120], exc_info=True)
        return False


def _review_ranking_all() -> list[str]:
    with _db_conn() as conn:
        rows = conn.execute(
            "SELECT ff.code, ff.momentum_20d, ff.hurst_60d, ff.calmar "
            "FROM fund_features ff JOIN fund_basic fb ON fb.code=ff.code "
            "WHERE fb.is_buyable=1"
        ).fetchall()
    if len(rows) < 200:
        return []

    mom = np.array([r[1] for r in rows], dtype=float)
    hurst = np.array([r[2] for r in rows], dtype=float)
    calmar = np.array([r[3] for r in rows], dtype=float)
    fixes = []

    corr_hm = np.corrcoef(hurst, mom)[0, 1]
    if corr_hm < 0:
        fixes.append(f"hurst与动量负相关({corr_hm:+.3f})，趋势信号失效")

    corr_cm = np.corrcoef(calmar, mom)[0, 1]
    if corr_cm < 0:
        fixes.append(f"calmar与动量负相关({corr_cm:+.3f})，回撤质量信号失效")

    with _db_conn() as idx:
        idx_rows = idx.execute(
            "SELECT close FROM index_daily WHERE code='sh000300' ORDER BY date DESC LIMIT 21"
        ).fetchall()
    if len(idx_rows) >= 21:
        idx_mom = (idx_rows[0][0] / idx_rows[-1][0] - 1) * 100
        rel = mom - idx_mom
        sorted_rel = np.sort(rel)[::-1]
        top10_mean = sorted_rel[:10].mean()
        bot10_mean = sorted_rel[-10:].mean()
        spread = top10_mean - bot10_mean
        if spread < 10:
            fixes.append(f"相对强弱区分度不足(Top10-Bottom10={spread:.1f}pp)")

    if fixes:
        new = {
            "model_weight": 0.5, "rel_strength_weight": 0.3,
            "calmar_weight": 0.1, "hurst_weight": 0.05,
            "momentum_guard_pct": -15.0,
        }
        _apply_ranking_weights(new)
    return fixes


# ── 月度结算 ───────────────────────────────────────────────

def _settle_outcomes(month: str) -> int:
    """更新 sector_selections 的 outcome 字段。"""
    with _db_conn() as conn:
        rows = conn.execute(
            "SELECT id, recommend_log_id FROM sector_selections "
            "WHERE date LIKE ? AND (outcome = '待定' OR outcome IS NULL)",
            (f"{month}%",),
        ).fetchall()
        settled = 0
        today = datetime.now().strftime("%Y-%m-%d")

        for ss_id, log_id in rows:
            if not log_id:
                continue
            log = conn.execute(
                "SELECT status, return_rate, recommend_date FROM recommend_log WHERE id = ?",
                (log_id,),
            ).fetchone()
            if not log:
                continue
            status, ret, reco_date = log

            if status in ("EXIT", "HOLD", "BUY_MORE", "WARNING"):
                reco_dt = datetime.strptime(reco_date, "%Y-%m-%d")
                days = (datetime.now() - reco_dt).days
                if days < _OUTCOME_DAYS_THRESHOLD and status != "EXIT":
                    continue

            outcome, note = "平", ""
            if status == "EXIT":
                if ret is not None:
                    if ret > 0.02:
                        outcome, note = "胜", f"退出时收益 {ret*100:+.2f}%"
                    elif ret < -0.05:
                        outcome, note = "负", f"退出时亏损 {ret*100:.2f}%"
                    else:
                        outcome, note = "平", f"退出时收益 {ret*100:+.2f}%"
            else:
                if ret is not None:
                    if ret > 0.02:
                        outcome, note = "胜", f"运行{days}日收益 {ret*100:+.2f}%"
                    elif ret < -0.05:
                        outcome, note = "负", f"运行{days}日亏损 {ret*100:.2f}%"

            conn.execute(
                "UPDATE sector_selections SET outcome=?, outcome_date=?, outcome_note=? WHERE id=?",
                (outcome, today, note, ss_id),
            )
            settled += 1

    logger.info("月度结算: %d 条 sector_selections 已更新 outcome", settled)
    return settled


# ── 批量 LLM 元分析 ───────────────────────────────────────

def _collect_cases(month: str) -> tuple[list[dict], list[dict], list[dict]]:
    """收集当月推荐案例（含回填 outcome 后的结果 + 监控信号链）。"""
    with _db_conn() as conn:
        rows = conn.execute(
            "SELECT ss.id, ss.recommend_log_id, ss.recommended_sectors, ss.sector_reasoning, "
            "ss.regime_label, ss.outcome, ss.outcome_note, rl.buy_reason, rl.code, rl.name, "
            "me.signal, me.trigger_trailing, me.trigger_drift, me.trigger_sector_adv, "
            "me.logic_verdict, me.sector_risk, me.holding_risk, me.detail "
            "FROM sector_selections ss "
            "LEFT JOIN recommend_log rl ON rl.id = ss.recommend_log_id "
            "LEFT JOIN monitor_events me ON me.recommend_log_id = rl.id "
            "WHERE ss.date LIKE ? AND ss.outcome != '待定' "
            "ORDER BY ss.date DESC LIMIT 20",
            (f"{month}%",),
        ).fetchall()

    successes, failures, neutrals = [], [], []
    for r in rows:
        (_, log_id, sectors_json, reasoning, regime, outcome, note, buy_reason, code, name,
         signal, trig_trail, trig_drift, trig_sector, logic_v, sector_r, holding_r, detail) = r
        sectors = json.loads(sectors_json) if sectors_json else []
        case = {
            "sectors": sectors, "fund": f"{code} {name}" if code else "无",
            "outcome": outcome, "note": note or "",
            "reasoning": (reasoning or "")[:200],
            "buy_reason": (buy_reason or "")[:200],
            "regime": regime or "",
            "signal": signal or "",
            "signal_triggers": {
                "trailing": trig_trail or 0, "drift": trig_drift or 0,
                "sector_adv": trig_sector or 0,
            },
            "logic": {
                "verdict": logic_v or "", "sector_risk": sector_r or 0,
                "holding_risk": holding_r or 0, "reason": (detail or "")[:200],
            },
        }
        if outcome == "胜":
            successes.append(case)
        elif outcome == "负":
            failures.append(case)
        else:
            neutrals.append(case)

    return successes, failures, neutrals


def _batch_llm_analyze(successes: list, failures: list, neutrals: list | None = None) -> list[dict]:
    """LLM 对比成功/失败/中性案例，提取系统性洞察。"""
    from data_foundation import _call_llm

    if neutrals is None:
        neutrals = []

    prompt = "你是基金投资策略的系统优化架构师。\n\n"
    prompt += "成功案例:\n"
    for i, s in enumerate(successes, 1):
        sig = s.get("signal", "")
        trig = s.get("signal_triggers", {})
        logic = s.get("logic", {})
        prompt += (f"  {i}. 赛道:{s['sectors']} | 基金:{s['fund']} | "
                   f"大盘:{s['regime']} | 结果:{s['outcome']} 说明:{s['note']}\n"
                   f"  推理:{s['reasoning']} | 信号:{sig} | 触发:{trig} | 逻辑:{logic}\n")
    prompt += "\n失败案例:\n"
    for i, f in enumerate(failures, 1):
        sig = f.get("signal", "")
        trig = f.get("signal_triggers", {})
        logic = f.get("logic", {})
        prompt += (f"  {i}. 赛道:{f['sectors']} | 基金:{f['fund']} | "
                   f"大盘:{f['regime']} | 结果:{f['outcome']} 说明:{f['note']}\n"
                   f"  推理:{f['reasoning']} | 信号:{sig} | 触发:{trig} | 逻辑:{logic}\n")
    if neutrals:
        prompt += "\n中性案例（微利/微亏，信号不明朗）:\n"
        for i, n in enumerate(neutrals, 1):
            sig = n.get("signal", "")
            trig = n.get("signal_triggers", {})
            logic = n.get("logic", {})
            prompt += (f"  {i}. 赛道:{n['sectors']} | 基金:{n['fund']} | "
                       f"大盘:{n['regime']} | 结果:{n['outcome']} 说明:{n['note']}\n"
                       f"  推理:{n['reasoning']} | 信号:{sig} | 触发:{trig} | 逻辑:{logic}\n")
    prompt += (
        "\n对比成功和失败案例，提取 3-5 条可操作的洞察。每条洞察应："
        "\n1. 具体到量化条件或模式特征"
        "\n2. 可被推荐/监控模块执行"
        "\n3. 标注洞察类型: sector(赛道选择)/position(基金选择)/timing(时机)/macro(宏观)"
        "\n输出 JSON 数组: [{\"insight\": \"...\", \"type\": \"sector/position/timing/macro\"}]"
    )

    content = _call_llm(prompt, temperature=0.3, max_tokens=1536)
    if content is None:
        logger.warning("LLM 不可用，跳过大分析")
        return []
    try:
        result = json.loads(content)
        if isinstance(result, list):
            return result
        if isinstance(result, dict) and "insight" in result:
            return [result]
    except Exception as e:
        logger.warning("LLM 结果解析失败: %s", str(e)[:120], exc_info=True)
    return []


def _calc_loss(code: str, reco_date: str, exit_date: str) -> float | None:
    with _db_conn() as conn:
        nav_reco = conn.execute(
            "SELECT cum_nav FROM fund_nav WHERE code = ? AND date = ?", (code, reco_date)
        ).fetchone()
        nav_exit = conn.execute(
            "SELECT cum_nav FROM fund_nav WHERE code = ? AND date = ?", (code, exit_date)
        ).fetchone()
    if not nav_reco or not nav_exit or not nav_reco[0] or not nav_exit[0]:
        return None
    return nav_exit[0] / nav_reco[0] - 1.0


def _keywords(text: str) -> set:
    text = re.sub(r"[^\w\u4e00-\u9fff]", " ", text)
    return set(t for t in text.split() if len(t) >= 2)


def _extract_rule(content: str) -> str:
    if not content:
        return ""
    try:
        result = json.loads(content)
        if isinstance(result, dict) and (result.get("rule") or "").strip():
            return result["rule"].strip()
    except (json.JSONDecodeError, ValueError):
        pass
    m = re.search(r'"?rule"?\s*[:：]\s*"?([^"\n}]+?)"?\s*[}\n]', content, re.IGNORECASE)
    if m:
        return m.group(1).strip().strip('"').strip()
    return ""


def _extract_rationale(content: str) -> str:
    try:
        result = json.loads(content)
        if isinstance(result, dict):
            return (result.get("rationale") or "").strip()
    except (json.JSONDecodeError, ValueError):
        pass
    m = re.search(r'"?rationale"?\s*[:：]\s*"?([^"\n}]+?)"?\s*[}\n]', content, re.IGNORECASE)
    return m.group(1).strip().strip('"').strip() if m else ""


def check_conflict(new_rule: str, existing_rules: list) -> bool:
    new_kw = _keywords(new_rule)
    if not new_kw:
        return True
    for er in existing_rules:
        er_kw = _keywords(er)
        if not er_kw:
            continue
        overlap = len(new_kw & er_kw) / len(new_kw | er_kw)
        if overlap > 0.6:
            logger.info("规则重叠度 %.2f: %s", overlap, er[:30])
            return True
    return False


def _bigrams(text: str) -> set:
    return {text[i:i+2] for i in range(len(text)-1)} if len(text) >= 2 else set(text)


def _insight_conflicts(new_insight: str, existing: list) -> bool:
    """用关键词级 Jaccard 判断洞察是否与已有记录重复。"""
    new_kw = _keywords(new_insight)
    if not new_kw:
        return True
    for ei in existing:
        ei_kw = _keywords(ei)
        if not ei_kw:
            continue
        overlap = len(new_kw & ei_kw) / len(new_kw | ei_kw)
        if overlap > 0.5:
            return True
    return False


def _save_insight(insight: dict) -> bool:
    with _db_conn() as conn:
        existing = [r[0] for r in conn.execute(
            "SELECT insight FROM evolution_insights WHERE active = 1"
        ).fetchall()]
        if _insight_conflicts(insight["insight"], existing):
            return False
        conn.execute(
            "INSERT INTO evolution_insights (insight, insight_type, created_date) "
            "VALUES (?, ?, ?)",
            (insight["insight"], insight.get("type", "sector"),
             datetime.now().strftime("%Y-%m-%d")),
        )
    logger.info("新洞察入库: [%s] %s", insight.get("type", "?"), insight["insight"][:60])
    return True


# ── 置信度衰减 ─────────────────────────────────────────────

def _decay_insights() -> int:
    """降低旧洞察置信度，长期无用则标记非活跃。"""
    with _db_conn() as conn:
        rows = conn.execute(
            "SELECT id, confidence, apply_count FROM evolution_insights WHERE active = 1"
        ).fetchall()
        decayed = 0
        for rid, conf, cnt in rows:
            new_conf = float(conf) * 0.95
            active = 1 if new_conf > 0.2 else 0
            conn.execute(
                "UPDATE evolution_insights SET confidence = ?, active = ? WHERE id = ?",
                (new_conf, active, rid),
            )
            decayed += 1
    logger.info("置信度衰减: %d 条洞察已更新", decayed)
    return decayed


# ── 主入口 ─────────────────────────────────────────────────

def _save_self_fix(fix: str) -> None:
    with _db_conn() as conn:
        conn.execute(
            "INSERT INTO evolution_insights (insight, insight_type, created_date) "
            "VALUES (?, 'ranking', ?)",
            (fix, datetime.now().strftime("%Y-%m-%d")),
        )
    logger.info("排分自纠偏: %s", fix[:60])


def run_evolve(month: str | None = None) -> None:
    """进化引擎主入口。"""
    if month is None:
        month = datetime.now().strftime("%Y-%m")

    # 1. 排分自纠偏（每月必跑）
    fixes = _review_ranking_all()
    for fix in fixes:
        _save_self_fix(fix)

    # 2. 月度结算
    _settle_outcomes(month)

    # 3. 批量 LLM 元分析
    successes, failures, neutrals = _collect_cases(month)
    if not successes and not failures and not neutrals:
        logger.info("当月无可分析案例，跳过元分析")
    else:
        insights = _batch_llm_analyze(successes, failures, neutrals)
        added = 0
        for ins in insights:
            if _save_insight(ins):
                added += 1
        logger.info("批量元分析: %d条成功/%d条失败/%d条中性 → 新增 %d 条洞察",
                    len(successes), len(failures), len(neutrals), added)

    # 4. 置信度衰减
    _decay_insights()

    logger.info("进化完成: 结算+元分析+衰减")


if __name__ == "__main__":
    import sys
    m = sys.argv[1] if len(sys.argv) > 1 else None
    run_evolve(m)

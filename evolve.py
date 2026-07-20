"""进化引擎：从亏损 EXIT 交易提取教训，生成硬规则写入 evolution_rules（Phase 4）。

ponytail: 单一真相源为 evolution_rules 表（recommend.py 的 llm_veto 已从此表读规则），
不额外维护 JSON 文件，避免双写不一致。

运行：uv run python evolve.py
"""

import json
import logging
import re
import time
from datetime import datetime

from data_foundation import _get_db, _load_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("evolve")

_LOSS_THRESHOLD = -0.05


# ========== 4.1 亏损交易筛选 ==========

def query_losing_trades(month: str) -> list[dict]:
    """查询当月 EXIT 且实际亏损 < -5% 的交易。

    month 形如 '2026-07'。收益 = exit日cum_nav / 推荐日cum_nav - 1。
    无法计算净值收益的 EXIT 记录跳过（数据缺失不臆造）。
    """
    conn = _get_db()
    rows = conn.execute(
        "SELECT id, recommend_date, exit_date, code, name, buy_reason, sell_reason "
        "FROM recommend_log WHERE status = 'EXIT' AND exit_date LIKE ?",
        (f"{month}%",),
    ).fetchall()
    conn.close()

    trades = []
    for r in rows:
        tid, reco_date, exit_date, code, name, buy_reason, sell_reason = r
        loss = _calc_loss(code, reco_date, exit_date)
        if loss is None:
            continue
        if loss < _LOSS_THRESHOLD:
            trades.append({
                "id": tid,
                "recommend_date": reco_date,
                "exit_date": exit_date,
                "code": code,
                "name": name,
                "buy_reason": buy_reason or "",
                "sell_reason": sell_reason or "",
                "loss": loss,
            })
    logger.info("当月亏损(<-5%%)交易: %d 笔", len(trades))
    return trades


def _calc_loss(code: str, reco_date: str, exit_date: str) -> float | None:
    conn = _get_db()
    nav_reco = conn.execute(
        "SELECT cum_nav FROM fund_nav WHERE code = ? AND date = ?", (code, reco_date)
    ).fetchone()
    nav_exit = conn.execute(
        "SELECT cum_nav FROM fund_nav WHERE code = ? AND date = ?", (code, exit_date)
    ).fetchone()
    conn.close()
    if not nav_reco or not nav_exit or not nav_reco[0] or not nav_exit[0]:
        return None
    return nav_exit[0] / nav_reco[0] - 1.0


# ========== 4.2 LLM 规则生成 ==========

def generate_rule(trade: dict) -> dict | None:
    """调用 LLM 生成一条可操作硬规则。限流/失败时返回 None（保守跳过）。"""
    settings = _load_settings()
    llm_cfg = settings.get("llm", {})
    api_key = llm_cfg.get("api_key", "")
    if not api_key:
        logger.warning("LLM 未配置，跳过规则生成")
        return None
    try:
        from openai import OpenAI
        client = OpenAI(base_url=llm_cfg.get("base_url"), api_key=api_key)
        prompt = (
            "你是一位基金投资策略的系统优化架构师。\n"
            "以下是一笔亏损超过-5%的交易记录：\n"
            f"买入日期: {trade['recommend_date']}\n"
            f"卖出日期: {trade['exit_date']}\n"
            f"买入理由: {trade['buy_reason']}\n"
            f"卖出理由: {trade['sell_reason']}\n"
            f"实际亏损: {trade['loss'] * 100:.2f}%\n\n"
            "请分析亏损根本原因，输出一条具体的、可操作的硬规则用于避免未来类似亏损。"
            "规则应具体到量化条件，避免模糊表述。\n"
            '只输出JSON：{"rule": "具体量化规则描述", "rationale": "为什么能避免类似亏损"}'
        )
        for attempt in range(3):
            try:
                resp = client.chat.completions.create(
                    model=llm_cfg.get("model", "gpt-4o-mini"),
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.3,
                    max_tokens=llm_cfg.get("max_tokens", 2000),
                    response_format={"type": "json_object"},
                )
                content = resp.choices[0].message.content
                rule = _extract_rule(content)
                if not rule:
                    return None
                return {"rule": rule, "rationale": _extract_rationale(content)}
            except Exception as e:
                if "429" in str(e) or "RateLimit" in type(e).__name__:
                    time.sleep(2 ** attempt * 2)
                    continue
                logger.error("规则生成失败: %s", e)
                return None
        logger.error("规则生成限流重试耗尽，跳过")
        return None
    except Exception as e:
        logger.error("规则生成异常: %s", e)
        return None


# ========== 4.3 冲突检查 ==========

def _keywords(text: str) -> set:
    # 朴素关键词：去标点后按2字以上中文片段 + 英文单词
    text = re.sub(r"[^\w\u4e00-\u9fff]", " ", text)
    toks = [t for t in text.split() if len(t) >= 2]
    return set(toks)


def _extract_rule(content: str) -> str:
    """从 LLM 输出提取 rule 文本：先试 JSON，失败则用正则兜底（兼容截断/噪声）。"""
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
            logger.info("新规则与已有规则重叠度 %.2f，视为重复: %s", overlap, er[:30])
            return True
    return False


# ========== 4.4 规则追加 ==========

def append_rule(rule: dict, source_trade_id: int) -> None:
    conn = _get_db()
    today = datetime.now().strftime("%Y-%m-%d")
    # 生成 id：R{date}_{seq}
    seq = conn.execute(
        "SELECT COUNT(*) FROM evolution_rules WHERE created_date = ?", (today,)
    ).fetchone()[0] + 1
    rid = f"R{today.replace('-', '')}_{seq:03d}"
    conn.execute(
        "INSERT INTO evolution_rules (id, rule, source_trade_id, created_date, active) "
        "VALUES (?, ?, ?, ?, 1)",
        (rid, rule["rule"], source_trade_id, today),
    )
    conn.commit()
    conn.close()
    logger.info("规则已追加: %s | %s", rid, rule["rule"][:50])


# ========== 4.5 主入口 ==========

def run_evolve(month: str | None = None) -> None:
    """进化引擎主入口：筛选亏损交易 → LLM 生成规则 → 冲突检查 → 入库。"""
    if month is None:
        month = datetime.now().strftime("%Y-%m")
    trades = query_losing_trades(month)
    if not trades:
        logger.info("当月无亏损交易，无需进化")
        return

    conn = _get_db()
    existing = [r[0] for r in conn.execute(
        "SELECT rule FROM evolution_rules WHERE active = 1"
    ).fetchall()]
    conn.close()

    added = 0
    for t in trades:
        rule = generate_rule(t)
        if rule is None:
            continue
        if check_conflict(rule["rule"], existing):
            continue
        append_rule(rule, t["id"])
        existing.append(rule["rule"])
        added += 1
    logger.info("进化完成: 处理 %d 笔亏损交易, 新增规则 %d 条", len(trades), added)


if __name__ == "__main__":
    import sys
    m = sys.argv[1] if len(sys.argv) > 1 else None
    run_evolve(m)

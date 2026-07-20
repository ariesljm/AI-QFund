"""虚拟池监控引擎：三道防线扫描 HOLD 基金，触发 EXIT 平仓（Phase 3）。

防线1 追踪止损：highest_nav - current_nav > 2 × ATR(14)
防线2 风格漂移：买入时RBSA第一大行业权重 - 当前 > 15%
防线3 LLM逻辑证伪：买入逻辑链被当日新闻证伪

运行：uv run python monitor.py
"""

import json
import logging
import sqlite3
import time
from datetime import datetime

import numpy as np

from data_foundation import _get_db, _load_settings
from recommend import get_macro_summary

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("monitor")

_DRIFT_THRESHOLD = 0.15
_ATR_MULTIPLE = 2.0


def _nav_since(code: str, since_date: str) -> list[float]:
    """返回该基金自推荐日（含）起的累计净值序列（升序）。"""
    conn = _get_db()
    rows = conn.execute(
        "SELECT cum_nav FROM fund_nav WHERE code = ? AND date >= ? ORDER BY date ASC",
        (code, since_date),
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def update_highest_nav(code: str, since_date: str) -> float:
    """返回买入（推荐）后至今的最高累计净值。"""
    navs = _nav_since(code, since_date)
    return float(max(navs)) if navs else 0.0


def calc_atr(navs: list[float], period: int = 14) -> float:
    """计算 ATR(14)，以累计净值序列近似（无高低价，用相邻波动代理 TR）。

    ponytail: 真实 ATR 需每日高低收，这里只有累计净值，用 |nav_t - nav_{t-1}|
    作为单日波幅近似真实波幅，取最近 period 日均值。
    """
    if len(navs) < 2:
        return 0.0
    tr = [abs(navs[i] - navs[i - 1]) for i in range(1, len(navs))]
    if len(tr) < period:
        return float(np.mean(tr)) if tr else 0.0
    return float(np.mean(tr[-period:]))


def check_trailing_stop(code: str, highest_nav: float, atr: float) -> tuple[bool, str]:
    """第一防线：追踪止损。返回 (是否触发, 说明)。"""
    navs = _nav_since(code, _reco_date_of(code))
    if not navs:
        return False, ""
    current = navs[-1]
    if highest_nav <= 0 or atr <= 0:
        return False, ""
    if highest_nav - current > _ATR_MULTIPLE * atr:
        return True, (
            f"追踪止损触发: 最高{highest_nav:.4f} - 当前{current:.4f}"
            f"={highest_nav - current:.4f} > 2×ATR({atr:.4f})"
        )
    return False, ""


def _reco_date_of(code: str) -> str:
    conn = _get_db()
    row = conn.execute(
        "SELECT recommend_date FROM recommend_log WHERE code = ? AND status = 'HOLD' "
        "ORDER BY id DESC LIMIT 1", (code,)
    ).fetchone()
    conn.close()
    return row[0] if row else ""


def check_style_drift(code: str) -> tuple[bool, str]:
    """第二防线：RBSA 风格漂移。买入时第一大行业权重 - 当前 > 15% 触发。"""
    conn = _get_db()
    reco_date = _reco_date_of(code)
    # 买入时权重：推荐日那一行 fund_features 的 rbsa_weight_1
    init_row = conn.execute(
        "SELECT rbsa_weight_1 FROM fund_features WHERE code = ? AND date = ?",
        (code, reco_date),
    ).fetchone()
    # 当前权重：最新一行
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
            f"风格漂移触发: 买入权重{init_w:.2f} - 当前{cur_w:.2f}"
            f"={drop:.2f} > 阈值{_DRIFT_THRESHOLD}"
        )
    return False, ""


def check_logic_falsification(code: str, buy_reason: str, news: str) -> tuple[bool, str]:
    """第三防线：LLM 逻辑证伪。返回 (是否触发(逻辑断裂), 说明)。

    LLM 不可用时（限流/未配置）保守返回不触发，避免误平。
    """
    settings = _load_settings()
    llm_cfg = settings.get("llm", {})
    api_key = llm_cfg.get("api_key", "")
    if not api_key:
        return False, "LLM 未配置，跳过逻辑证伪"
    try:
        from openai import OpenAI
        client = OpenAI(base_url=llm_cfg.get("base_url"), api_key=api_key)
        prompt = (
            "你是一位严格的基金投研审核员。下面是一条基金买入逻辑，"
            "以及今日财经新闻摘要。请判断该买入逻辑链是否被新闻证伪。\n\n"
            f"买入逻辑: {buy_reason}\n\n"
            f"今日新闻摘要: {news}\n\n"
            "只输出JSON：{\"verdict\": \"维持\" 或 \"断裂\", \"reason\": \"简要说明\"}"
        )
        for attempt in range(3):
            try:
                resp = client.chat.completions.create(
                    model=llm_cfg.get("model", "gpt-4o-mini"),
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1,
                    max_tokens=512,
                    response_format={"type": "json_object"},
                )
                content = resp.choices[0].message.content
                result = json.loads(content)
                verdict = str(result.get("verdict", "")).strip()
                if "断裂" in verdict:
                    return True, f"LLM逻辑证伪: {result.get('reason', '')}"
                return False, f"LLM逻辑维持: {result.get('reason', '')}"
            except Exception as e:
                if "429" in str(e) or "RateLimit" in type(e).__name__:
                    time.sleep(2 ** attempt * 2)
                    continue
                return False, f"LLM证伪调用失败({e})，保守跳过"
        return False, "LLM证伪限流重试耗尽，保守跳过"
    except Exception as e:
        return False, f"LLM证伪异常({e})，保守跳过"


def _exit_position(code: str, sell_reason: str) -> None:
    conn = _get_db()
    today = datetime.now().strftime("%Y-%m-%d")
    conn.execute(
        "UPDATE recommend_log SET status = 'EXIT', sell_reason = ?, exit_date = ? "
        "WHERE code = ? AND status = 'HOLD'",
        (sell_reason, today, code),
    )
    conn.commit()
    conn.close()
    logger.info("平仓 EXIT: %s | %s", code, sell_reason)


def run_monitor() -> None:
    """遍历所有 HOLD 基金，执行三道防线，触发则平仓入库。"""
    conn = _get_db()
    rows = conn.execute(
        "SELECT code, name, recommend_date, buy_reason FROM recommend_log "
        "WHERE status = 'HOLD'"
    ).fetchall()
    conn.close()
    if not rows:
        logger.info("无 HOLD 持仓，监控结束")
        return

    date_str = datetime.now().strftime("%Y-%m-%d")
    news = get_macro_summary(date_str)

    exited = 0
    for code, name, reco_date, buy_reason in rows:
        logger.info("=== 监控 %s %s ===", code, name)
        # 防线1：追踪止损
        highest = update_highest_nav(code, reco_date)
        navs = _nav_since(code, reco_date)
        atr = calc_atr(navs)
        triggered, reason = check_trailing_stop(code, highest, atr)
        if not triggered:
            # 防线2：风格漂移
            triggered, reason = check_style_drift(code)
        if not triggered:
            # 防线3：LLM 逻辑证伪
            triggered, reason = check_logic_falsification(code, buy_reason or "", news.get("news", ""))
        if triggered:
            _exit_position(code, reason)
            exited += 1
        else:
            logger.info("  %s 三道防线均未触发，继续持有", code)

    logger.info("监控完成: 扫描 %d 只, 平仓 %d 只", len(rows), exited)


if __name__ == "__main__":
    run_monitor()

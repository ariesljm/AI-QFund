"""LLM 决策审计 seam：llm_audit 表读写（ADR-0001 决策域写唯一 seam 的技术审计子集）。
"""

import json as _json
from datetime import datetime

from app.database import db_conn


def insert_llm_audit(caller: str, prompt: str, raw_output: str, parsed_json: str | None,
                  duration_ms: float, tokens: int, ok: bool, max_rows: int = 5000) -> None:
    """写入 LLM 决策审计（P0-3，ADR-0001 决策域表写唯一 seam）。

    含 prompt 快照/原始输出/解析结果 + 滚动保留（最多 max_rows 行）。
    调用侧保留 try/except 容错——审计失败不阻断主流程（技术记录语义不丢）。
    """
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO llm_audit (ts, caller, prompt_hash, prompt_preview, raw_output, "
            "parsed_result, duration_ms, tokens, ok) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), caller or "",
             f"{len(prompt)}:{prompt[:64]}", prompt.strip().replace(chr(10), " ")[:200],
             (raw_output or "")[:4000], parsed_json, int(duration_ms), int(tokens or 0),
             1 if ok else 0),
        )
        conn.execute("DELETE FROM llm_audit WHERE id NOT IN "
                     "(SELECT id FROM llm_audit ORDER BY id DESC LIMIT ?)", (max_rows,))


def get_recent_audits(limit: int = 10) -> list[dict]:
    """最近 LLM 审计记录（新→旧，票 23 审计可见性：排雷过程可追溯）。

    每条含 ts/caller/ok/parsed_result（JSON 字符串，调用方解析 verdict/risk_score/\n    veto_reasons）/prompt_preview/raw_output。无记录返回空列表。
    """
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT ts, caller, ok, parsed_result, prompt_preview, raw_output "
            "FROM llm_audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for ts, caller, ok, parsed, preview, raw in rows:
        verdict = risk = None
        if parsed:
            try:
                p = _json.loads(parsed)
                verdict = p.get("audit_verdict") or p.get("logic_verdict")
                risk = p.get("risk_score")
            except (TypeError, ValueError):
                pass
        out.append({"ts": ts, "caller": caller, "ok": bool(ok),
                    "parsed": parsed, "verdict": verdict, "risk_score": risk,
                    "prompt_preview": preview, "raw_output": raw})
    return out


__all__ = ["insert_llm_audit", "get_recent_audits"]

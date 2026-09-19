"""知识库持久化 seam（票 17）：Bad/Good-Case 案例表 + 活跃案例检索供 few-shot 回流。

纯函数核心在 app/engine/knowledge.py（make_case/retrieve_cases/assemble_few_shot）；
本模块只管落库与检索，供 prompts.audit_user_prompt 注入活跃案例（ADR-0009 只增不改）。

触发落库由 engine/evolve.py（异常回撤>8% 或 EXIT）调用 save_case；
prompts 注入调 get_active_cases → engine.retrieve_cases → engine.assemble_few_shot。
"""

import json
from datetime import datetime

from app.database import db_conn

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS knowledge_cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_type TEXT NOT NULL,           -- bad / good
    decision_date TEXT NOT NULL,       -- 推荐决策日
    fund TEXT NOT NULL,                -- 基金代码
    industry TEXT,                     -- RBSA 第一行业（检索条件）
    audit_json TEXT,                   -- 当日审计 JSON（出处，只增不改）
    outcome_json TEXT,                 -- 实际结果（40日收益/异常/是否EXIT）
    created_at TEXT DEFAULT (datetime('now'))
)
"""

# 活跃案例注入预算（与 engine/knowledge.FEW_SHOT_MAX_CHARS 对齐：注入条数上限）
ACTIVE_CASE_LIMIT = 10


def _ensure_table() -> None:
    with db_conn() as conn:
        conn.execute(_CREATE_TABLE)
        conn.commit()


def save_case(case: dict) -> None:
    """落库一个案例（ADR-0009 只增不改：不 UPDATE/DELETE）。

    case 结构由 engine.knowledge.make_case 产出（type/decision_date/fund/industry/audit/outcome）。
    """
    _ensure_table()
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO knowledge_cases (case_type, decision_date, fund, industry, audit_json, outcome_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                case.get("type", "bad"),
                case.get("decision_date", ""),
                case.get("fund", ""),
                case.get("industry", ""),
                json.dumps(case.get("audit") or {}, ensure_ascii=False),
                json.dumps(case.get("outcome") or {}, ensure_ascii=False),
            ),
        )
        conn.commit()


def get_active_cases(limit: int = ACTIVE_CASE_LIMIT) -> list[dict]:
    """活跃案例（最近 limit 条，bad+good 混合，供 few-shot 注入）。

    返回 engine.retrieve_cases 可消费的结构（audit/outcome 反序列化回 dict）。
    案例不足时返回 []——prompt 注入空串，等价于无回流（不破坏现有审计）。
    """
    _ensure_table()
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT case_type, decision_date, fund, industry, audit_json, outcome_json "
            "FROM knowledge_cases ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        try:
            audit = json.loads(r[4]) if r[4] else {}
            outcome = json.loads(r[5]) if r[5] else {}
        except json.JSONDecodeError:
            audit, outcome = {}, {}
        out.append({"type": r[0], "decision_date": r[1], "fund": r[2],
                    "industry": r[3], "audit": audit, "outcome": outcome})
    return out


__all__ = ["save_case", "get_active_cases"]

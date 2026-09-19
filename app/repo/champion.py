"""影子闸门台账（票 19）：champion_challenger 表 + 版本/windows/切换记录。

判定纯函数在 app/engine/gate.py（gate_decision）；本模块只做台账持久化：
每次影子比较记一行（champion/challenger 版本、窗口 IR、regime、p 值、pass/fail、动作），
供追溯"哪个 Challenger 在何时因何证据上线/被拒/回滚"。
"""

import json

from app.database import db_conn

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS champion_challenger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    champion_version TEXT NOT NULL,
    challenger_version TEXT NOT NULL,
    champion_windows TEXT,     -- JSON: 非重叠窗口 IR 序列
    challenger_windows TEXT,   -- JSON
    regime_results TEXT,       -- JSON: [{regime, challenger, champion}]
    p_value REAL,
    passed INTEGER,            -- 0/1
    reasons TEXT,              -- JSON 或分隔文本
    action TEXT,               -- promoted / rejected / rollback
    created_at TEXT DEFAULT (datetime('now'))
)
"""


def _ensure_table() -> None:
    with db_conn() as conn:
        conn.execute(_CREATE_TABLE)
        conn.commit()


def record_decision(champion_version: str, challenger_version: str,
                    champion_windows: list[float], challenger_windows: list[float],
                    regime_results: list[dict], decision: dict,
                    action: str) -> None:
    """落一条影子闸门台账（判定结果 + 动作）。"""
    _ensure_table()
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO champion_challenger "
            "(champion_version, challenger_version, champion_windows, challenger_windows, "
            " regime_results, p_value, passed, reasons, action) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                champion_version, challenger_version,
                json.dumps(champion_windows),
                json.dumps(challenger_windows),
                json.dumps(regime_results, ensure_ascii=False),
                decision.get("p"),
                1 if decision.get("passed") else 0,
                json.dumps(decision.get("reasons", []), ensure_ascii=False),
                action,
            ),
        )
        conn.commit()


def get_history(limit: int = 20) -> list[dict]:
    """最近台账（追溯影子闸门决策）。"""
    _ensure_table()
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT id, champion_version, challenger_version, p_value, passed, "
            "action, created_at FROM champion_challenger ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [{"id": r[0], "champion_version": r[1], "challenger_version": r[2],
             "p_value": r[3], "passed": bool(r[4]), "action": r[5], "created_at": r[6]}
            for r in rows]


__all__ = ["record_decision", "get_history"]

"""2.0 推荐产出链路 seam：screen_candidates（Top30 候选池）+ recommend_v2（最终 Top5）。
"""

import json as _json

from app.database import db_conn


def save_screen_candidates(date: str, rows: list[dict]) -> int:
    """Top30 候选池落库（票 11）：(date, code, score, feature_snapshot JSON)。

    同日重复跑 → INSERT OR REPLACE（幂等）；特征快照供审计与复盘。
    """
    if not rows:
        return 0
    with db_conn() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO screen_candidates "
            "(date, code, score, feature_snapshot) VALUES (?, ?, ?, ?)",
            [(date, r["code"], r["score"],
              _json.dumps(r.get("features") or {}, ensure_ascii=False)) for r in rows])
        conn.commit()
    return len(rows)


def get_screen_candidates(date: str) -> list[dict]:
    """读取某日 Top30 候选池（按 score 降序），用于审计/复盘/Web。"""
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT code, score, feature_snapshot FROM screen_candidates "
            "WHERE date = ? ORDER BY score DESC", (date,)).fetchall()
    out = []
    for code, score, snap in rows:
        try:
            feat = _json.loads(snap) if snap else {}
        except (TypeError, ValueError):
            feat = {}
        out.append({"code": code, "score": score, "features": feat})
    return out


def save_recommend_v2(date: str, rows: list[dict]) -> int:
    """2.0 最终推荐 Top5 落库（票 11：date/code/final_score/audit_json）。幂等 REPLACE，同日唯一：重跑覆盖旧 Top5。"""
    if not rows:
        return 0
    with db_conn() as conn:
        conn.execute("DELETE FROM recommend_v2 WHERE date = ?", (date,))
        conn.executemany(
            "INSERT OR REPLACE INTO recommend_v2 (date, code, final_score, audit_json) "
            "VALUES (?, ?, ?, ?)",
            [(date, r["code"], r["final_score"],
              _json.dumps(r.get("audit") or {}, ensure_ascii=False)) for r in rows])
        conn.commit()
    return len(rows)


def get_recommend_v2_codes(limit: int = 50) -> list[str]:
    """2.0 推荐对象清单（全部日期去重，监控状态机分母；票 23 状态机视图）。"""
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT code FROM recommend_v2 ORDER BY code LIMIT ?",
            (limit,)).fetchall()
    return [r[0] for r in rows]


def get_recommend_v2(date: str) -> list[dict]:
    """读取某日 2.0 推荐 Top5（final_score 降序）。"""
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT code, final_score, audit_json FROM recommend_v2 "
            "WHERE date = ? ORDER BY final_score DESC", (date,)).fetchall()
    out = []
    for code, score, audit in rows:
        try:
            a = _json.loads(audit) if audit else {}
        except (TypeError, ValueError):
            a = {}
        out.append({"code": code, "final_score": score, "audit": a})
    return out


__all__ = ["save_screen_candidates", "get_screen_candidates", "save_recommend_v2", "get_recommend_v2_codes", "get_recommend_v2"]

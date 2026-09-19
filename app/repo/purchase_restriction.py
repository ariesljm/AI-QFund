"""申购状态 seam：purchase_restrictions 维护清单读写。
"""

from app.database import db_conn


def get_purchase_status(code: str) -> str | None:
    """基金申购状态（T10）：normal/limited/suspended；无记录返回 None（未知）。

    降级方案：purchase_restrictions 为维护者手工维护清单（东财接口无稳定字段时），
    候选装配时查询并标注，未知状态不阻塞推荐。
    """
    with db_conn() as conn:
        row = conn.execute(
            "SELECT status FROM purchase_restrictions WHERE code = ?", (code,)).fetchone()
    return row[0] if row else None


def get_purchase_statuses(codes: list[str]) -> dict[str, str]:
    """候选批量申购状态：{code: status}（T10 批量原语，N+1 收敛）。

    purchase_restrictions 为维护者手工维护清单；未知 code 不在返回 dict 中（调用方按 unknown 处理）。
    """
    if not codes:
        return {}
    ph = ",".join("?" for _ in codes)
    with db_conn() as conn:
        rows = conn.execute(f"SELECT code, status FROM purchase_restrictions "
                            f"WHERE code IN ({ph})", codes).fetchall()
    return dict(rows)


def save_purchase_restriction(code: str, status: str, note: str = "",
                              daily_limit: float | None = None) -> None:
    """写入/更新基金申购状态（幂等，维护清单导入/数据源回填用）。

    daily_limit 为单日申购上限（元），None = 无限购/未知（票 06）。
    """
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO purchase_restrictions (code, status, note, daily_limit) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(code) DO UPDATE SET status=excluded.status, "
            "note=excluded.note, daily_limit=excluded.daily_limit, "
            "updated_at=datetime('now')",
            (code, status, note, daily_limit))
        conn.commit()


__all__ = ["get_purchase_status", "get_purchase_statuses", "save_purchase_restriction"]

"""结算账本读写（决策域）。

表结构见 `data/schema.sql` 的 `settlement_ledger`；口径与三统计量见 `app.settlement`。
本模块只负责库 I/O，不做任何口径判断。

唯一约束 `(reco_date, code, benchmark_version)` 使重复结算幂等：同一推荐在同一标尺
版本下只有一行，重跑覆盖而不是追加，因此"窗口补齐后重跑"是安全的。
"""

from collections.abc import Iterable

from app.database import db_conn
from app.settlement import Settlement, as_row

_COLUMNS = (
    "reco_date", "code", "benchmark_version", "settle_date",
    "abs_return", "excess_return", "peer_n", "max_drawdown", "confidence",
)
_KEYS = ("reco_date", "code", "benchmark_version")


def insert(rows: Iterable[Settlement]) -> int:
    """写入结算记录；同一 (推荐日, 基金, 标尺版本) 覆盖更新而非重复插入。返回条数。"""
    payload = [as_row(r) for r in rows]
    if not payload:
        return 0
    updates = ", ".join(f"{c}=excluded.{c}" for c in _COLUMNS if c not in _KEYS)
    sql = (
        f"INSERT INTO settlement_ledger ({', '.join(_COLUMNS)}) "
        f"VALUES ({', '.join('?' * len(_COLUMNS))}) "
        f"ON CONFLICT ({', '.join(_KEYS)}) DO UPDATE SET {updates}"
    )
    with db_conn() as conn:
        conn.executemany(sql, payload)
        conn.commit()
    return len(payload)


def list_rows(reco_date: str | None = None, code: str | None = None,
              benchmark_version: str | None = None) -> list[dict]:
    """读取结算记录（dict 行，交给 `app.settlement.load` 转换后聚合）。"""
    where: list[str] = []
    params: list[object] = []
    for col, val in (("reco_date", reco_date), ("code", code),
                     ("benchmark_version", benchmark_version)):
        if val is not None:
            where.append(f"{col} = ?")
            params.append(val)
    sql = "SELECT " + ", ".join(_COLUMNS) + " FROM settlement_ledger"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY reco_date, code"
    with db_conn() as conn:
        cur = conn.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]

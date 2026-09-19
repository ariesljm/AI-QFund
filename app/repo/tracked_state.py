"""状态机闭环 seam：tracked_states（HOLD/WATCH/EXIT）+ signal_outcomes（校准层结算）+ monitor_events（1.x 残留读，dashboard 兼容）。
"""

from app.database import db_conn


def get_tracked_state(object_type: str, object_id: str) -> dict | None:
    """三级状态机当前状态（票 15）：{state, date, signals_json}；无记录 → None。"""
    with db_conn() as conn:
        row = conn.execute(
            "SELECT state, date, signals_json FROM tracked_states "
            "WHERE object_type = ? AND object_id = ?", (object_type, object_id)).fetchone()
    if not row:
        return None
    return {"state": row[0], "date": row[1], "signals_json": row[2]}


def save_tracked_state(object_type: str, object_id: str, state: str,
                       date: str, signals_json: str | None = None) -> None:
    """写入/更新状态机当前状态（幂等：同对象覆盖，EXIT 不可逆由转移函数保证）。"""
    with db_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO tracked_states "
            "(object_type, object_id, state, date, signals_json) VALUES (?, ?, ?, ?, ?)",
            (object_type, object_id, state, date, signals_json))
        conn.commit()


def get_all_tracked_states(limit: int = 50) -> list[dict]:
    """全部状态机跟踪对象（票 23 状态机视图）：{object_type, object_id, state, date}。"""
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT object_type, object_id, state, date FROM tracked_states "
            "ORDER BY date DESC LIMIT ?", (limit,)).fetchall()
    return [{"object_type": r[0], "object_id": r[1], "state": r[2], "date": r[3]}
            for r in rows]


def _ensure_signal_outcomes_table() -> None:
    """建表兑底（schema.sql 主建，此处 IF NOT EXISTS 兑底 + 旧表补 fund 列）。"""
    with db_conn() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS signal_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id TEXT NOT NULL,
            ts TEXT NOT NULL,
            date TEXT,
            fund TEXT,
            outcome INTEGER)""")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(signal_outcomes)")}
        if "fund" not in cols:
            try: conn.execute("ALTER TABLE signal_outcomes ADD COLUMN fund TEXT")
            except Exception: pass
        conn.commit()


def record_signal_trigger(signal_id: str, date: str, fund: str = "") -> None:
    """校准层记账（票 18 决策周期入口）：信号触发记一行（outcome 待结算）。

    fund: 触发该信号的基金代码（settle 时算 40 日超额用；空则无法结算超额）。"""
    _ensure_signal_outcomes_table()
    from datetime import datetime as _dt
    with db_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO signal_outcomes (signal_id, ts, date, fund, outcome) "
            "VALUES (?, ?, ?, ?, NULL)",
            (signal_id, _dt.now().strftime("%Y-%m-%d %H:%M:%S.%f"), date, fund))
        conn.commit()


def settle_signal(signal_id: str, ts: str, hit: bool) -> None:
    """40 日结算：回填 outcome（0/1）。"""
    with db_conn() as conn:
        conn.execute("UPDATE signal_outcomes SET outcome = ? WHERE signal_id = ? AND ts = ?",
                     (1 if hit else 0, signal_id, ts))
        conn.commit()


def get_signal_history(signal_id: str) -> list[bool]:
    """某信号已结算的历史结果（旧→新，末尾=最新；calibration.assess 消费）。"""
    _ensure_signal_outcomes_table()
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT outcome FROM signal_outcomes WHERE signal_id = ? AND outcome IS NOT NULL "
            "ORDER BY ts ASC", (signal_id,)).fetchall()
    return [bool(r[0]) for r in rows]


def get_pending_settlements() -> list[dict]:
    """未结算信号（outcome IS NULL）：供 evolve 按 40 日超额判定命中后 settle。"""
    _ensure_signal_outcomes_table()
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT signal_id, ts, date, fund FROM signal_outcomes WHERE outcome IS NULL"
        ).fetchall()
    return [{"signal_id": r[0], "ts": r[1], "date": r[2], "fund": r[3]} for r in rows]


def get_signal_stats() -> list[dict]:
    """校准层信号命中率统计（票 23 校准曲线数据）：{signal_id, hits, samples, hit_rate}。

    只计已结算（outcome 非 NULL）；无结算记录 → []。按样本量降序。
    """
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT signal_id, SUM(outcome), COUNT(*) FROM signal_outcomes "
            "WHERE outcome IS NOT NULL GROUP BY signal_id ORDER BY COUNT(*) DESC").fetchall()
    return [{"signal_id": r[0], "hits": int(r[1] or 0), "samples": int(r[2]),
             "hit_rate": (int(r[1] or 0) / int(r[2])) if r[2] else None}
            for r in rows]


def save_calibration(signal_id: str, hit_rate: float | None, samples: int,
                     action: str) -> None:
    """校准判定落库（assess 输出有去向，不再只进日志；状态机消费读此表）。"""
    from datetime import datetime as _dt
    with db_conn() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS calibration_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            signal_id TEXT NOT NULL,
            hit_rate REAL,
            samples INTEGER,
            action TEXT)""")
        conn.execute(
            "INSERT INTO calibration_log (ts, signal_id, hit_rate, samples, action) "
            "VALUES (?, ?, ?, ?, ?)",
            (_dt.now().strftime("%Y-%m-%d %H:%M:%S"), signal_id, hit_rate, samples, action))
        conn.commit()


def get_latest_monitor_event(code: str) -> dict | None:
    """持仓基金最新监控事件（结构化行，调用方按键取，不再按位置解包裸元组）。"""
    with db_conn() as conn:
        row = conn.execute('SELECT signal, logic_verdict, sector_risk, holding_risk, detail, date, is_stale FROM monitor_events WHERE code=? ORDER BY date DESC, id DESC LIMIT 1', (code,)).fetchone()
    if not row:
        return None
    keys = ["signal", "logic_verdict", "sector_risk", "holding_risk", "detail", "date", "is_stale"]
    return dict(zip(keys, row, strict=False))


__all__ = ["get_tracked_state", "save_tracked_state", "get_all_tracked_states", "record_signal_trigger", "settle_signal", "get_signal_history", "get_signal_stats", "save_calibration", "get_latest_monitor_event", "get_pending_settlements"]

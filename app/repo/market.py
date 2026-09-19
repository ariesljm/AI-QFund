"""指数行情/大盘状态 底层数据 seam（从 base 拆分：index/regime 域）。"""

from app import domain
from app.database import db_conn


def get_index_close(code: str, date: str | None=None) -> float | None:
    with db_conn() as conn:
        if date:
            row = conn.execute('SELECT close FROM index_daily WHERE code = ? AND date <= ? ORDER BY date DESC LIMIT 1', (code, date)).fetchone()
        else:
            row = conn.execute('SELECT close FROM index_daily WHERE code = ? ORDER BY date DESC LIMIT 1', (code,)).fetchone()
    return row[0] if row else None


def get_index_rows(code: str='sh000300') -> list[tuple]:
    """宽基指数日线行 (date, close, volume)，按日期升序（特征计算/回测共用）。"""
    sql = 'SELECT date, close, volume FROM index_daily WHERE code = ? ORDER BY date ASC'
    with db_conn() as conn:
        return conn.execute(sql, (code,)).fetchall()


def get_index_series(code: str='sh000300', columns: tuple[str, ...]=('date', 'close', 'volume', 'ema60'), since: str | None=None) -> list[tuple]:
    """宽基指数日线序列（按日期升序），供特征/训练/回测/Web 共用。"""
    cols = ', '.join(columns)
    sql = f'SELECT {cols} FROM index_daily WHERE code = ?'
    params: tuple = (code,)
    if since:
        sql += ' AND date >= ?'
        params = (code, since)
    with db_conn() as conn:
        rows = conn.execute(sql + ' ORDER BY date ASC', params).fetchall()
    return rows


def _ema250_latest(closes: list[float]) -> float | None:
    """收盘序列的 EMA250（年线）末值（现算）；数据不足 250 条返回 None。

    与回测 _ema250_of / backtest gate 同口径（k=2/251, adjust=False）；
    市场 regime 判定等长周期口径共用单一来源。
    """
    if len(closes) <= 250:
        return None
    k = 2.0 / 251.0
    ema = closes[0]
    for v in closes[1:]:
        ema = v * k + ema * (1.0 - k)
    return ema


def get_market_regime() -> str:
    """多周期共振牛熊判定（T06）：沪深300 close vs EMA60(短) + EMA250(长，现算)。

    双确认才判 BULL/BEAR，方向矛盾归 NEUTRAL（震荡期不做方向性降权）；
    长周期数据不足时回退单周期（旧逻辑）。大盘状态机单一来源。
    """
    with db_conn() as conn:
        row = conn.execute(
            "SELECT close, ema60 FROM index_daily WHERE code='sh000300' "
            "AND close IS NOT NULL AND ema60 IS NOT NULL ORDER BY date DESC LIMIT 1"
        ).fetchone()
        if not row:
            return domain.REGIME_NEUTRAL
        close, ema60 = row[0], row[1]
        # 长周期 EMA250（年线）：取历史收盘现算（index_daily 无长均线列）
        full = [r[0] for r in conn.execute(
            "SELECT close FROM index_daily WHERE code='sh000300' AND close IS NOT NULL "
            "ORDER BY date").fetchall()]
        ema250 = _ema250_latest(full)
        if ema250 is not None:
            return domain.regime_from_multi_timeframe(close, ema60, ema250)
        # 长周期数据不足 → 回退单周期（旧逻辑）
        return domain.regime_from_close_ema60(close, ema60)

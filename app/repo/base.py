"""底层数据 seam：fund/nav/index/holdings/features/meta 等可重建的底层数据只读与写入。"""

from datetime import datetime

from app import domain
from app.database import db_conn, meta_get, meta_set
from app.repo import meta_keys as META
from app.utils.log import get_logger

logger = get_logger("repo")


# 模型特征列清单（fund_features 表列名，单一来源；repo 拼 SQL / 特征计算 / 回测均从此导入）
FEATURE_COLS = domain.FEATURE_COLS

# 市场状态列（R1 绝对收益目标配套）：不进 fund_features 表，训练/打分时从指数现算注入
MARKET_COLS = domain.MARKET_COLS

# 推荐模型前向预测窗口（交易日），训练与回测共用（领域常量单一来源）
FORWARD_WINDOW = domain.FORWARD_DAYS
def _latest_feature_join() -> str:
    """每基金仅取最新特征快照的 JOIN 子句。

    fund_features 存全部历史供训练；排序/反事实若混入历史行，旧快照可能凭更高
    momentum/combo 胜出——推荐基于过期特征，且 entry score 与监控当日重打分不一致，
    会触发 R2d 推荐当天立即 BUY_MORE（017787 事故根因）。
    """
    return ("JOIN (SELECT code, MAX(date) AS date FROM fund_features GROUP BY code) lat "
            "ON lat.code = ff.code AND lat.date = ff.date")


def get_all_ranking_rows() -> list[dict]:
    """全市场可投基金最新特征快照（推荐降级路径用），每基金一行。"""
    feat_cols = ', '.join('ff.' + c for c in FEATURE_COLS)
    with db_conn() as conn:
        rows = conn.execute(f"SELECT ff.code, fb.name, ff.regime, ff.rbsa_industry_1, ff.rbsa_weight_1, lat.date AS feature_date, {feat_cols} FROM fund_features ff {_latest_feature_join()} JOIN fund_basic fb ON fb.code = ff.code WHERE fb.is_buyable = 1 AND ff.rbsa_industry_1 IS NOT NULL AND ff.rbsa_industry_1 != ''").fetchall()
    names = ['code', 'name', 'regime', 'rbsa_industry_1', 'rbsa_weight_1', 'feature_date'] + FEATURE_COLS
    return [dict(zip(names, r, strict=False)) for r in rows]

def get_available_sectors() -> list[str]:
    """可用赛道清单（真实 RBSA 行业，排除空值与兜底'其他'）。

    '其他'是行业映射缺失时的兜底值，不是可投行业；若混入清单，LLM 可能
    从['其他']里选中它，导致无真实行业却降级推荐基金。
    """
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT rbsa_industry_1 FROM fund_features "
            "WHERE rbsa_industry_1 IS NOT NULL AND rbsa_industry_1 NOT IN ('', '其他') "
            "UNION SELECT DISTINCT rbsa_industry_2 FROM fund_features "
            "WHERE rbsa_industry_2 IS NOT NULL AND rbsa_industry_2 NOT IN ('', '其他') "
            "UNION SELECT DISTINCT rbsa_industry_3 FROM fund_features "
            "WHERE rbsa_industry_3 IS NOT NULL AND rbsa_industry_3 NOT IN ('', '其他')"
        ).fetchall()
    return [r[0] for r in rows]

def get_buyable_codes() -> list[str]:
    """可投基金代码全集（持仓/净值下载、特征批量计算共用；buyable 查询单一归属）。"""
    with db_conn() as conn:
        rows = conn.execute('SELECT code FROM fund_basic WHERE is_buyable = 1').fetchall()
    return [r[0] for r in rows]

def get_buyable_feature_stats() -> list[tuple[str, float | None, float | None, float | None]]:
    """可投基金核心特征快照（进化引擎排分自纠偏用）。

    只取最新特征日期：fund_features 每基金保留 250 行历史快照，混入旧快照会
    稀释动量/相关性信号导致误报（8-08 数据停摆期间曾报 corr=-1.000 / spread=1.0pp，
    最新日期口径下为 +0.08 / 53pp）。"""
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT ff.code, ff.momentum_20d, ff.hurst_60d, ff.calmar "
            "FROM fund_features ff JOIN fund_basic fb ON fb.code=ff.code "
            "WHERE fb.is_buyable=1 "
            "AND ff.date = (SELECT MAX(date) FROM fund_features)").fetchall()
    return list(rows)

def get_codes_missing_rbsa() -> list[str]:
    """RBSA 行业暴露缺失的基金（强制重算 RBSA 用）。"""
    with db_conn() as conn:
        rows = conn.execute("SELECT code FROM fund_features WHERE (rbsa_industry_1 IS NULL OR rbsa_industry_1 = '' OR rbsa_industry_1 = '其他')   OR (rbsa_industry_2 IS NULL OR rbsa_industry_2 = '')   OR (rbsa_industry_3 IS NULL OR rbsa_industry_3 = '')").fetchall()
    return [r[0] for r in rows]

def get_feature_codes_before(date: str) -> list[str]:
    """特征日期早于指定日期的基金（行业映射更新后强制重算用）。"""
    with db_conn() as conn:
        rows = conn.execute('SELECT code FROM fund_features WHERE date < ?', (date,)).fetchall()
    return [r[0] for r in rows]

def get_feature_dates_map() -> dict[str, str]:
    """code → 最近特征日期 映射（批量计算跳过判断用）。"""
    with db_conn() as conn:
        rows = conn.execute('SELECT code, date FROM fund_features').fetchall()
    return dict(rows)

def get_fund_name(code: str) -> str | None:
    with db_conn() as conn:
        row = conn.execute('SELECT name FROM fund_basic WHERE code = ?', (code,)).fetchone()
    return row[0] if row else None

def get_fund_pool_stats() -> tuple[int, list[dict]]:
    with db_conn() as conn:
        total = conn.execute('SELECT COUNT(*) FROM fund_basic WHERE is_buyable = 1').fetchone()[0]
        by_type = conn.execute('SELECT type, COUNT(*) FROM fund_basic WHERE is_buyable = 1 GROUP BY type ORDER BY COUNT(*) DESC').fetchall()
    return (total, [{'type': t[0] or '其他', 'count': t[1]} for t in by_type])

def get_holdings(code: str, limit: int=10) -> list[dict]:
    with db_conn() as conn:
        rows = conn.execute('SELECT h.stock_code, h.stock_name, h.weight, i.industry_name FROM fund_holdings h LEFT JOIN stock_industry_map i ON h.stock_code = i.stock_code WHERE h.code = ? AND h.report_date = (  SELECT MAX(report_date) FROM fund_holdings WHERE code = ?) ORDER BY h.weight DESC LIMIT ?', (code, code, limit)).fetchall()
    return [{'stock_code': r[0], 'stock_name': r[1], 'weight': r[2], 'industry': r[3] or ''} for r in rows]

def get_holdings_summaries(codes: list[str], limit: int = 5) -> dict[str, dict]:
    """候选批量持仓素材：{code: {"holdings": [top-N], "report_date": str|None}}。

    推荐 LLM 终选素材装配 N+1 收敛（批量先例 get_candidate_nav_summaries 同风格）：
    一次返回全部候选的最新报告期 + 该期前 limit 大持仓；无记录报告期/持仓为空。
    """
    if not codes:
        return {}
    ph = ",".join("?" for _ in codes)
    out = {c: {"holdings": [], "report_date": None} for c in codes}
    with db_conn() as conn:
        latest = dict(conn.execute(
            f"SELECT code, MAX(report_date) FROM fund_holdings "
            f"WHERE code IN ({ph}) GROUP BY code", codes).fetchall())
        for c in codes:
            if c in latest:
                out[c]["report_date"] = latest[c]
        pairs = [x for c in codes if c in latest for x in (c, latest[c])]
        if pairs:
            pair_ph = ",".join("(?,?)" for _ in pairs[::2])
            for code, sc, sn, w, ind in conn.execute(
                f"SELECT h.code, h.stock_code, h.stock_name, h.weight, i.industry_name "
                f"FROM fund_holdings h LEFT JOIN stock_industry_map i "
                f"ON h.stock_code = i.stock_code "
                f"WHERE (h.code, h.report_date) IN ({pair_ph}) "
                f"ORDER BY h.code, h.weight DESC", pairs).fetchall():
                lst = out[code]["holdings"]
                if len(lst) < limit:
                    lst.append({"stock_code": sc, "stock_name": sn,
                                "weight": w, "industry": ind or ""})
    return out

def get_holdings_at_report(code: str, report_date: str, limit: int=10) -> list[dict]:
    """按报告期取持仓（R4 对称切片：锚点报告期前 N 大，与最新前 N 大对称比较）。

    历史报告期数据随季报追加保留；无该报告期数据返回空列表（调用方回退快照）。
    """
    with db_conn() as conn:
        rows = conn.execute(
            'SELECT h.stock_code, h.stock_name, h.weight, i.industry_name '
            'FROM fund_holdings h LEFT JOIN stock_industry_map i ON h.stock_code = i.stock_code '
            'WHERE h.code = ? AND h.report_date = ? ORDER BY h.weight DESC LIMIT ?',
            (code, report_date, limit)).fetchall()
    return [{'stock_code': r[0], 'stock_name': r[1], 'weight': r[2], 'industry': r[3] or ''} for r in rows]

def get_index_close(code: str, date: str | None=None) -> float | None:
    with db_conn() as conn:
        if date:
            row = conn.execute('SELECT close FROM index_daily WHERE code = ? AND date <= ? ORDER BY date DESC LIMIT 1', (code, date)).fetchone()
        else:
            row = conn.execute('SELECT close FROM index_daily WHERE code = ? ORDER BY date DESC LIMIT 1', (code,)).fetchone()
    return row[0] if row else None

def get_index_momentum(code: str='sh000300', days: int=21) -> float:
    with db_conn() as conn:
        idx = conn.execute('SELECT close FROM index_daily WHERE code = ? ORDER BY date DESC LIMIT ?', (code, days)).fetchall()
    return (idx[0][0] / idx[-1][0] - 1) * 100 if len(idx) >= days else 0.0

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

def get_industry_map() -> dict[str, str]:
    """stock_code → industry_name 全量映射（RBSA 聚合用）。"""
    with db_conn() as conn:
        rows = conn.execute('SELECT stock_code, industry_name FROM stock_industry_map').fetchall()
    return dict(rows)

def _percentile(values: list[float], pct: float) -> float:
    """线性插值分位数（pct ∈ [0,100]），与 sector_pool 同口径，避免截面分位判断漂移。"""
    if not values:
        return 0.0
    s = sorted(values)
    pos = (len(s) - 1) * pct / 100.0
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def get_sector_trend_features(date: str, flow_days: int = 5) -> dict[str, dict]:
    """赛道多周期趋势特征（聚合层，量化定池/LLM 素材共用单一来源）。

    对每个真实 RBSA 赛道聚合：
      mom_5d / mom_20d / vol_20d : 最近特征日（≤ date）基金均值（成员 <3 → None）
      flow_5d / flow_days         : 板块快照近 flow_days 个交易日主力净流入累计（万元）与覆盖天数
      label                      : 趋势质量标签（资金流出/短期走弱/高位降权/趋势健康）
    高位判定：20 日动量截面 P75+（与定池过热口径同一线性插值分位）。
    窗口降级：快照不足 flow_days 天时按可用天数累计并标注 flow_days，上游显式感知。
    """
    feat_date = get_latest_feature_date_before(date)
    with db_conn() as conn:
        feat_rows = conn.execute(
            "SELECT rbsa_industry_1, AVG(mom_5d), AVG(momentum_20d), AVG(vol_20d), COUNT(*) "
            "FROM fund_features WHERE date = ? AND rbsa_industry_1 IS NOT NULL "
            "AND rbsa_industry_1 NOT IN ('', '其他') "
            "GROUP BY rbsa_industry_1", (feat_date or date,)).fetchall()
        snap_rows = conn.execute(
            "SELECT sector_name, date, SUM(net_flow) FROM sector_daily_snapshot "
            "WHERE date <= ? GROUP BY sector_name, date", (date,)).fetchall()
    # 板块快照：每赛道取最近 flow_days 个交易日累计（不足则降级标注天数）
    flow_by_sector: dict[str, dict] = {}
    for sector_name, d, f in snap_rows:
        bucket = flow_by_sector.setdefault(sector_name, {"dates": [], "total": 0.0})
        bucket["dates"].append((d, f or 0.0))
    for bucket in flow_by_sector.values():
        bucket["dates"].sort(key=lambda x: x[0], reverse=True)
        bucket["total"] = sum(f for _, f in bucket["dates"][:flow_days])
        bucket["days"] = min(len(bucket["dates"]), flow_days)

    out: dict[str, dict] = {}
    mom20_vals: list[float] = []
    for sector, m5, m20, vol, cnt in feat_rows:
        has_funds = (cnt or 0) >= 3
        out[sector] = {
            "mom_5d": float(m5) if has_funds and m5 is not None else None,
            "mom_20d": float(m20) if has_funds and m20 is not None else None,
            "vol_20d": float(vol) if has_funds and vol is not None else None,
            "flow_5d": flow_by_sector.get(sector, {}).get("total"),
            "flow_days": flow_by_sector.get(sector, {}).get("days", 0),
            "label": "",
        }
        if out[sector]["mom_20d"] is not None:
            mom20_vals.append(out[sector]["mom_20d"])
    p75 = _percentile(mom20_vals, 75.0) if len(mom20_vals) >= 2 else None
    for t in out.values():
        if t["flow_5d"] is not None and t["flow_5d"] < 0:
            t["label"] = "资金流出"
        elif t["mom_5d"] is not None and t["mom_5d"] <= 0:
            t["label"] = "短期走弱"
        elif p75 is not None and t["mom_20d"] is not None and t["mom_20d"] >= p75:
            t["label"] = "高位降权"
        else:
            t["label"] = "趋势健康"
    return out


def get_latest_feature_date() -> str | None:
    """fund_features 最新特征日期（赛道中位动量对齐用）。"""
    with db_conn() as conn:
        row = conn.execute('SELECT MAX(date) FROM fund_features').fetchone()
    return row[0] if row else None

def get_latest_feature_date_before(date_str: str) -> str | None:
    """<= 指定日期的最近特征日（跨日/盘前运行时定池回退用，避免空池误判）。"""
    with db_conn() as conn:
        row = conn.execute(
            'SELECT MAX(date) FROM fund_features WHERE date <= ?', (date_str,)).fetchone()
    return row[0] if row and row[0] else None

def get_latest_features(code: str) -> dict | None:
    with db_conn() as conn:
        row = conn.execute('SELECT hurst_60d, momentum_20d, calmar, downside_vol, capture_up, capture_down, drawdown_60d, reversal_20d, mom_5d, mom_60d, vol_20d, rbsa_industry_1, rbsa_weight_1, rbsa_industry_2, rbsa_weight_2, rbsa_industry_3, rbsa_weight_3, date FROM fund_features WHERE code = ? ORDER BY date DESC LIMIT 1', (code,)).fetchone()
    if not row:
        return None
    return {'hurst_60d': row[0], 'momentum_20d': row[1], 'calmar': row[2], 'downside_vol': row[3], 'capture_up': row[4], 'capture_down': row[5], 'drawdown_60d': row[6], 'reversal_20d': row[7], 'mom_5d': row[8], 'mom_60d': row[9], 'vol_20d': row[10], 'rbsa_industry_1': row[11], 'rbsa_weight_1': row[12] or 0, 'rbsa_industry_2': row[13], 'rbsa_weight_2': row[14] or 0, 'rbsa_industry_3': row[15], 'rbsa_weight_3': row[16] or 0, 'date': row[17]}

def get_latest_holdings_date(code: str) -> str | None:
    """基金最新季报披露日期。"""
    with db_conn() as conn:
        row = conn.execute('SELECT MAX(report_date) FROM fund_holdings WHERE code = ?', (code,)).fetchone()
    return row[0] if row else None

def get_latest_holdings_rows() -> list[tuple]:
    """全部基金最新报告期持仓行 (code, stock_code, stock_name, weight)（RBSA 预加载用）。"""
    with db_conn() as conn:
        rows = conn.execute('SELECT code, stock_code, stock_name, weight FROM fund_holdings WHERE report_date IN (SELECT MAX(report_date) FROM fund_holdings GROUP BY code)').fetchall()
    return list(rows)

def _ema250_latest(closes: list[float]) -> float | None:
    """收盘序列的 EMA250（年线）末值（现算）；数据不足 250 条返回 None。

    与回测 _ema250_of / backtest gate 同口径（k=2/251, adjust=False）；
    市场 regime 判定与市场门共用单一来源。
    """
    if len(closes) <= 250:
        return None
    k = 2.0 / 251.0
    ema = closes[0]
    for v in closes[1:]:
        ema = v * k + ema * (1.0 - k)
    return ema


def get_market_below_ema250() -> bool:
    """沪深300 收盘 < EMA250（年线）——市场门判定（2026-09，回测验证）。

    与回测 gate_verdict(rules) 同口径（单条件）：跌破年线 → 今日不出手，
    从源头回避熊市区间。回测（2021-2026、67 决策点、同一轮同源对比）：
    出手日均值 +2.30%→+6.88%、胜率 55%→72%、出手序列回撤 -49.8%→-24.8%。
    数据不足（<250 条）返回 False（不误拦，交回 LLM 门）。
    """
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT close FROM index_daily WHERE code='sh000300' AND close IS NOT NULL "
            "ORDER BY date").fetchall()
    closes = [float(r[0]) for r in rows]
    if not closes:
        return False
    ema = _ema250_latest(closes)
    return ema is not None and closes[-1] < ema


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

def get_holdings_report_dates() -> dict[str, str]:
    """各基金最新持仓报告期（持仓增量下载的本地最新窗口判断，原 foundation 内联 GROUP BY）。"""
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT code, MAX(report_date) FROM fund_holdings GROUP BY code").fetchall()
    return dict(rows)


def get_nav_time_state() -> tuple[dict[str, tuple[str, str]], list[str]]:
    """净值时间状态（单一归属）：(每基金日期区间 {code:(首日,末日)}, 全部净值日期升序)。

    打标/陈旧/增量下载共用：全局最新净值日 = max(v[1] for v in ranges.values())，
    滞后计数用完整日期集（trading_day_lag 口径）。替代 foundation/nav 四处逐行重复。
    """
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT code, MIN(date), MAX(date) FROM fund_nav GROUP BY code").fetchall()
        ranges = {r[0]: (r[1], r[2]) for r in rows}
        dates = sorted(r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM fund_nav").fetchall())
    return ranges, dates


def has_nav_data() -> bool:
    """基金净值表是否已有数据（增量 vs 首装全量分支）。"""
    with db_conn() as conn:
        return conn.execute("SELECT 1 FROM fund_nav LIMIT 1").fetchone() is not None


def has_index_data() -> bool:
    """指数表是否已有数据（Step3 增量窗口 vs 历史全量）。"""
    with db_conn() as conn:
        return conn.execute("SELECT 1 FROM index_daily LIMIT 1").fetchone() is not None


def get_industry_map_gap_count() -> int:
    """持仓中未映射行业的股票数（行业映射 90 天跳过判定，原 foundation 内联子查询）。"""
    with db_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT h.stock_code FROM fund_holdings h "
            "LEFT JOIN stock_industry_map i ON h.stock_code = i.stock_code "
            "WHERE i.stock_code IS NULL)").fetchone()[0]


def get_industry_map_targets() -> tuple[list[str], set[str]]:
    """行业映射同步输入：(全部持仓股票, 已映射股票集)（增量过滤，原 foundation 两条内联读）。"""
    with db_conn() as conn:
        all_stocks = [r[0] for r in conn.execute(
            "SELECT DISTINCT stock_code FROM fund_holdings").fetchall()]
        mapped = {r[0] for r in conn.execute(
            "SELECT stock_code FROM stock_industry_map").fetchall()}
    return all_stocks, mapped


def get_industry_map_stats() -> tuple[int, int]:
    """RBSA 统计：已映射股票数, 有持仓基金数（Step6 面板，原 foundation 两条内联读）。"""
    with db_conn() as conn:
        mapped = conn.execute("SELECT COUNT(*) FROM stock_industry_map").fetchone()[0]
        funds = conn.execute("SELECT COUNT(DISTINCT code) FROM fund_holdings").fetchone()[0]
    return mapped, funds


def get_market_technical() -> dict | None:
    """沪深300最新技术面快照（结构化数据），供 LLM regime 判定注入 prompt；数据不足返回 None。

    返回 {"date", "close", "chg_pct", "ema60", "closes"}；文案拼装由消费方（LLM 装配）负责。
    """
    with db_conn() as conn:
        rows = conn.execute("SELECT date, close, ema60 FROM index_daily WHERE code='sh000300' AND close IS NOT NULL ORDER BY date DESC LIMIT 6").fetchall()
    if not rows or not rows[0][2]:
        return None
    latest_date, close, ema60 = rows[0]
    prev_close = rows[1][1] if len(rows) > 1 and rows[1][1] else close
    chg = (close - prev_close) / prev_close * 100 if prev_close else 0.0
    return {
        "date": latest_date, "close": close, "chg_pct": chg,
        "ema60": ema60, "closes": [r[1] for r in reversed(rows)],
    }

def get_meta(key: str) -> str | None:
    """读取 meta 配置值（行业映射更新时间等）。"""
    with db_conn() as conn:
        return meta_get(conn, key)


def save_meta(key: str, value: str) -> None:
    """写入 meta 配置值（与 get_meta 对称，供进化引擎记录时间戳等）。"""
    with db_conn() as conn:
        meta_set(conn, key, value)


def get_settings_all() -> dict[str, str]:
    """settings: 前缀键批量读（config 运行时持久化，ADR-0005 meta 单一读写 seam）。"""
    with db_conn() as conn:
        return dict(conn.execute(
            "SELECT key, value FROM meta WHERE key LIKE 'settings:%'"
        ).fetchall())


def save_settings_all(items: dict[str, str]) -> None:
    """settings: 前缀键整段替换（先删后插，幂等）；消费方不再持有裸 meta SQL。"""
    with db_conn() as conn:
        conn.execute("DELETE FROM meta WHERE key LIKE 'settings:%'")
        conn.executemany(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            list(items.items()),
        )
        conn.commit()


def get_interval_days(key: str) -> int | None:
    """距上次记录（meta 键值 YYYY-MM-DD）的间隔天数；无记录/解析失败返回 None。

    架构深化 I：时间戳解析收敛为窄读（消费方不再各自 strptime/ValueError 兜底），
    冷却/限频口径统一可比。
    """
    raw = get_meta(key)
    if not raw:
        return None
    try:
        last = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None
    return (datetime.now().date() - last).days


def get_int_cursor(key: str) -> int:
    """整数游标（meta 键值整数）；无记录/解析失败返回 0（架构深化 I）。"""
    raw = get_meta(key)
    if not raw:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def get_data_latest_date() -> str | None:
    """核心数据表的最新日期（净值/特征/指数/宏观/监控），无数据返回 None。

    各表 date 均为 YYYY-MM-DD；取跨表最大值即「数据更新到哪天」。
    """
    tables = ("fund_nav", "fund_features", "index_daily", "macro_news", "monitor_events")
    sql = " UNION ALL ".join(f"SELECT MAX(date) AS d FROM {t}" for t in tables)
    with db_conn() as conn:
        row = conn.execute(f"SELECT MAX(d) FROM ({sql})").fetchone()
    return row[0] if row and row[0] else None


def get_model_last_trained() -> str | None:
    """读取最近一次模型训练日期（meta 表），无则返回 None。"""
    with db_conn() as conn:
        return meta_get(conn, META.MODEL_LAST_TRAINED)

def get_sector_momentum_median(sector: str, date: str) -> float | None:
    """赛道 20 日动量中位数（成员 <3 返回 None）：推荐 mom_gap 与监控赛道优势共用单一来源。

    架构深化 J：原 get_momentum_in_sector 浅 helper（仅被本函数使用）内联，
    口径不变（仅 momentum_20d 非空过滤——与三周期 medians 的列集要求不同，保持独立）。
    """
    with db_conn() as conn:
        rows = conn.execute(
            'SELECT momentum_20d FROM fund_features WHERE rbsa_industry_1 = ? AND date = ? '
            'AND momentum_20d IS NOT NULL', (sector, date)).fetchall()
    moms = [r[0] for r in rows]
    if len(moms) < 3:
        return None
    values = sorted(moms)
    n = len(values)
    return values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2


def get_sector_momentum_medians(sector: str, date: str) -> dict | None:
    """赛道 5/20/60 日动量中位数（量化定池信号源；成员 <3 返回 None）。"""
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT mom_5d, momentum_20d, mom_60d FROM fund_features "
            "WHERE rbsa_industry_1 = ? AND date = ? "
            "AND mom_5d IS NOT NULL AND momentum_20d IS NOT NULL AND mom_60d IS NOT NULL",
            (sector, date)).fetchall()
    if len(rows) < 3:
        return None
    def med(vals):
        return (lambda v: v[len(v) // 2] if len(v) % 2 else (v[len(v) // 2 - 1] + v[len(v) // 2]) / 2)(sorted(vals))
    return {
        "mom_5d": med([r[0] for r in rows]),
        "mom_20d": med([r[1] for r in rows]),
        "mom_60d": med([r[2] for r in rows]),
        "n": len(rows),
    }

def get_rbsa_at_date(code: str, date: str) -> tuple[str | None, float | None] | None:
    """指定日期快照的 (rbsa_industry_1, rbsa_weight_1)；无记录返回 None。"""
    with db_conn() as conn:
        row = conn.execute(
            'SELECT rbsa_industry_1, rbsa_weight_1 FROM fund_features WHERE code = ? AND date = ?',
            (code, date)).fetchone()
    return row if row else None


def get_first_rbsa_after(code: str, date: str) -> tuple[str | None, float | None] | None:
    """date 之后（含）第一个非空 RBSA 快照 (rbsa_industry_1, rbsa_weight_1)。

    用于买入日处于持仓报告期空窗（当天快照 rbsa 为空）时的基准兑底。
    """
    with db_conn() as conn:
        row = conn.execute(
            'SELECT rbsa_industry_1, rbsa_weight_1 FROM fund_features '
            'WHERE code = ? AND date >= ? AND rbsa_industry_1 IS NOT NULL AND rbsa_industry_1 != "" '
            'ORDER BY date LIMIT 1', (code, date)).fetchone()
    return row if row else None

def get_sector_candidates(sectors: list[str]) -> list[dict]:
    """赛道内候选基金：fund_features 三行业匹配 + 全部特征列（推荐排序用）。"""
    if not sectors:
        return []
    placeholders = ','.join('?' * len(sectors))
    feat_cols = ', '.join('ff.' + c for c in FEATURE_COLS)
    with db_conn() as conn:
        rows = conn.execute(f'SELECT ff.code, fb.name, ff.regime, ff.rbsa_industry_1, ff.rbsa_weight_1, ff.rbsa_industry_2, ff.rbsa_weight_2, ff.rbsa_industry_3, ff.rbsa_weight_3, lat.date AS feature_date, {feat_cols} FROM fund_features ff {_latest_feature_join()} JOIN fund_basic fb ON fb.code = ff.code WHERE fb.is_buyable = 1 AND (ff.rbsa_industry_1 IN ({placeholders})   OR ff.rbsa_industry_2 IN ({placeholders})   OR ff.rbsa_industry_3 IN ({placeholders}))', sectors + sectors + sectors).fetchall()
    names = ['code', 'name', 'regime', 'rbsa_industry_1', 'rbsa_weight_1', 'rbsa_industry_2', 'rbsa_weight_2', 'rbsa_industry_3', 'rbsa_weight_3', 'feature_date'] + FEATURE_COLS
    return [dict(zip(names, r, strict=False)) for r in rows]

def get_sector_heatmap(limit: int=6) -> list[dict]:
    """行业热力图：平均 RBSA 权重与平均动量的 Top 行业（结构化行）。"""
    with db_conn() as conn:
        rows = conn.execute("SELECT rbsa_industry_1, AVG(rbsa_weight_1), AVG(momentum_20d) FROM fund_features WHERE rbsa_industry_1 IS NOT NULL AND rbsa_industry_1 != '' GROUP BY rbsa_industry_1 ORDER BY AVG(rbsa_weight_1) DESC LIMIT ?", (limit,)).fetchall()
    return [{"name": r[0], "weight": r[1], "momentum": r[2]} for r in rows]

def get_train_fund_codes(min_bars: int, limit: int) -> list[str]:
    """随机采样满足最小净值条数的基金代码（训练集构建）。"""
    with db_conn() as conn:
        rows = conn.execute('SELECT code FROM fund_nav GROUP BY code HAVING COUNT(*) >= ? ORDER BY RANDOM() LIMIT ?', (min_bars, limit)).fetchall()
    return [r[0] for r in rows]


def sample_fund_codes_before(date: str, min_bars: int, limit: int) -> list[str]:
    """按截止日期动态随机采样基金（回测按决策日采样，防幸存者偏差）。

    只用 date 当日及之前有 min_bars 条净值的基金（早期回测点无历史数据自动排除）。
    经 repo 统一读 seam——回测不再直连数据库手写 SQL。
    """
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT code FROM fund_nav WHERE date <= ? GROUP BY code "
            "HAVING COUNT(*) >= ? ORDER BY RANDOM() LIMIT ?",
            (date, min_bars, limit)).fetchall()
    return [r[0] for r in rows]


def check_data_ready() -> dict[str, int]:
    """推荐前置数据就绪状态（持仓/行业映射/特征计数）。

    门控（check_holdings_ready）的单一数据来源：引擎/管线不再内嵌裸 SQL 计数，
    查询细节在此下沉到 seam（架构深化候选 2）。feature_cnt 为最近特征日有数据的
    基金数——审计 P1-2：特征全缺时不能冒充"空推荐日"。
    """
    with db_conn() as conn:
        holdings_cnt = conn.execute("SELECT COUNT(*) FROM fund_holdings").fetchone()[0]
        industry_cnt = conn.execute("SELECT COUNT(*) FROM stock_industry_map").fetchone()[0]
        feature_cnt = conn.execute(
            "SELECT COUNT(*) FROM fund_features "
            "WHERE date = (SELECT MAX(date) FROM fund_features)").fetchone()[0]
    return {"holdings_cnt": holdings_cnt, "industry_cnt": industry_cnt,
            "feature_cnt": feature_cnt}


def is_recommend_data_ready() -> bool:
    """推荐数据就绪谓词：异常统一兜底返回 False。

    判定语义（单一来源）：持仓>0 且行业映射>0 且特征存在>0；
    审计 P1-2：特征表为空/最近特征日无数据时视为未就绪（数据故障不应被
    静默包装成"空推荐日"）；DB 瞬时异常不向门控调用方抛穿（避免中断后续槽位）。
    """
    try:
        status = check_data_ready()
    except Exception:
        return False
    return (status["holdings_cnt"] > 0 and status["industry_cnt"] > 0
            and status["feature_cnt"] > 0)


def get_latest_rbsa_sector_map() -> dict[str, str]:
    """code → 最新 RBSA 第一行业 映射（回测赛道模式用，当前时点快照）。"""
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT code, rbsa_industry_1 FROM fund_features "
            "WHERE date = (SELECT MAX(date) FROM fund_features) AND rbsa_industry_1 != ''").fetchall()
    return dict(rows)



def get_system_logs(lines: int = 200, after: int = 0, before: int = 0) -> tuple[list[tuple], int, int]:
    """系统日志读取（Web /api/logs 用，避免绕过 repo seam 内联 SQL）。

    - after > 0：增量拉新（id > after，轮转/清理后仍可靠）；
    - before > 0：向前翻页拉更早（id < before 的最近 lines 条，按 id 升序返回）；
    - 默认：最新 lines 条（按 id 升序返回，UI 从上到下按时间正序展示）。
    返回 (rows, total, last_id)；last_id 恒为返回行中最大 id。
    """
    from app.utils.log import SYSTEM_LOG_TABLE_SQL
    with db_conn() as conn:
        conn.execute(SYSTEM_LOG_TABLE_SQL)
        total = conn.execute("SELECT COUNT(*) FROM system_logs").fetchone()[0]
        if before > 0:
            rows = conn.execute(
                "SELECT id, ts, level, logger, event, message, correlation_id "
                "FROM system_logs WHERE id < ? ORDER BY id DESC LIMIT ?",
                (before, lines),
            ).fetchall()
            rows.reverse()
        elif after <= 0:
            rows = conn.execute(
                "SELECT id, ts, level, logger, event, message, correlation_id "
                "FROM system_logs ORDER BY id DESC LIMIT ?",
                (lines,),
            ).fetchall()
            rows.reverse()
        else:
            rows = conn.execute(
                "SELECT id, ts, level, logger, event, message, correlation_id "
                "FROM system_logs WHERE id > ? ORDER BY id LIMIT ?",
                (after, lines),
            ).fetchall()
    last_id = after
    for r in rows:
        last_id = r[0]
    return rows, total, last_id


def get_uptime_days() -> int:
    with db_conn() as conn:
        start = meta_get(conn, META.UPTIME_START)
    if start:
        return (datetime.now() - datetime.strptime(start, '%Y-%m-%d')).days
    return 365


def set_model_last_trained(date_str: str) -> None:
    """记录最近一次模型训练日期。"""
    with db_conn() as conn:
        meta_set(conn, META.MODEL_LAST_TRAINED, date_str)


def get_model_label_version() -> str | None:
    """读取模型训练标签版本（meta 表），无则返回 None（老模型无元信息）。"""
    with db_conn() as conn:
        return meta_get(conn, META.MODEL_LABEL_VERSION)


def set_model_label_version(version: str) -> None:
    """记录模型训练标签版本（train 成功后写入，供加载路径校验）。"""
    with db_conn() as conn:
        meta_set(conn, META.MODEL_LABEL_VERSION, version)



__all__ = ["FEATURE_COLS", "MARKET_COLS", "FORWARD_WINDOW", "check_data_ready", "is_recommend_data_ready", "get_all_ranking_rows", "get_available_sectors", "get_buyable_codes", "get_buyable_feature_stats", "get_codes_missing_rbsa", "get_feature_codes_before", "get_feature_dates_map", "get_fund_name", "get_fund_pool_stats", "get_holdings", "get_holdings_at_report", "get_holdings_report_dates", "get_holdings_summaries", "get_index_close", "get_index_momentum", "get_index_rows", "get_index_series", "get_industry_map", "get_industry_map_gap_count", "get_industry_map_stats", "get_industry_map_targets", "get_latest_feature_date", "get_latest_feature_date_before", "get_latest_features", "get_latest_holdings_date", "get_latest_holdings_rows", "get_market_below_ema250", "get_market_regime", "get_market_technical", "get_meta", "get_model_last_trained", "get_model_label_version", "set_model_label_version", "get_data_latest_date", "get_interval_days", "get_int_cursor", "get_nav_time_state", "get_sector_momentum_median", "get_sector_momentum_medians", "get_rbsa_at_date", "get_first_rbsa_after", "get_sector_candidates", "get_sector_heatmap", "get_system_logs", "get_train_fund_codes", "get_uptime_days", "has_index_data", "has_nav_data", "sample_fund_codes_before", "save_meta", "set_model_last_trained"]

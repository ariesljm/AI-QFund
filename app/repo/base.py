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

def get_buyable_codes() -> list[str]:
    """可投基金代码全集（持仓/净值下载、特征批量计算共用；buyable 查询单一归属）。"""
    with db_conn() as conn:
        rows = conn.execute('SELECT code FROM fund_basic WHERE is_buyable = 1').fetchall()
    return [r[0] for r in rows]


def get_fund_basics() -> list[tuple[str, str]]:
    """全部可投基金 (code, type)——票 11 主动权益池筛选用（type 实测枚举：混合型/指数型/股票型）。"""
    with db_conn() as conn:
        rows = conn.execute('SELECT code, type FROM fund_basic WHERE is_buyable = 1').fetchall()
    return [(r[0], r[1]) for r in rows]


def get_restriction_facts(codes: list[str]) -> dict[str, dict]:
    """硬过滤事实（票 11 接线）：{code: {aum, purchase_status, daily_limit, nav_count}}。

    单查询收敛 + 全表聚合规避：aum 全 buyable 一次查询；申赎小表 IN；
    **nav_count 不查 fund_nav**（1160 万行聚合）——is_buyable=1 已由
    mark_short_history_funds 排除 <62 条的基金，nav_count 语义由 buyable 覆盖。
    """
    if not codes:
        return {}
    wanted = set(codes)
    out: dict[str, dict] = {c: {"aum": None, "purchase_status": "unknown",
                                "daily_limit": None, "nav_count": 999} for c in codes}
    with db_conn() as conn:
        for code, aum in conn.execute(
                "SELECT code, aum FROM fund_basic WHERE is_buyable = 1").fetchall():
            if code in wanted:
                out[code]["aum"] = aum
        ph = ",".join("?" for _ in codes)
        for code, status, dlimit in conn.execute(
                f"SELECT code, status, daily_limit FROM purchase_restrictions "
                f"WHERE code IN ({ph})", codes).fetchall():
            if code in wanted:
                out[code]["purchase_status"] = status or "unknown"
                out[code]["daily_limit"] = dlimit
    return out

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

def get_fund_pool_stats() -> tuple[int, list[dict]]:
    with db_conn() as conn:
        total = conn.execute('SELECT COUNT(*) FROM fund_basic WHERE is_buyable = 1').fetchone()[0]
        by_type = conn.execute('SELECT type, COUNT(*) FROM fund_basic WHERE is_buyable = 1 GROUP BY type ORDER BY COUNT(*) DESC').fetchall()
    return (total, [{'type': t[0] or '其他', 'count': t[1]} for t in by_type])

def get_holdings(code: str, limit: int=10, as_of: str | None = None) -> list[dict]:
    """持仓 top-N。as_of（决策日）非空时按 PIT 口径取期次：只认公告日 <= as_of 的
    报告期（共识 Q15）；否则取库内最新一期。
    """
    if as_of:
        sql = ('SELECT h.stock_code, h.stock_name, h.weight, i.industry_name FROM fund_holdings h '
               'LEFT JOIN stock_industry_map i ON h.stock_code = i.stock_code '
               'WHERE h.code = ? AND h.report_date = (SELECT MAX(report_date) FROM fund_holdings '
               'WHERE code = ? AND disclosure_date <= ?) ORDER BY h.weight DESC LIMIT ?')
        args: tuple = (code, code, as_of, limit)
    else:
        sql = ('SELECT h.stock_code, h.stock_name, h.weight, i.industry_name FROM fund_holdings h '
               'LEFT JOIN stock_industry_map i ON h.stock_code = i.stock_code '
               'WHERE h.code = ? AND h.report_date = (SELECT MAX(report_date) FROM fund_holdings '
               'WHERE code = ?) ORDER BY h.weight DESC LIMIT ?')
        args: tuple = (code, code, limit)
    with db_conn() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [{'stock_code': r[0], 'stock_name': r[1], 'weight': r[2], 'industry': r[3] or ''} for r in rows]

def get_holdings_summaries(codes: list[str], limit: int = 5, as_of: str | None = None) -> dict[str, dict]:
    """候选批量持仓素材：{code: {"holdings": [top-N], "report_date": str|None}}。

    推荐 LLM 终选素材装配 N+1 收敛（批量先例 get_candidate_nav_summaries 同风格）：
    一次返回全部候选的最新报告期 + 该期前 limit 大持仓；无记录报告期/持仓为空。
    as_of（决策日）非空时按 PIT 口径取期次：只认公告日 <= as_of 的报告期，
    否则回测/训练会把尚未公告的季报持仓喂给当时的决策（共识 Q15）。
    """
    if not codes:
        return {}
    ph = ",".join("?" for _ in codes)
    out = {c: {"holdings": [], "report_date": None} for c in codes}
    latest_sql = (f"SELECT code, MAX(report_date) FROM fund_holdings "
                  f"WHERE code IN ({ph}) AND disclosure_date <= ? GROUP BY code")
    latest_args: list = list(codes) + ([as_of] if as_of else [])
    if not as_of:
        latest_sql = (f"SELECT code, MAX(report_date) FROM fund_holdings "
                      f"WHERE code IN ({ph}) GROUP BY code")
    with db_conn() as conn:
        latest = dict(conn.execute(latest_sql, latest_args).fetchall())
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


def get_pe_histories(codes: list[str], days: int = 750) -> dict[str, list[float]]:
    """个股 PE 日频历史（升序，末位=最新），供重仓股加权 PE 分位（票 05）用。

    days 默认 750 ≈ 3 年交易日；每只股票取最近 days 条 PE_TTM 非空值。
    窗口函数 PARTITION BY stock_code 保证各股票独立限长，不会被跨股票截断。
    无数据/样本不足由调用方自行设门槛。
    """
    if not codes:
        return {}
    ph = ",".join("?" for _ in codes)
    out: dict[str, list[float]] = {c: [] for c in codes}
    with db_conn() as conn:
        rows = conn.execute(
            f"SELECT stock_code, pe FROM ("
            f"  SELECT stock_code, pe, date, "
            f"  ROW_NUMBER() OVER (PARTITION BY stock_code ORDER BY date DESC) AS rn "
            f"  FROM stock_valuation_daily WHERE stock_code IN ({ph}) AND pe IS NOT NULL"
            f") WHERE rn <= ? ORDER BY stock_code, date ASC",
            codes + [days]).fetchall()
    for code, pe in rows:
        out[code].append(pe)
    return out


def get_stock_daily(code: str, days: int | None = None) -> dict[str, float]:
    """个股日线（前复权收盘价，票 07）：{date: close} 升序。days 非空取最近 N 条。"""
    sql = "SELECT date, close FROM stock_daily WHERE stock_code = ? ORDER BY date"
    args: list = [code]
    if days is not None:
        sql = ("SELECT date, close FROM ("
               "  SELECT date, close, ROW_NUMBER() OVER (ORDER BY date DESC) AS rn "
               "  FROM stock_daily WHERE stock_code = ?) WHERE rn <= ? ORDER BY date")
        args = [code, days]
    with db_conn() as conn:
        return {r[0]: r[1] for r in conn.execute(sql, args).fetchall()}


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


def get_holdings_two_periods(code: str, limit: int = 10) -> tuple[list[dict], list[dict]]:
    """最近两期持仓（当期 + 次期，报告期倒序）——持仓异动切片装配 seam（票 12 切片一）。

    仅一期或缺失 → prev 为空列表（切片显式'无对比基准'，不脑补）；
    两期均按 weight 降序取 limit。PIT 一致性由消费方（get_holdings as_of）负责，
    此处是给 LLM 当期判断用的最近两期快照口径。
    """
    with db_conn() as conn:
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT report_date FROM fund_holdings WHERE code = ? "
            "ORDER BY report_date DESC LIMIT 2", (code,))]
    cur = get_holdings_at_report(code, dates[0], limit) if dates else []
    prev = get_holdings_at_report(code, dates[1], limit) if len(dates) > 1 else []
    return cur, prev


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

def get_industry_map() -> dict[str, str]:
    """stock_code → industry_name 全量映射（RBSA 聚合用）。"""
    with db_conn() as conn:
        rows = conn.execute('SELECT stock_code, industry_name FROM stock_industry_map').fetchall()
    return dict(rows)



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


def get_latest_features_batch(codes: list[str] | None = None) -> dict[str, dict]:
    """全部（或给定）基金最新特征一次查询（票 11 全市场初筛 N+1 收敛）。

    窗口函数每基金取最新一行的特征；无特征基金不在返回中。
    返回 {code: {FEATURE_COLS..., rbsa_industry_1, rbsa_weight_1, date}}。
    """
    _COLS = (*FEATURE_COLS, "rbsa_industry_1", "rbsa_weight_1", "date")
    where = ""
    args: list = []
    if codes:
        ph = ",".join("?" for _ in codes)
        where = f" WHERE code IN ({ph})"
        args = list(codes)
    sql = (f"SELECT code, {', '.join('f.' + c for c in _COLS)} FROM ("
           f"  SELECT code, {', '.join(_COLS)}, "
           f"  ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) AS rn "
           f"  FROM fund_features{where}) f WHERE rn = 1")
    with db_conn() as conn:
        rows = conn.execute(sql, args).fetchall()
    out: dict[str, dict] = {}
    for row in rows:
        out[row[0]] = {c: row[i + 1] for i, c in enumerate(_COLS)}
    return out

def get_latest_holdings_rows() -> list[tuple]:
    """全部基金最新报告期持仓行 (code, stock_code, stock_name, weight)（RBSA 预加载用）。"""
    with db_conn() as conn:
        rows = conn.execute('SELECT code, stock_code, stock_name, weight FROM fund_holdings WHERE report_date IN (SELECT MAX(report_date) FROM fund_holdings GROUP BY code)').fetchall()
    return list(rows)

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

def get_holdings_report_dates() -> dict[str, str]:
    """各基金最新持仓报告期（持仓增量下载的本地最新窗口判断，原 foundation 内联 GROUP BY）。"""
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT code, MAX(report_date) FROM fund_holdings GROUP BY code").fetchall()
    return dict(rows)


def get_holdings_report_dates_all() -> dict[str, set[str]]:
    """各基金已留存的全部持仓报告期（历史多期回填的增量跳过判断）。"""
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT code, report_date FROM fund_holdings").fetchall()
    result: dict[str, set[str]] = {}
    for code, report_date in rows:
        result.setdefault(code, set()).add(report_date)
    return result


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



def get_sector_heatmap(limit: int=6) -> list[dict]:
    """行业热力图：平均 RBSA 权重与平均动量的 Top 行业（结构化行）。"""
    with db_conn() as conn:
        rows = conn.execute("SELECT rbsa_industry_1, AVG(rbsa_weight_1), AVG(momentum_20d) FROM fund_features WHERE rbsa_industry_1 IS NOT NULL AND rbsa_industry_1 != '' GROUP BY rbsa_industry_1 ORDER BY AVG(rbsa_weight_1) DESC LIMIT ?", (limit,)).fetchall()
    return [{"name": r[0], "weight": r[1], "momentum": r[2]} for r in rows]

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


__all__ = ["FEATURE_COLS", "FORWARD_WINDOW", "MARKET_COLS", "_ema250_latest", "_latest_feature_join", "check_data_ready", "get_all_ranking_rows", "get_buyable_codes", "get_codes_missing_rbsa", "get_data_latest_date", "get_feature_codes_before", "get_feature_dates_map", "get_fund_basics", "get_fund_pool_stats", "get_holdings", "get_holdings_at_report", "get_holdings_report_dates", "get_holdings_report_dates_all", "get_holdings_summaries", "get_holdings_two_periods", "get_index_close", "get_index_rows", "get_index_series", "get_industry_map", "get_industry_map_gap_count", "get_industry_map_stats", "get_industry_map_targets", "get_int_cursor", "get_interval_days", "get_latest_feature_date_before", "get_latest_features", "get_latest_features_batch", "get_latest_holdings_rows", "get_market_regime", "get_meta", "get_model_last_trained", "get_nav_time_state", "get_pe_histories", "get_restriction_facts", "get_sector_heatmap", "get_settings_all", "get_stock_daily", "get_system_logs", "get_uptime_days", "has_index_data", "has_nav_data", "save_meta", "save_settings_all"]

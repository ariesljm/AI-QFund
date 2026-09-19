"""基金/持仓/估值/个股/行业 底层数据 seam（从 base 拆分：fund/holdings 域）。"""

from app.database import db_conn


def get_fund_basics() -> list[tuple[str, str]]:
    """全部可投基金 (code, type)——票 11 主动权益池筛选用（type 实测枚举：混合型/指数型/股票型）。"""
    with db_conn() as conn:
        rows = conn.execute('SELECT code, type FROM fund_basic WHERE is_buyable = 1').fetchall()
    return [(r[0], r[1]) for r in rows]


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


def get_industry_map() -> dict[str, str]:
    """stock_code → industry_name 全量映射（RBSA 聚合用）。"""
    with db_conn() as conn:
        rows = conn.execute('SELECT stock_code, industry_name FROM stock_industry_map').fetchall()
    return dict(rows)


def get_latest_holdings_rows() -> list[tuple]:
    """全部基金最新报告期持仓行 (code, stock_code, stock_name, weight)（RBSA 预加载用）。"""
    with db_conn() as conn:
        rows = conn.execute('SELECT code, stock_code, stock_name, weight FROM fund_holdings WHERE report_date IN (SELECT MAX(report_date) FROM fund_holdings GROUP BY code)').fetchall()
    return list(rows)


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

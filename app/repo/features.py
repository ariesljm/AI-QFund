"""特征/候选池/硬过滤 底层数据 seam（从 base 拆分：features/ranking 域）。"""

from app import domain
from app.database import db_conn

FEATURE_COLS = domain.FEATURE_COLS


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

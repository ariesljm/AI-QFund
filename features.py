"""特征计算模块：Hurst、动量、卡玛、RBSA、大盘状态机。"""

from log_utils import get_logger
import sqlite3
import time

import numpy as np

import log_utils  # noqa: F401

logger = get_logger("features")




def calc_hurst(series: np.ndarray, max_lag: int = 20) -> float:
    if len(series) < max_lag + 10:
        return 0.5
    lags = range(2, max_lag + 1)
    rs_values = []
    for lag in lags:
        n_blocks = len(series) // lag
        if n_blocks == 0:
            continue
        rs_list = []
        for i in range(n_blocks):
            block = series[i * lag : (i + 1) * lag]
            mean_block = np.mean(block)
            deviations = np.cumsum(block - mean_block)
            r = np.max(deviations) - np.min(deviations)
            s = np.std(block, ddof=1) if np.std(block, ddof=1) > 0 else 1e-10
            rs_list.append(r / s)
        if rs_list:
            rs_values.append((np.log(lag), np.log(np.mean(rs_list))))
    if len(rs_values) < 2:
        return 0.5
    x = np.array([v[0] for v in rs_values])
    y = np.array([v[1] for v in rs_values])
    if len(x) < 2 or np.any(~np.isfinite(y)):
        return 0.5
    slope = np.polyfit(x, y, 1)[0]
    return float(np.clip(slope, 0, 1))


def calc_rbsa(holdings: list[dict], conn: sqlite3.Connection | None = None) -> list[dict]:
    industry_weights: dict[str, float] = {}
    for h in holdings:
        stock_code = h["stock_code"]
        industry = None
        if conn:
            row = conn.execute(
                "SELECT industry_name FROM stock_industry_map WHERE stock_code = ?",
                (stock_code,),
            ).fetchone()
            if row and row[0]:
                industry = row[0]
        if not industry:
            industry = "其他"
        industry_weights[industry] = industry_weights.get(industry, 0) + h["weight"]
    sorted_industries = sorted(industry_weights.items(), key=lambda x: x[1], reverse=True)
    return [{"industry": ind, "weight": w} for ind, w in sorted_industries[:3]]


def calc_features(code: str, conn: sqlite3.Connection) -> dict:
    cur = conn.execute(
        "SELECT date, cum_nav FROM fund_nav WHERE code = ? ORDER BY date ASC",
        (code,),
    )
    rows = cur.fetchall()
    if len(rows) < 60:
        logger.warning("基金 %s 净值数据不足 (%d 天)，跳过特征计算", code, len(rows))
        return {}
    dates = [r[0] for r in rows]
    navs = np.array([r[1] for r in rows], dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        returns = np.diff(navs) / navs[:-1]
    returns = returns[np.isfinite(returns)]
    features: dict = {"code": code, "date": dates[-1]}
    window = min(60, len(returns))
    features["hurst_60d"] = calc_hurst(returns[-window:])
    if len(navs) >= 20:
        features["momentum_20d"] = float((navs[-1] / navs[-20] - 1) * 100)
    else:
        features["momentum_20d"] = 0.0
    if len(navs) >= 60:
        cum_returns = navs[-60:] / navs[-60]
        peak = np.maximum.accumulate(cum_returns)
        drawdown = (cum_returns - peak) / peak
        max_dd = float(np.min(drawdown))
        ann_return = float((navs[-1] / navs[-60] - 1) * 252 / 60)
        features["calmar"] = ann_return / abs(max_dd) if abs(max_dd) > 1e-10 else 0.0
    else:
        features["calmar"] = 0.0
    if len(returns) >= 20:
        neg_returns = returns[-20:][returns[-20:] < 0]
        features["downside_vol"] = float(np.std(neg_returns) * np.sqrt(252)) if len(neg_returns) > 0 else 0.0
    else:
        features["downside_vol"] = 0.0
    cur_idx = conn.execute(
        "SELECT date, close, volume FROM index_daily WHERE code = 'sh000300' ORDER BY date ASC",
    )
    idx_rows = cur_idx.fetchall()
    idx_volumes = np.array([r[2] for r in idx_rows], dtype=float) if idx_rows else np.array([])
    idx_closes = np.array([r[1] for r in idx_rows], dtype=float) if idx_rows else np.array([])
    if len(idx_rows) >= 60 and len(returns) >= 60:
        idx_returns = np.diff(idx_closes) / idx_closes[:-1]
        idx_returns = idx_returns[np.isfinite(idx_returns)]
        min_len = min(60, len(returns), len(idx_returns))
        fund_ret = returns[-min_len:]
        idx_ret = idx_returns[-min_len:]
        up_mask = idx_ret > 0
        down_mask = idx_ret < 0
        if np.sum(up_mask) > 0:
            features["capture_up"] = float(np.mean(fund_ret[up_mask]) / np.mean(idx_ret[up_mask]))
        else:
            features["capture_up"] = 1.0
        if np.sum(down_mask) > 0:
            features["capture_down"] = float(np.mean(fund_ret[down_mask]) / np.mean(idx_ret[down_mask]))
        else:
            features["capture_down"] = 1.0
    else:
        features["capture_up"] = 1.0
        features["capture_down"] = 1.0
    # bias_60d 改为指数乖离率（沪深300 close vs MA60），表征大盘超跌反弹环境
    if len(idx_rows) >= 60:
        idx_ma60 = np.mean(idx_closes[-60:])
        features["bias_60d"] = float((idx_closes[-1] - idx_ma60) / idx_ma60 * 100)
    else:
        features["bias_60d"] = 0.0
    # 数据质量校验：检测 NaN/Inf/极端值
    for key in ("hurst_60d", "momentum_20d", "calmar", "downside_vol",
                 "capture_up", "capture_down", "bias_60d"):
        v = features.get(key)
        if v is None or (isinstance(v, float) and (np.isnan(v) or np.isinf(v))):
            logger.warning("基金 %s 特征 %s 异常 (%s)，置为 0.0", code, key, v)
            features[key] = 0.0
    if abs(features.get("momentum_20d", 0)) > 30:
        logger.warning("基金 %s 20日动量异常: %.2f%%", code, features["momentum_20d"])
    if abs(features.get("bias_60d", 0)) > 25:
        logger.warning("基金 %s 60日偏离度异常: %.2f%%", code, features["bias_60d"])

    return features


def calc_all_features(batch_commit: int = 500) -> int:
    from data_store import _db_conn

    with _db_conn() as conn:
        all_codes = [
            r[0] for r in conn.execute(
                "SELECT code FROM fund_basic WHERE is_buyable = 1"
            ).fetchall()
        ]
        total = len(all_codes)
        rbsa_data: dict[str, list[dict]] = {}
        cur_r = conn.execute(
            "SELECT code, stock_code, stock_name, weight FROM fund_holdings "
            "WHERE report_date IN "
            "(SELECT MAX(report_date) FROM fund_holdings GROUP BY code)"
        )
        _rbsa_buf: dict[str, list[dict]] = {}
        for code, sc, sn, w in cur_r.fetchall():
            _rbsa_buf.setdefault(code, []).append({"stock_code": sc, "stock_name": sn, "weight": w})
        for code, holdings in _rbsa_buf.items():
            top = calc_rbsa(holdings, conn)
            if top:
                rbsa_data[code] = top
        logger.info("RBSA 预加载完成: %d 只基金有行业暴露", len(rbsa_data))
        # 大盘状态机：沪深300 close vs MA60 → BULL/BEAR
        regime = "NEUTRAL"
        idx_row = conn.execute(
            "SELECT close, ma60 FROM index_daily WHERE code='sh000300' ORDER BY date DESC LIMIT 1"
        ).fetchone()
        if idx_row and idx_row[0] and idx_row[1] and idx_row[1] > 0:
            regime = "BULL" if idx_row[0] > idx_row[1] else "BEAR"
        logger.info("大盘状态机: %s (close=%s, ma60=%s)", regime,
                     idx_row[0] if idx_row else None, idx_row[1] if idx_row else None)
        feature_dates = dict(
            conn.execute("SELECT code, date FROM fund_features").fetchall()
        )
        nav_latest = dict(
            conn.execute("SELECT code, MAX(date) FROM fund_nav GROUP BY code").fetchall()
        )
        holdings_need_rbsa = set()
        cur_e = conn.execute(
            "SELECT code FROM fund_features "
            "WHERE (rbsa_industry_1 IS NULL OR rbsa_industry_1 = '' OR rbsa_industry_1 = '其他')"
            "   OR (rbsa_industry_2 IS NULL OR rbsa_industry_2 = '')"
            "   OR (rbsa_industry_3 IS NULL OR rbsa_industry_3 = '')"
        )
        for (c,) in cur_e.fetchall():
            if c in rbsa_data:
                holdings_need_rbsa.add(c)
        # 行业映射更新后，强制重算已过期RBSA
        im_row = conn.execute("SELECT value FROM meta WHERE key = 'industry_map_updated'").fetchone()
        industry_map_date = im_row[0] if im_row else None
        if industry_map_date:
            stale = conn.execute(
                "SELECT code FROM fund_features WHERE date < ?", (industry_map_date,)
            ).fetchall()
            for (c,) in stale:
                if c in rbsa_data and c not in holdings_need_rbsa:
                    holdings_need_rbsa.add(c)
        skip_codes = {
            c for c in all_codes
            if c in feature_dates and c in nav_latest and feature_dates[c] >= nav_latest[c]
            and c not in holdings_need_rbsa
        }
        logger.info(
            "待计算特征基金: %d 只, 跳过已最新 %d 只, 强制重算RBSA %d 只",
            total - len(skip_codes), len(skip_codes), len(holdings_need_rbsa),
        )
        done = 0
        saved = 0
        start_time = time.monotonic()
        for code in all_codes:
            if code in skip_codes:
                done += 1
                continue
            features = calc_features(code, conn)
            done += 1
            if features:
                top = rbsa_data.get(code, [])
                rbsa_industry_1 = top[0]["industry"] if len(top) > 0 else ""
                rbsa_weight_1 = top[0]["weight"] if len(top) > 0 else 0.0
                rbsa_industry_2 = top[1]["industry"] if len(top) > 1 else ""
                rbsa_weight_2 = top[1]["weight"] if len(top) > 1 else 0.0
                rbsa_industry_3 = top[2]["industry"] if len(top) > 2 else ""
                rbsa_weight_3 = top[2]["weight"] if len(top) > 2 else 0.0
                conn.execute(
                    "INSERT OR REPLACE INTO fund_features "
                    "(code, date, regime, hurst_60d, momentum_20d, calmar, downside_vol, "
                    "capture_up, capture_down, bias_60d, "
                    "rbsa_industry_1, rbsa_weight_1, "
                    "rbsa_industry_2, rbsa_weight_2, "
                    "rbsa_industry_3, rbsa_weight_3) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        features["code"], features["date"], regime,
                        features.get("hurst_60d"), features.get("momentum_20d"),
                        features.get("calmar"), features.get("downside_vol"),
                        features.get("capture_up"), features.get("capture_down"),
                        features.get("bias_60d"),
                        rbsa_industry_1, rbsa_weight_1,
                        rbsa_industry_2, rbsa_weight_2,
                        rbsa_industry_3, rbsa_weight_3,
                    ),
                )
                saved += 1
            if saved % batch_commit == 0:
                conn.commit()
                elapsed = time.monotonic() - start_time
                speed = done / elapsed if elapsed > 0 else 0
                logger.info("特征计算进度: %d/%d, speed=%.1f/s", done, total, speed)
    elapsed = time.monotonic() - start_time
    logger.info("特征计算完成: %d/%d 只基金入库, 耗时 %.1f 秒", saved, total, elapsed)
    return saved

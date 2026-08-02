"""特征计算模块：Hurst、动量、卡玛、RBSA、大盘状态机。"""

from app.utils.log import get_logger
import time

import numpy as np
import pandas as pd

import app.repo as repo

logger = get_logger("features")

_FEATURE_RETENTION_ROWS = 250
"""fund_features 每只基金保留的特征快照行数（与净值保留窗口一致，覆盖监控风格漂移的历史查询）。"""


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


def compute_fund_features(navs: np.ndarray, idx_closes: np.ndarray,
                          idx_volumes: np.ndarray) -> dict | None:
    """从净值+指数数组计算 7 个特征（纯函数，不触碰 DB；数据不足返回 None）。

    特征公式单一来源：calc_features / 训练样本 / 回测均复用，避免多套公式漂移。
    """
    if len(navs) < 60:
        return None
    with np.errstate(divide="ignore", invalid="ignore"):
        returns = np.diff(navs) / navs[:-1]
    returns = returns[np.isfinite(returns)]

    feat: dict = {}
    window = min(60, len(returns))
    feat["hurst_60d"] = float(calc_hurst(returns[-window:]))
    feat["momentum_20d"] = float((navs[-1] / navs[-20] - 1) * 100) if len(navs) >= 20 else 0.0

    if len(navs) >= 60:
        cum = navs[-60:] / navs[-60]
        peak = np.maximum.accumulate(cum)
        dd = (cum - peak) / peak
        max_dd = float(np.min(dd))
        ann = float((navs[-1] / navs[-60] - 1) * 252 / 60)
        feat["calmar"] = ann / abs(max_dd) if abs(max_dd) > 1e-10 else 0.0
    else:
        feat["calmar"] = 0.0

    if len(returns) >= 20:
        neg = returns[-20:][returns[-20:] < 0]
        feat["downside_vol"] = float(np.std(neg) * np.sqrt(252)) if len(neg) > 0 else 0.0
    else:
        feat["downside_vol"] = 0.0

    if len(idx_closes) >= 60 and len(returns) >= 60:
        idx_ret = np.diff(idx_closes) / idx_closes[:-1]
        idx_ret = idx_ret[np.isfinite(idx_ret)]
        m = min(60, len(returns), len(idx_ret))
        fr, ir = returns[-m:], idx_ret[-m:]
        up, down = ir > 0, ir < 0
        feat["capture_up"] = float(np.mean(fr[up]) / np.mean(ir[up])) if up.sum() > 0 else 1.0
        feat["capture_down"] = float(np.mean(fr[down]) / np.mean(ir[down])) if down.sum() > 0 else 1.0
    else:
        feat["capture_up"] = feat["capture_down"] = 1.0

    if len(idx_closes) >= 60:
        idx_ma60 = np.mean(idx_closes[-60:])
        feat["bias_60d"] = float((idx_closes[-1] - idx_ma60) / idx_ma60 * 100)
    else:
        feat["bias_60d"] = 0.0
    return feat


def combo_score(score_norm, rel_strength, calmar, hurst, w,
                sector_rel_momentum=0.0, sector_rel_calmar=0.0,
                rbsa_weight=0.0):
    """组合打分公式单一来源（主路径/降级路径/回测共用）。

    主路径传 sector_rel + rbsa_weight；降级与回测路径缺失的数据按 0 处理。
    """
    return (score_norm * w["model"]
            + rel_strength * w["rs"]
            + sector_rel_momentum * 0.15
            + calmar * w["cal"]
            + sector_rel_calmar * 0.05
            + (hurst - 0.5) * 10 * w["hurst"]
            + rbsa_weight * 0.003)


def regime_combo_weights(regime: str, cfg: dict) -> dict:
    """根据大盘状态调整因子权重：BULL 偏动量+赫斯特，BEAR 偏卡玛。"""
    w_model = cfg["model_weight"]
    w_rs = cfg["rel_strength_weight"]
    w_cal = cfg["calmar_weight"]
    w_hurst = cfg["hurst_weight"]
    if regime == "BULL":
        w_rs *= 1.3
        w_hurst *= 1.3
        w_cal *= 0.5
    elif regime == "BEAR":
        w_cal *= 1.5
        w_rs *= 0.7
        w_hurst *= 0.5
    return {"model": w_model, "rs": w_rs, "cal": w_cal, "hurst": w_hurst}


def score_frame(df: pd.DataFrame, model, cfg: dict, idx_mom: float, *,
                default_regime: str = "NEUTRAL",
                rbsa_weight_col: str | None = None,
                sector_rel_momentum_col: str | None = None,
                sector_rel_calmar_col: str | None = None) -> pd.DataFrame:
    """对特征 DataFrame 统一打分：预测 → 相对化 → 归一化 → combo。

    主路径 / 降级路径 / 回测共用。model 为 None 时 score_norm 取 0.5（回测无模型场景）。
    行内已有 regime 列时优先使用，否则回退 default_regime。
    """
    df = df.copy()
    if model is not None:
        X = df[repo.FEATURE_COLS].astype(float)
        df["score"] = model.predict(X)
        df = df[np.isfinite(df["score"])]
        s_min, s_max = df["score"].min(), df["score"].max()
        s_range = s_max - s_min if s_max > s_min else 1.0
        df["score_norm"] = (df["score"] - s_min) / s_range
    else:
        df["score_norm"] = 0.5
    df["rel_strength"] = df["momentum_20d"] - idx_mom
    calmar_clipped = df["calmar"].clip(-5, 5)
    if "regime" in df.columns and len(df) > 0 and pd.notna(df["regime"].iloc[0]):
        regime = df["regime"].iloc[0]
    else:
        regime = default_regime
    w = regime_combo_weights(regime, cfg)
    df["combo"] = combo_score(
        df["score_norm"], df["rel_strength"], calmar_clipped, df["hurst_60d"], w,
        sector_rel_momentum=df[sector_rel_momentum_col] if sector_rel_momentum_col else 0.0,
        sector_rel_calmar=df[sector_rel_calmar_col] if sector_rel_calmar_col else 0.0,
        rbsa_weight=df[rbsa_weight_col] if rbsa_weight_col else 0.0,
    )
    return df


def calc_rbsa(holdings: list[dict], industry_map: dict[str, str] | None = None) -> list[dict]:
    """按持仓权重聚合前 3 大行业暴露。

    industry_map 为预加载的 stock_code→industry_name 映射（由 calc_all_features 一次性载入，
    避免逐持仓查询）。
    """
    industry_weights: dict[str, float] = {}
    for h in holdings:
        stock_code = h["stock_code"]
        industry = (industry_map or {}).get(stock_code) or "其他"
        industry_weights[industry] = industry_weights.get(industry, 0) + h["weight"]
    sorted_industries = sorted(industry_weights.items(), key=lambda x: x[1], reverse=True)
    return [{"industry": ind, "weight": w} for ind, w in sorted_industries[:3]]


def calc_features(code: str, conn=None,
                  idx_closes: np.ndarray | None = None,
                  idx_volumes: np.ndarray | None = None) -> dict:
    rows = repo.get_fund_nav_rows(code, conn)
    if len(rows) < 60:
        logger.warning("基金 %s 净值数据不足 (%d 天)，跳过特征计算", code, len(rows))
        return {}
    dates = [r[0] for r in rows]
    navs = np.array([r[1] for r in rows], dtype=float)
    if idx_closes is None:
        idx_rows = repo.get_index_rows(conn=conn)
        idx_volumes = np.array([r[2] for r in idx_rows], dtype=float) if idx_rows else np.array([])
        idx_closes = np.array([r[1] for r in idx_rows], dtype=float) if idx_rows else np.array([])
    feat = compute_fund_features(navs, idx_closes, idx_volumes)
    if feat is None:
        return {}
    features: dict = {"code": code, "date": dates[-1]}
    features.update(feat)
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
    from app.database import db_conn

    with db_conn() as conn:
        all_codes = repo.get_buyable_codes()
        total = len(all_codes)
        # 预加载全局不变的数据，避免逐基金/逐持仓重复查询（N+1）
        industry_map = repo.get_industry_map()
        idx_rows = repo.get_index_rows(conn=conn)
        idx_volumes = np.array([r[2] for r in idx_rows], dtype=float) if idx_rows else np.array([])
        idx_closes = np.array([r[1] for r in idx_rows], dtype=float) if idx_rows else np.array([])
        rbsa_data: dict[str, list[dict]] = {}
        _rbsa_buf: dict[str, list[dict]] = {}
        for code, sc, sn, w in repo.get_latest_holdings_rows():
            _rbsa_buf.setdefault(code, []).append({"stock_code": sc, "stock_name": sn, "weight": w})
        for code, holdings in _rbsa_buf.items():
            top = calc_rbsa(holdings, industry_map)
            if top:
                rbsa_data[code] = top
        logger.info("RBSA 预加载完成: %d 只基金有行业暴露", len(rbsa_data))
        # 大盘状态机：沪深300 close vs MA60 → BULL/BEAR（repo 单一来源）
        regime = repo.get_market_regime()
        logger.info("大盘状态机: %s", regime)
        feature_dates = repo.get_feature_dates_map()
        nav_latest = repo.get_nav_latest_dates()
        holdings_need_rbsa = set()
        for c in repo.get_codes_missing_rbsa():
            if c in rbsa_data:
                holdings_need_rbsa.add(c)
        # 行业映射更新后，强制重算已过期RBSA
        industry_map_date = repo.get_meta("industry_map_updated")
        if industry_map_date:
            for c in repo.get_feature_codes_before(industry_map_date):
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
            features = calc_features(code, conn, idx_closes, idx_volumes)
            done += 1
            if features:
                top = rbsa_data.get(code, [])
                features["regime"] = regime
                features["rbsa_industry_1"] = top[0]["industry"] if len(top) > 0 else ""
                features["rbsa_weight_1"] = top[0]["weight"] if len(top) > 0 else 0.0
                features["rbsa_industry_2"] = top[1]["industry"] if len(top) > 1 else ""
                features["rbsa_weight_2"] = top[1]["weight"] if len(top) > 1 else 0.0
                features["rbsa_industry_3"] = top[2]["industry"] if len(top) > 2 else ""
                features["rbsa_weight_3"] = top[2]["weight"] if len(top) > 2 else 0.0
                repo.save_fund_features(conn, features)
                saved += 1
            if saved % batch_commit == 0:
                conn.commit()
                elapsed = time.monotonic() - start_time
                speed = done / elapsed if elapsed > 0 else 0
                logger.info("特征计算进度: %d/%d, speed=%.1f/s", done, total, speed)
        # 修剪：每只基金仅保留最近 N 行特征快照，防止历史快照无限累积
        repo.trim_fund_features(conn, _FEATURE_RETENTION_ROWS)
        conn.commit()
    elapsed = time.monotonic() - start_time
    logger.info("特征计算完成: %d/%d 只基金入库, 耗时 %.1f 秒", saved, total, elapsed)
    return saved

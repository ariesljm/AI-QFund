"""推荐引擎：LLM 选赛道 → LightGBM 赛道内排 → LLM 定论（Phase 2 重构）。

漏斗：准备标注数据 → 训练 LightGBM → 宏观LLM选赛道
      → 赛道内相对化排序 → 持仓+新闻交叉验证 → LLM终选定论 → 入库。

依赖 data_foundation 的 DB 连接与特征计算结果（fund_features 表）。
运行：uv run python recommend.py
"""

import json
import logging
from app.utils.log import get_logger
import sqlite3
from datetime import datetime
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

from app.features.calculator import calc_hurst
from app.llm.macro_agent import build_macro_context, MacroContext
from app.database import db_conn, get_db as _get_db
from app.data.nav import fetch_fund_nav_incremental
from app.llm.client import call_llm
from app.llm.prompts import final_pick_prompt, final_pick_system_prompt

logger = get_logger("recommend")

MODEL_PATH = Path("models/lgb_model.txt")
FEATURE_COLS = [
    "hurst_60d", "momentum_20d", "calmar", "downside_vol",
    "capture_up", "capture_down", "bias_60d",
]
_FORWARD_WINDOW = 20


def _load_ranking_cfg() -> dict:
    """从 meta 表读取排序权重，找不到则用默认值。"""
    defaults = {
        "model_weight": 0.5,
        "rel_strength_weight": 0.15,
        "calmar_weight": 0.1,
        "hurst_weight": 0.1,
        "momentum_guard_pct": -15.0,
    }
    try:
        with db_conn() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'ranking_cfg'"
            ).fetchone()
            if row:
                import json
                cfg = json.loads(row[0])
                defaults.update({k: v for k, v in cfg.items() if k in defaults})
    except Exception:
        pass
    return defaults


# ========== 2.1 标注数据准备 ==========

def _features_from_window(navs: np.ndarray, idx_closes: np.ndarray,
                          idx_volumes: np.ndarray) -> dict | None:
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

    # bias_60d 改为指数乖离率（沪深300 close vs MA60）
    if len(idx_closes) >= 60:
        feat["bias_60d"] = float((idx_closes[-1] - np.mean(idx_closes[-60:])) / np.mean(idx_closes[-60:]) * 100)
    else:
        feat["bias_60d"] = 0.0
    feat["rbsa_weight_1"] = 0.0
    return feat


_MAX_TRAIN_FUNDS = 2000
"""ponytail: 全量12K基金特征计算太慢，限2000只代表性样本训练。"""


def prepare_lgb_training_data() -> tuple[pd.DataFrame, pd.Series, pd.DataFrame, pd.Series]:
    """面板样本 + walk-forward 时间划分，返回 (X_train, y_train, X_val, y_val)。

    ponytail: 不预加载全库NAV（14M行→51s），改用逐基金SQL流式处理，
    并限制基金数避免训练过慢。
    """
    with db_conn() as conn:
        idx_rows = conn.execute(
            "SELECT date, close, volume FROM index_daily WHERE code = 'sh000300' ORDER BY date ASC"
        ).fetchall()
        if not idx_rows:
            raise RuntimeError("沪深300指数数据缺失，无法准备训练数据")
        idx_df = pd.DataFrame(idx_rows, columns=["date", "close", "volume"])
        idx_df["date"] = pd.to_datetime(idx_df["date"])
        idx_df = idx_df.set_index("date").sort_index()
        idx_close = idx_df["close"]
        idx_vol = idx_df["volume"]
        idx_ret_fwd = idx_close.shift(-_FORWARD_WINDOW) / idx_close - 1.0

        fund_codes = [
            r[0] for r in conn.execute(
                "SELECT code FROM fund_nav GROUP BY code "
                "HAVING COUNT(*) >= ? ORDER BY RANDOM() LIMIT ?",
                (60 + _FORWARD_WINDOW, _MAX_TRAIN_FUNDS),
            ).fetchall()
        ]
        if not fund_codes:
            logger.warning("训练集为空")
            empty = pd.DataFrame(columns=FEATURE_COLS)
            return empty, pd.Series(dtype=float, name="alpha_20d"), empty, pd.Series(dtype=float, name="alpha_20d")

        # 面板采样：每只基金沿时间轴每 _STEP 天取一个样本
        _STEP = 20
        samples = []
        for code in fund_codes:
            rows = conn.execute(
                "SELECT date, cum_nav FROM fund_nav WHERE code=? ORDER BY date ASC",
                (code,),
            ).fetchall()
            dates = [pd.Timestamp(r[0]) for r in rows]
            navs = [r[1] for r in rows]
            if len(dates) < 60 + _FORWARD_WINDOW:
                continue
            navs_arr = np.array(navs, dtype=float)
            max_pos = len(dates) - 1 - _FORWARD_WINDOW
            for pos in range(60, max_pos + 1, _STEP):
                d = dates[pos]
                if d not in idx_ret_fwd.index or pd.isna(idx_ret_fwd[d]):
                    continue
                y = navs_arr[pos + _FORWARD_WINDOW] / navs_arr[pos] - 1.0 - idx_ret_fwd[d]
                idx_pos = idx_close.index.get_indexer([d])[0]
                if idx_pos < 0 or idx_pos < 60:
                    continue
                idx_closes_w = idx_close.iloc[idx_pos - 59: idx_pos + 1].to_numpy(dtype=float)
                idx_vols_w = idx_vol.iloc[idx_pos - 59: idx_pos + 1].to_numpy(dtype=float)
                feat = _features_from_window(navs_arr[:pos + 1], idx_closes_w, idx_vols_w)
                if feat is None or any(pd.isna(v) for v in feat.values()):
                    continue
                samples.append((d, feat, y))

    if not samples:
        logger.warning("训练集为空")
        empty = pd.DataFrame(columns=FEATURE_COLS)
        return empty, pd.Series(dtype=float, name="alpha_20d"), empty, pd.Series(dtype=float, name="alpha_20d")

    # walk-forward：按时间排序，最后 20% 样本作验证集
    samples.sort(key=lambda x: x[0])
    split_idx = int(len(samples) * 0.8)
    train_s, val_s = samples[:split_idx], samples[split_idx:]

    X_train = pd.DataFrame([s[1] for s in train_s], columns=FEATURE_COLS)
    y_train = pd.Series([s[2] for s in train_s], name="alpha_20d")
    X_val = pd.DataFrame([s[1] for s in val_s], columns=FEATURE_COLS)
    y_val = pd.Series([s[2] for s in val_s], name="alpha_20d")

    logger.info("训练集构建完成: %d只基金, 训练 %d 条, 验证 %d 条, 特征 %d 维",
                len(fund_codes), len(X_train), len(X_val), len(FEATURE_COLS))
    return X_train, y_train, X_val, y_val


def train_lgb_model(X_train: pd.DataFrame, y_train: pd.Series,
                    X_val: pd.DataFrame | None = None,
                    y_val: pd.Series | None = None) -> lgb.Booster:
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    params = {
        "objective": "regression", "metric": "rmse",
        "learning_rate": 0.05, "num_leaves": 31,
        "min_data_in_leaf": 20, "feature_fraction": 0.9,
        "verbose": -1, "seed": 42,
    }
    train_data = lgb.Dataset(X_train, label=y_train)
    if X_val is not None and len(X_val) > 0 and y_val is not None and len(y_val) > 0:
        val_data = lgb.Dataset(X_val, label=y_val, reference=train_data)
        booster = lgb.train(
            params, train_data, num_boost_round=500,
            valid_sets=[val_data],
            callbacks=[lgb.early_stopping(stopping_rounds=50, verbose=False)],
        )
    else:
        booster = lgb.train(params, train_data, num_boost_round=200)
    booster.save_model(str(MODEL_PATH))
    logger.info("LightGBM 模型已保存: %s (best_iter=%s)", MODEL_PATH, booster.best_iteration)
    return booster


# ========== 赛道内排序 ==========

# LLM行业名 → RBSA行业名 精简映射（仅保留高频命名差异，LLM已通过prompt自行翻译）
_SECTOR_ALIASES: dict[str, str] = {
    "风电设备": "电源设备",
    "光伏": "电源设备",
    "光伏设备": "电源设备",
    "军工": "航空航天装备",
    "军工装备": "航空航天装备",
    "军工电子": "航空航天装备",
    "白酒": "饮料",
    "证券": "非银行金融",
    "券商": "非银行金融",
    "油气": "石油天然气",
    "工业金属": "基本金属",
    "电力设备": "输变电设备",
    "芯片": "半导体",
    "芯片设计": "半导体",
    "存储芯片": "半导体",
    "半导体设备": "半导体",
    "半导体材料": "半导体",
}


def _match_one_sector(ideal: str, candidates: list[str]) -> str | None:
    """把 LLM 选的行业名匹配到 RBSA 行业名（精确匹配 + 别名 + 子串，不模糊匹配）。"""
    normalized = _SECTOR_ALIASES.get(ideal, ideal)
    ideal_lower = normalized.lower()
    # 1. 精确匹配
    for c in candidates:
        if c and c.lower() == ideal_lower:
            return c
    # 2. candidate 包含 ideal（如 ideal="食品" candidate="食品饮料"）
    for c in candidates:
        if c and ideal_lower in c.lower():
            return c
    # 3. ideal 包含 candidate（如 ideal="石油天然气" candidate="天然气"）
    for c in candidates:
        if c and c.lower() in ideal_lower:
            return c
    return None


def _resolve_sectors(sectors: list[str]) -> list[str]:
    """把 LLM 选的行业名匹配到 RBSA 表中存在的行业名。"""
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT rbsa_industry_1 FROM fund_features "
            "WHERE rbsa_industry_1 IS NOT NULL AND rbsa_industry_1 != ''"
        ).fetchall()
    candidates = [r[0] for r in rows]
    resolved = []
    for s in sectors:
        matched = _match_one_sector(s, candidates)
        if matched:
            resolved.append(matched)
        else:
            logger.info("赛道 %s 未匹配到RBSA行业，跳过", s)
    return list(dict.fromkeys(resolved))  # 去重保留顺序

def _index_momentum() -> float:
    with db_conn() as conn:
        idx = conn.execute(
            "SELECT close FROM index_daily WHERE code='sh000300' ORDER BY date DESC LIMIT 21"
        ).fetchall()
    return (idx[0][0] / idx[-1][0] - 1) * 100 if len(idx) >= 21 else 0.0


def _get_market_regime() -> str:
    """从沪深300收盘价 vs MA60 判断大盘状态：BULL/BEAR。"""
    with db_conn() as conn:
        row = conn.execute(
            "SELECT close, ma60 FROM index_daily WHERE code='sh000300' "
            "AND close IS NOT NULL AND ma60 IS NOT NULL "
            "ORDER BY date DESC LIMIT 1"
        ).fetchone()
    if not row or not row[0] or not row[1] or row[1] <= 0:
        return "NEUTRAL"
    return "BULL" if row[0] > row[1] else "BEAR"


def _regime_combo_weights(regime: str, cfg: dict) -> dict:
    """根据大盘状态调整因子权重：BULL 偏动量+赫斯特，BEAR 偏卡玛。"""
    w_model = cfg["model_weight"]
    w_rs = cfg["rel_strength_weight"]
    w_cal = cfg["calmar_weight"]
    w_hurst = cfg["hurst_weight"]
    if regime == "BULL":
        w_rs *= 1.3; w_hurst *= 1.3; w_cal *= 0.5
    elif regime == "BEAR":
        w_cal *= 1.5; w_rs *= 0.7; w_hurst *= 0.5
    return {"model": w_model, "rs": w_rs, "cal": w_cal, "hurst": w_hurst}


def _add_sector_relatives(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["sector_rel_momentum"] = 0.0
    df["sector_rel_calmar"] = 0.0
    for _, g in df.groupby("sector"):
        if len(g) >= 2:
            mel_mom = g["momentum_20d"].median()
            mel_cal = g["calmar"].clip(-5, 5).median()
            df.loc[g.index, "sector_rel_momentum"] = g["momentum_20d"] - mel_mom
            df.loc[g.index, "sector_rel_calmar"] = g["calmar"] - mel_cal
    return df


def _rank_within_sectors(ctx: MacroContext, model: lgb.Booster) -> list[dict]:
    """在 LLM 选中的赛道内，用赛道相对化特征排序，每赛道取 Top 1-2，返回 3-5 只候选。"""
    raw_sectors = ctx.recommended_sectors
    if not raw_sectors:
        logger.info("无指定赛道，降级为全市场 Top 10")
        return rank_funds(model)

    sectors = _resolve_sectors(raw_sectors)
    if not sectors:
        logger.info("所有赛道均未匹配RBSA行业，降级为全市场 Top 10")
        return rank_funds(model)
    logger.info("LLM赛道 %s → 匹配到 %s", raw_sectors, sectors)
    risk_sectors = _resolve_sectors(ctx.risk_sectors)

    with db_conn() as conn:
        placeholders = ",".join("?" * len(sectors))
        rows = conn.execute(
            f"SELECT ff.code, fb.name, ff.regime, "
            f"ff.rbsa_industry_1, ff.rbsa_weight_1, "
            f"ff.rbsa_industry_2, ff.rbsa_weight_2, "
            f"ff.rbsa_industry_3, ff.rbsa_weight_3, "
            f"{', '.join('ff.' + c for c in FEATURE_COLS)} "
            f"FROM fund_features ff "
            f"JOIN fund_basic fb ON fb.code = ff.code "
            f"WHERE fb.is_buyable = 1 "
            f"AND (ff.rbsa_industry_1 IN ({placeholders}) "
            f"  OR ff.rbsa_industry_2 IN ({placeholders}) "
            f"  OR ff.rbsa_industry_3 IN ({placeholders}))",
            sectors + sectors + sectors,
        ).fetchall()

        if not rows:
            logger.info("赛道内无匹配基金，降级为全市场 Top 10")
            return rank_funds(model)

        cols = ["code", "name", "regime",
                "rbsa_industry_1", "rbsa_weight_1",
                "rbsa_industry_2", "rbsa_weight_2",
                "rbsa_industry_3", "rbsa_weight_3"] + FEATURE_COLS
        df = pd.DataFrame(rows, columns=cols)
        df = df.dropna(subset=FEATURE_COLS)
        if df.empty:
            return rank_funds(model)

    # 展开多行业：一只基金如有多个行业匹配赛道，则重复出现
    expanded = []
    for _, r in df.iterrows():
        for i in range(1, 4):
            ind = r.get(f"rbsa_industry_{i}")
            if ind and ind in sectors:
                row = r.to_dict()
                row["sector"] = ind
                row["rbsa_weight"] = r.get(f"rbsa_weight_{i}", 0) or 0
                expanded.append(row)
    if not expanded:
        logger.info("所有基金均无匹配行业，降级为全市场 Top 10")
        return rank_funds(model)
    df = pd.DataFrame(expanded)

    df = df[~df["sector"].isin(risk_sectors)]
    df = _add_sector_relatives(df)
    cfg = _load_ranking_cfg()
    df = df[df["momentum_20d"] >= cfg["momentum_guard_pct"]]

    X = df[FEATURE_COLS].astype(float)
    df["score"] = model.predict(X)
    df = df[np.isfinite(df["score"])]

    idx_mom = _index_momentum()
    df["rel_strength"] = df["momentum_20d"] - idx_mom
    calmar_clipped = df["calmar"].clip(-5, 5)
    score_min, score_max = df["score"].min(), df["score"].max()
    score_range = score_max - score_min if score_max > score_min else 1.0
    df["score_norm"] = (df["score"] - score_min) / score_range
    regime = df["regime"].iloc[0] if "regime" in df.columns and len(df) > 0 and pd.notna(df["regime"].iloc[0]) else _get_market_regime()
    w = _regime_combo_weights(regime, cfg)
    df["combo"] = (
        df["score_norm"] * w["model"]
        + df["rel_strength"] * w["rs"]
        + df["sector_rel_momentum"] * 0.15
        + calmar_clipped * w["cal"]
        + df["sector_rel_calmar"] * 0.05
        + (df["hurst_60d"] - 0.5) * 10 * w["hurst"]
        + df["rbsa_weight"] * 0.003
    )

    top_per_sector = []
    for sector in sectors:
        sdf = df[df["sector"] == sector].sort_values("combo", ascending=False)
        top_per_sector.extend(sdf.head(2).to_dict("records"))

    top_per_sector = sorted(top_per_sector, key=lambda x: x["combo"], reverse=True)[:5]
    if not top_per_sector:
        return rank_funds(model)

    results = []
    for f in top_per_sector:
        results.append({
            "code": f["code"], "name": f["name"],
            "sector": f["sector"],
            "rbsa_industry_1": f.get("rbsa_industry_1", ""),
            "rbsa_industry_2": f.get("rbsa_industry_2", ""),
            "rbsa_industry_3": f.get("rbsa_industry_3", ""),
            "rbsa_weight_1": float(f.get("rbsa_weight_1", 0) or 0),
            "rbsa_weight_2": float(f.get("rbsa_weight_2", 0) or 0),
            "rbsa_weight_3": float(f.get("rbsa_weight_3", 0) or 0),
            "score": float(f["score"]), "combo": float(f["combo"]),
            "hurst_60d": float(f["hurst_60d"]), "momentum_20d": float(f["momentum_20d"]),
            "calmar": float(f["calmar"]),
            "sector_rel_momentum": round(float(f.get("sector_rel_momentum", 0)), 1),
            "sector_rel_calmar": round(float(f.get("sector_rel_calmar", 0)), 1),
        })
    return results


def rank_funds(model: lgb.Booster) -> list[dict]:
    """全市场排名（降级备选），返回 Top 10。"""
    cfg = _load_ranking_cfg()
    guard = cfg["momentum_guard_pct"]
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT ff.code, fb.name, ff.regime, ff.rbsa_industry_1, ff.rbsa_weight_1, "
            f"{', '.join('ff.' + c for c in FEATURE_COLS)} "
            "FROM fund_features ff "
            "JOIN fund_basic fb ON fb.code = ff.code "
            "WHERE fb.is_buyable = 1 "
            "AND ff.rbsa_industry_1 IS NOT NULL AND ff.rbsa_industry_1 != ''"
        ).fetchall()

    cols = ["code", "name", "regime", "rbsa_industry_1", "rbsa_weight_1"] + FEATURE_COLS
    df = pd.DataFrame(rows, columns=cols)
    df = df.dropna(subset=FEATURE_COLS)
    if df.empty:
        return []

    X = df[FEATURE_COLS].astype(float)
    df = df.copy()
    df["score"] = model.predict(X)
    df = df[np.isfinite(df["score"])]

    idx_mom = _index_momentum()
    df["rel_strength"] = df["momentum_20d"] - idx_mom
    df = df[df["momentum_20d"] >= guard]
    calmar_clipped = df["calmar"].clip(-5, 5)
    score_min, score_max = df["score"].min(), df["score"].max()
    score_range = score_max - score_min if score_max > score_min else 1.0
    df["score_norm"] = (df["score"] - score_min) / score_range
    regime = df["regime"].iloc[0] if "regime" in df.columns and len(df) > 0 and pd.notna(df["regime"].iloc[0]) else _get_market_regime()
    w = _regime_combo_weights(regime, cfg)
    df["combo"] = (
        df["score_norm"] * w["model"]
        + df["rel_strength"] * w["rs"]
        + calmar_clipped * w["cal"]
        + (df["hurst_60d"] - 0.5) * 10 * w["hurst"]
        + df["rbsa_weight_1"] * 0.003
    )
    top = df.sort_values("combo", ascending=False).head(10)
    candidates = []
    for _, r in top.iterrows():
        candidates.append({
            "code": r["code"], "name": r["name"], "regime": r["regime"],
            "score": float(r["score"]), "combo": float(r["combo"]),
            "hurst_60d": float(r["hurst_60d"]), "momentum_20d": float(r["momentum_20d"]),
            "calmar": float(r["calmar"]),
            "rbsa_industry_1": r.get("rbsa_industry_1", ""),
            "rbsa_weight_1": float(r.get("rbsa_weight_1", 0.0) or 0.0),
        })
    return candidates


# ========== LLM 最终定论 ==========

def _load_insights(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT insight FROM evolution_insights "
        "WHERE active = 1 AND confidence > 0.3 "
        "ORDER BY created_date DESC LIMIT 8"
    ).fetchall()
    return [r[0] for r in rows]



def _llm_final_pick(candidates: list[dict], ctx: MacroContext, insights: list) -> dict:
    """LLM 基于重仓股+CLS新闻匹配+持仓时效性做最终选择，返回选定基金和否决记录。"""
    with db_conn() as conn:
        latest_feature_date = conn.execute(
            "SELECT MAX(date) FROM fund_features"
        ).fetchone()[0]

        for c in candidates:
            hold_rows = conn.execute(
                "SELECT h.stock_code, h.stock_name, h.weight, "
                "COALESCE(s.industry_name, '其他') "
                "FROM fund_holdings h "
                "LEFT JOIN stock_industry_map s ON h.stock_code = s.stock_code "
                "WHERE h.code = ? "
                "AND h.report_date = (SELECT MAX(report_date) FROM fund_holdings WHERE code = ?) "
                "ORDER BY h.weight DESC LIMIT 5",
                (c["code"], c["code"]),
            ).fetchall()
            c["holdings"] = [
                {"stock_code": r[0], "stock_name": r[1], "weight": r[2], "industry": r[3]}
                for r in hold_rows
            ]
            matched = []
            for h in c["holdings"]:
                for s in ctx.cls_stock_mentions:
                    if s["code"] == h["stock_code"] or s["name"] == h["stock_name"]:
                        matched.append({"stock_name": h["stock_name"], "stock_code": h["stock_code"], **s})
                        break
            c["matched_news"] = matched

            report_row = conn.execute(
                "SELECT MAX(report_date) FROM fund_holdings WHERE code = ?",
                (c["code"],),
            ).fetchone()
            report_date = report_row[0] if report_row else None
            c["report_date"] = report_date
            if report_date:
                try:
                    rd = datetime.strptime(report_date, "%Y-%m-%d")
                    months = (datetime.now().year - rd.year) * 12 + (datetime.now().month - rd.month)
                    c["holdings_months"] = max(0, months)
                except Exception:
                    c["holdings_months"] = None
            else:
                c["holdings_months"] = None

            sector = c.get("sector") or c.get("rbsa_industry_1", "")
            fund_mom = c.get("momentum_20d", 0) or 0
            if sector and latest_feature_date:
                peer_rows = conn.execute(
                    "SELECT momentum_20d FROM fund_features "
                    "WHERE rbsa_industry_1 = ? AND date = ? AND momentum_20d IS NOT NULL",
                    (sector, latest_feature_date),
                ).fetchall()
                if len(peer_rows) >= 3:
                    values = sorted(r[0] for r in peer_rows)
                    n = len(values)
                    median = values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2
                    c["sector_median_mom"] = round(float(median), 1)
                    c["mom_gap"] = round(float(fund_mom) - float(median), 1)
                else:
                    c["sector_median_mom"] = None
                    c["mom_gap"] = None
            else:
                c["sector_median_mom"] = None
                c["mom_gap"] = None

    prompt = final_pick_prompt(candidates, ctx, insights)
    system_prompt = final_pick_system_prompt()

    content = call_llm(prompt, system_prompt=system_prompt, max_tokens=4096)

    if content is None:
        raise RuntimeError("LLM最终定论调用失败，无法完成基金推荐")

    valid_codes = {c["code"]: c["name"] for c in candidates}
    result = _parse_llm_result(content, valid_codes)
    if result is not None:
        return result

    raise RuntimeError(f"LLM最终定论返回无法解析: {content[:300]}")


def _parse_llm_result(content: str, valid_codes: dict) -> dict | None:
    import re
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.IGNORECASE)
    try:
        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    result = {str(k).strip(". "): v for k, v in parsed.items()}
    code = str(result.get("selected_code") or "")
    if code in valid_codes:
        return {
            "selected_code": code,
            "selected_name": result.get("selected_name", valid_codes[code]),
            "reason": result.get("reason", ""),
            "vetoed": result.get("vetoed", []),
        }
    return None


# ========== 推荐入库 ==========

_LAST_RECO_PATH = Path("data/last_recommendation.txt")


def _dump_recommendation(date_str, code, name, rank, score, regime, candidates, vetoed,
                        clear: bool = False):
    lines = [
        f"推荐日期: {date_str}", f"选定代码: {code}", f"选定名称: {name}",
        f"排名: {rank}", f"评分: {score:.4f}", f"大盘环境: {regime}",
        "", "候选:",
    ]
    for i, c in enumerate(candidates, 1):
        mark = " <-- 选定" if c["code"] == code else ""
        sector = c.get("sector", c.get("rbsa_industry_1", ""))
        lines.append(f"  {i}. {c['code']} {c['name']} [{sector}] (评分 {c['score']:.4f}){mark}")
    if vetoed:
        lines.append("")
        lines.append("LLM 否决:")
        for v in vetoed:
            lines.append(f"  - {v.get('code')} {v.get('name')}: {v.get('reason')}")
    _LAST_RECO_PATH.parent.mkdir(parents=True, exist_ok=True)
    mode = "w" if clear else "a"
    if mode == "a" and _LAST_RECO_PATH.exists():
        lines.insert(0, "---")
    with open(_LAST_RECO_PATH, mode, encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _save_recommendation(date_str: str, selected: dict, candidates: list[dict],
                           vetoed: list, regime: str, feature_snapshot: str = "",
                           clear: bool = False) -> int:
    """入库推荐记录，返回新插入行的 id。"""
    with db_conn() as conn:
        rank = next(
            (i + 1 for i, c in enumerate(candidates) if c["code"] == selected["selected_code"]), 1)
        score = next(
            (c["score"] for c in candidates if c["code"] == selected["selected_code"]), None)
        combo = next(
            (c["combo"] for c in candidates if c["code"] == selected["selected_code"]), None)
        veto_json = json.dumps(vetoed, ensure_ascii=False)
        reason = selected.get("reason", "")
        if vetoed:
            reason = reason + " | 否决记录: " + veto_json
        real_name = conn.execute(
            "SELECT name FROM fund_basic WHERE code = ?", (selected["selected_code"],)
        ).fetchone()
        real_name = real_name[0] if real_name else selected["selected_name"]
        entry_nav_row = conn.execute(
            "SELECT cum_nav FROM fund_nav WHERE code=? "
            "ORDER BY date DESC LIMIT 1",
            (selected["selected_code"],),
        ).fetchone()
        entry_nav = entry_nav_row[0] if entry_nav_row else None
        conn.execute(
            "INSERT INTO recommend_log "
            "(recommend_date, code, name, rank, score, combo, regime, buy_reason, status, feature_snapshot, entry_nav) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'HOLD', ?, ?)",
            (date_str, selected["selected_code"], real_name,
             rank, score, combo, regime, reason, feature_snapshot, entry_nav),
        )
        new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        logger.info("推荐入库: %s %s (排名%d, 分数%.4f, id=%d)",
                    selected["selected_code"], real_name, rank, score or 0.0, new_id)
    _dump_recommendation(date_str, selected["selected_code"], real_name, rank, score,
                          regime, candidates, vetoed, clear=clear)
    return new_id


def run_recommendation(retrain: bool = False, force: bool = False) -> None:
    """推荐引擎主入口：LLM 选赛道 → 赛道内排序 → LLM 定论 → 入库。

    force=True 时跳过宏观缓存，强制实时抓取新闻+LLM 重新选赛道。
    """
    date_str = datetime.now().strftime("%Y-%m-%d")
    with db_conn() as conn:
        insights = _load_insights(conn)

    if retrain or not MODEL_PATH.exists():
        logger.info("=== 准备训练数据并训练 LightGBM ===")
        X_train, y_train, X_val, y_val = prepare_lgb_training_data()
        if len(X_train) == 0:
            logger.warning("训练样本为空——NAV数据不足或数据基座未完成，跳过本次推荐")
            return
        model = train_lgb_model(X_train, y_train, X_val, y_val)
    else:
        logger.info("=== 加载已保存模型 ===")
        model = lgb.Booster(model_file=str(MODEL_PATH))

    logger.info("=== LLM 宏观分析 + 选赛道 ===")
    ctx = build_macro_context(date_str, force=force)
    llm_regime = ctx.regime_label.upper()
    llm_regime = "BULL" if llm_regime.startswith("BULL") else "BEAR" if llm_regime.startswith("BEAR") else "NEUTRAL"
    logger.info("选定赛道: %s | 回避: %s | 大盘: %s",
                ctx.recommended_sectors, ctx.risk_sectors, ctx.regime_label)

    target_sectors = ctx.recommended_sectors[:2]
    if not target_sectors:
        logger.error("LLM 未推荐任何赛道，终止")
        return
    logger.info("=== 赛道内相对化排序 ===")
    finalists = _rank_within_sectors(ctx, model)
    if not finalists:
        logger.error("无候选基金，终止推荐")
        return
    logger.info("候选 %d 只: %s",
                len(finalists),
                ", ".join(f"{f['code']}({f.get('sector','?')},combo={f['combo']:.3f})"
                          for f in finalists))

    count = 0
    for idx, sector in enumerate(target_sectors):
        sector_candidates = [
            c for c in finalists
            if c.get("sector") == sector or c.get("rbsa_industry_1") == sector
        ]
        if not sector_candidates:
            logger.warning("赛道 [%s] 无可投基金，跳过", sector)
            continue

        logger.info("=== LLM 最终定论 [%d/2 %s] (%d 只候选) ===",
                    idx + 1, sector, len(sector_candidates))
        result = _llm_final_pick(sector_candidates, ctx, insights)

        selected = {
            "selected_code": result["selected_code"],
            "selected_name": result["selected_name"],
            "reason": result.get("reason", ""),
        }
        vetoed = result.get("vetoed", [])
        logger.info("LLM 选定 [%s]: %s %s | 否决 %d 只",
                    sector, selected["selected_code"], selected["selected_name"], len(vetoed))

        guard = _load_ranking_cfg()["momentum_guard_pct"]
        sel_momentum = next(
            (float(c.get("momentum_20d", 0)) for c in sector_candidates
             if c["code"] == selected["selected_code"]), None)
        if sel_momentum is not None and sel_momentum < guard:
            logger.warning("风控拦截 [%s]: %s 近20日动量 %.1f%% 低于阈值 %.0f%%",
                           sector, selected["selected_code"], sel_momentum, guard)
            with db_conn() as conn:
                conn.execute(
                    "INSERT INTO recommend_log "
                    "(recommend_date, code, name, rank, score, combo, regime, buy_reason, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'REJECT')",
                    (date_str, selected["selected_code"], selected["selected_name"],
                     0, 0.0, 0.0, llm_regime,
                     f"风控拦截: 20日动量{sel_momentum:.1f}% 低于阈值{guard:.0f}%"),
                )
            logger.info("风控拦截已入库: %s", selected["selected_code"])
            continue

        sel_features = next(
            (c for c in sector_candidates if c["code"] == selected["selected_code"]), {})
        feature_snapshot = json.dumps({
            "sector": sel_features.get("sector", ""),
            "momentum_20d": sel_features.get("momentum_20d", 0),
            "hurst_60d": sel_features.get("hurst_60d", 0),
            "calmar": sel_features.get("calmar", 0),
            "sector_rel_momentum": sel_features.get("sector_rel_momentum", 0),
            "sector_rel_calmar": sel_features.get("sector_rel_calmar", 0),
        }, ensure_ascii=False)

        with db_conn() as conn:
            new_rows = fetch_fund_nav_incremental(selected["selected_code"], conn)
            if new_rows:
                conn.commit()
                logger.info("净值同步: %s 新增 %d 条", selected["selected_code"], new_rows)

        saved_id = _save_recommendation(
            date_str, selected, sector_candidates, vetoed, llm_regime, feature_snapshot,
            clear=(idx == 0),
        )
        _write_sector_selection(date_str, ctx, saved_id)
        count += 1

    logger.info("推荐流程完成: 赛道 %d 个 → 入库 %d 条",
                len(target_sectors), count)


def _write_sector_selection(date_str: str, ctx: MacroContext,
                            log_id: int, sector_name: str | None = None) -> None:
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO sector_selections (date, recommend_log_id, recommended_sectors, "
            "risk_sectors, sector_reasoning, regime_label) VALUES (?, ?, ?, ?, ?, ?)",
            (date_str, log_id,
             json.dumps(ctx.recommended_sectors, ensure_ascii=False),
             json.dumps(ctx.risk_sectors, ensure_ascii=False),
             ctx.sector_reasoning, ctx.regime_label),
        )


if __name__ == "__main__":
    import sys
    retrain = "--retrain" in sys.argv
    run_recommendation(retrain=retrain)

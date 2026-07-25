"""推荐引擎：LLM 选赛道 → LightGBM 赛道内排 → LLM 定论（Phase 2 重构）。

漏斗：准备标注数据 → 训练 LightGBM → 宏观LLM选赛道
      → 赛道内相对化排序 → 持仓+新闻交叉验证 → LLM终选定论 → 入库。

依赖 data_foundation 的 DB 连接与特征计算结果（fund_features 表）。
运行：uv run python recommend.py
"""

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

from features import calc_hurst
from macro_agent import build_macro_context, MacroContext
from data_store import _get_db
from data_foundation import fetch_fund_nav_incremental

logger = logging.getLogger("recommend")

MODEL_PATH = Path("models/lgb_model.txt")
FEATURE_COLS = [
    "hurst_60d", "momentum_20d", "calmar", "downside_vol",
    "capture_up", "capture_down", "bias_60d",
    "etf_flow_slope_5d",
]
_FORWARD_WINDOW = 20


def _load_ranking_cfg() -> dict:
    return {
        "rel_strength_weight": 0.6,
        "calmar_weight": 0.2,
        "hurst_weight": 0.2,
        "momentum_guard_pct": -15.0,
    }


# ========== 2.1 标注数据准备 ==========

def _features_from_window(navs: np.ndarray, idx_closes: np.ndarray,
                          idx_volumes: np.ndarray,
                          etf_volumes: np.ndarray | None = None) -> dict | None:
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

    feat["bias_60d"] = float((navs[-1] - np.mean(navs[-60:])) / np.mean(navs[-60:]) * 100)
    ev = etf_volumes if etf_volumes is not None else idx_volumes
    if len(ev) >= 5:
        vw = ev[-5:][ev[-5:] > 0]
        if len(vw) >= 2:
            feat["etf_flow_slope_5d"] = float(np.polyfit(np.arange(len(vw), dtype=float), np.log(vw), 1)[0])
        else:
            feat["etf_flow_slope_5d"] = 0.0
    else:
        feat["etf_flow_slope_5d"] = 0.0
    feat["rbsa_weight_1"] = 0.0
    return feat


def prepare_lgb_training_data() -> tuple[pd.DataFrame, pd.Series]:
    conn = _get_db()
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

    etf_rows = conn.execute(
        "SELECT date, volume FROM index_daily WHERE code = 'sh510300' ORDER BY date ASC"
    ).fetchall()
    etf_df = pd.DataFrame(etf_rows, columns=["date", "volume"]) if etf_rows else pd.DataFrame(columns=["date", "volume"])
    etf_df["date"] = pd.to_datetime(etf_df["date"])
    etf_df = etf_df.set_index("date").sort_index()
    etf_vol = etf_df["volume"]

    nav_rows = conn.execute(
        "SELECT code, date, cum_nav FROM fund_nav ORDER BY code, date ASC"
    ).fetchall()
    nav_df = pd.DataFrame(nav_rows, columns=["code", "date", "cum_nav"])
    nav_df["date"] = pd.to_datetime(nav_df["date"])
    conn.close()

    X_list, y_list = [], []
    for code, g in nav_df.groupby("code"):
        g = g.set_index("date")["cum_nav"].sort_index()
        if len(g) < 60 + _FORWARD_WINDOW:
            continue
        last_pos = len(g) - 1 - _FORWARD_WINDOW
        d = g.index[last_pos]
        d20 = g.index[last_pos + _FORWARD_WINDOW]
        fund_fwd = g[d20] / g[d] - 1.0
        if d not in idx_ret_fwd.index or pd.isna(idx_ret_fwd[d]):
            continue
        y = fund_fwd - idx_ret_fwd[d]
        idx_pos = idx_close.index.get_indexer([d])[0]
        if idx_pos < 0 or idx_pos < 60:
            continue
        idx_closes_w = idx_close.iloc[idx_pos - 59: idx_pos + 1].to_numpy(dtype=float)
        idx_vols_w = idx_vol.iloc[idx_pos - 59: idx_pos + 1].to_numpy(dtype=float)
        etf_vols_w = idx_vols_w
        if len(etf_vol) > 0:
            try:
                etf_pos = etf_vol.index.get_indexer([d])[0]
                if etf_pos >= 60:
                    etf_vols_w = etf_vol.iloc[etf_pos - 59: etf_pos + 1].to_numpy(dtype=float)
            except Exception:
                pass
        feat = _features_from_window(g.iloc[: last_pos + 1].to_numpy(dtype=float),
                                      idx_closes_w, idx_vols_w, etf_vols_w)
        if feat is None or any(pd.isna(v) for v in feat.values()):
            continue
        X_list.append(feat)
        y_list.append(y)

    X = pd.DataFrame(X_list, columns=FEATURE_COLS)
    y = pd.Series(y_list, name="alpha_20d")
    logger.info("训练集构建完成: 样本 %d 条, 特征 %d 维", len(X), len(FEATURE_COLS))
    return X, y


def train_lgb_model(X: pd.DataFrame, y: pd.Series) -> lgb.Booster:
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    params = {
        "objective": "regression", "metric": "rmse",
        "learning_rate": 0.05, "num_leaves": 31,
        "min_data_in_leaf": 20, "feature_fraction": 0.9,
        "verbose": -1, "seed": 42,
    }
    train_data = lgb.Dataset(X, label=y)
    booster = lgb.train(params, train_data, num_boost_round=200)
    booster.save_model(str(MODEL_PATH))
    logger.info("LightGBM 模型已保存: %s", MODEL_PATH)
    return booster


# ========== 赛道内排序 ==========

def _bigrams(s: str) -> set:
    return {s[i:i+2] for i in range(len(s)-1)} if len(s) >= 2 else {s}


# ponytail: LLM行业名 → RBSA行业名 同义映射，加当新LLM输出未能匹配时
_SECTOR_ALIASES: dict[str, str] = {
    "风电设备": "电源设备",
    "光伏设备": "电源设备",
    "光伏": "电源设备",
    "水电": "电力",
    "水利发电": "电力",
    "油气开采": "石油天然气",
    "油气": "石油天然气",
    "航空运输": "航空机场",
    "航空航天": "航空航天装备",
    "电网设备": "输变电设备",
    "配电设备": "输变电设备",
    "特高压": "输变电设备",
    "能源金属": "稀有金属",
    "电池化学品": "化学制品",
    "数字芯片设计": "半导体",
    "模拟芯片设计": "半导体",
    "线缆部件及其他": "其他",
    "芯片设计": "半导体",
    "存储芯片": "半导体",
    "半导体设备": "半导体",
    "半导体材料": "半导体",
    "输变电": "输变电设备",
    "电力设备": "输变电设备",
    "电气设备": "输变电设备",
    "集成电路封测": "半导体",
    "集成电路封装": "半导体",
    "先进封装": "半导体",
    "高带宽内存": "半导体",
    "AI芯片": "半导体",
    "证券": "非银行金融",
    "券商": "非银行金融",
    "综合金融服务": "非银行金融",
    "工业金属": "基本金属",
    "白酒": "饮料",
    "军工": "航空航天装备",
    "军工电子": "航空航天装备",
    "军工装备": "航空航天装备",
}


def _match_one_sector(ideal: str, candidates: list[str]) -> str | None:
    """把 LLM 选的行业名模糊匹配到 RBSA 行业名。"""
    normalized = _SECTOR_ALIASES.get(ideal, ideal)
    ideal_lower = normalized.lower()
    for c in candidates:
        if not c:
            continue
        c_lower = c.lower()
        if c_lower == ideal_lower:
            return c
    for c in candidates:
        if not c:
            continue
        c_lower = c.lower()
        if c_lower in ideal_lower:
            return c
    for c in candidates:
        if not c:
            continue
        c_lower = c.lower()
        if ideal_lower in c_lower:
            return c
    # 4. bigram Jaccard（中文语义匹配）
    ideal_grams = _bigrams(ideal_lower)
    best, best_score = None, 0.0
    for c in candidates:
        if not c:
            continue
        c_grams = _bigrams(c.lower())
        if not c_grams or not ideal_grams:
            continue
        score = len(ideal_grams & c_grams) / len(ideal_grams | c_grams)
        if score > best_score:
            best_score = score
            best = c
    return best if best_score >= 0.25 else None


def _resolve_sectors(sectors: list[str]) -> list[str]:
    """把 LLM 选的行业名匹配到 RBSA 表中存在的行业名。"""
    conn = _get_db()
    rows = conn.execute(
        "SELECT DISTINCT rbsa_industry_1 FROM fund_features "
        "WHERE rbsa_industry_1 IS NOT NULL AND rbsa_industry_1 != ''"
    ).fetchall()
    conn.close()
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
    conn = _get_db()
    idx = conn.execute(
        "SELECT close FROM index_daily WHERE code='sh000300' ORDER BY date DESC LIMIT 21"
    ).fetchall()
    conn.close()
    return (idx[0][0] / idx[-1][0] - 1) * 100 if len(idx) >= 21 else 0.0


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

    conn = _get_db()
    placeholders = ",".join("?" * len(sectors))
    rows = conn.execute(
        f"SELECT ff.code, fb.name, ff.rbsa_industry_1, "
        f"{', '.join('ff.' + c for c in FEATURE_COLS)} "
        f"FROM fund_features ff "
        f"JOIN fund_basic fb ON fb.code = ff.code "
        f"WHERE fb.is_buyable = 1 "
        f"AND ff.rbsa_industry_1 IN ({placeholders})",
        sectors,
    ).fetchall()

    if not rows:
        logger.info("赛道内无匹配基金，降级为全市场 Top 10")
        conn.close()
        return rank_funds(model)

    cols = ["code", "name", "sector"] + FEATURE_COLS
    df = pd.DataFrame(rows, columns=cols)
    df = df.dropna(subset=FEATURE_COLS)
    if df.empty:
        conn.close()
        return rank_funds(model)

    df = df[~df["sector"].isin(risk_sectors)]
    df = _add_sector_relatives(df)
    df = df[df["momentum_20d"] >= _load_ranking_cfg()["momentum_guard_pct"]]

    X = df[FEATURE_COLS].astype(float)
    df["score"] = model.predict(X)
    df = df[np.isfinite(df["score"])]

    idx_mom = _index_momentum()
    df["rel_strength"] = df["momentum_20d"] - idx_mom
    calmar_clipped = df["calmar"].clip(-5, 5)
    score_min, score_max = df["score"].min(), df["score"].max()
    score_range = score_max - score_min if score_max > score_min else 1.0
    df["score_norm"] = (df["score"] - score_min) / score_range
    df["combo"] = (
        df["rel_strength"] * 0.3
        + df["sector_rel_momentum"] * 0.3
        + calmar_clipped * 0.1
        + df["sector_rel_calmar"] * 0.1
        + (df["hurst_60d"] - 0.5) * 10 * 0.15
        + df["score_norm"] * 0.05
    )

    top_per_sector = []
    for sector in sectors:
        sdf = df[df["sector"] == sector].sort_values("combo", ascending=False)
        top_per_sector.extend(sdf.head(2).to_dict("records"))
    conn.close()

    top_per_sector = sorted(top_per_sector, key=lambda x: x["combo"], reverse=True)[:5]
    if not top_per_sector:
        return rank_funds(model)

    results = []
    for f in top_per_sector:
        results.append({
            "code": f["code"], "name": f["name"],
            "sector": f["sector"],
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
    conn = _get_db()
    rows = conn.execute(
        "SELECT ff.code, fb.name, ff.regime, "
        f"{', '.join('ff.' + c for c in FEATURE_COLS)} "
        "FROM fund_features ff "
        "JOIN fund_basic fb ON fb.code = ff.code "
        "WHERE fb.is_buyable = 1 "
        "AND ff.rbsa_industry_1 IS NOT NULL AND ff.rbsa_industry_1 != ''"
    ).fetchall()

    cols = ["code", "name", "regime"] + FEATURE_COLS
    df = pd.DataFrame(rows, columns=cols)
    df = df.dropna(subset=FEATURE_COLS)
    if df.empty:
        return []

    X = df[FEATURE_COLS].astype(float)
    df = df.copy()
    df["score"] = model.predict(X)
    df = df[np.isfinite(df["score"])]

    idx_mom = _index_momentum()
    conn.close()
    df["rel_strength"] = df["momentum_20d"] - idx_mom
    df = df[df["momentum_20d"] >= guard]
    calmar_clipped = df["calmar"].clip(-5, 5)
    score_min, score_max = df["score"].min(), df["score"].max()
    score_range = score_max - score_min if score_max > score_min else 1.0
    df["score_norm"] = (df["score"] - score_min) / score_range
    df["combo"] = (
        df["rel_strength"] * cfg["rel_strength_weight"]
        + calmar_clipped * cfg["calmar_weight"]
        + (df["hurst_60d"] - 0.5) * 10 * cfg["hurst_weight"]
        + df["score_norm"] * 0.05
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


def _build_final_prompt(candidates: list[dict], ctx: MacroContext, insights: list) -> str:
    lines = [
        "【赛道推论】",
        ctx.sector_reasoning or "无",
        f"推荐赛道: {', '.join(ctx.recommended_sectors) or '全市场'}",
        f"回避赛道: {', '.join(ctx.risk_sectors) or '无'}",
    ]
    if insights:
        lines += ["", "【历史教训参考（请参考以下经验避免同类失误）】"]
        lines += [f"  - {r}" for r in insights]
    lines += [
        "",
        "【宏观判定】",
        f"大盘判定: {ctx.regime_label}",
        "",
        "【候选基金（赛道内排序，含重仓股与新闻匹配）】",
    ]
    for i, c in enumerate(candidates, 1):
        sector = c.get("sector") or c.get("rbsa_industry_1", "")
        lines.append(
            f"第{i}名: {c['code']} {c['name']} | 赛道: {sector} | "
            f"卡玛: {c['calmar']:.2f} | "
            f"Hurst: {c['hurst_60d']:.2f} | 组合分: {c['combo']:.3f}"
        )
        holdings = c.get("holdings", [])
        if holdings:
            hds = [f"{h['stock_name']}({h['industry']},权重{h['weight']:.1f}%)" for h in holdings]
            lines.append(f"  重仓股: {', '.join(hds)}")
        matched = c.get("matched_news", [])
        if matched:
            for m in matched:
                lines.append(
                    f"  ⚡ 新闻匹配: 持仓股 {m['stock_name']} 出现在今日新闻 "
                    f"[等级={m['level']}] \"{m['title'][:50]}\""
                )
    lines += [
        "",
        "【任务指令】",
        "基于以上所有信息（赛道推论、重仓股、新闻匹配、进化规则），",
        "从候选中选出最有潜力的一只基金。重点考虑：",
        "1. 基金重仓股与今日新闻的匹配程度（利好>利空）",
        "2. 基金所在赛道是否被宏观定论认可",
        "3. 赛道内相对强弱和量化指标",
        "4. 是否重复历史教训中提到的失败模式",
        "",
        "【严格要求】选定理由必须：",
        "1. 用普通投资者能看懂的中文，禁止使用任何英文术语、缩写、专业词汇",
        "2. 禁止出现：RBSA、Hurst、Calmar、回撤、波动率、动量、Beta、Alpha等术语",
        "3. 禁止出现类似 'RBSA行业::64.93' 这种格式",
        "4. 必须包含：今天宏观分析的结论（大盘走势、资金流向、板块轮动）",
        "5. 必须包含：这只基金重仓的行业和股票，以及为什么这些持仓是好的",
        "6. 必须包含：最近有什么利好消息或政策支持这个行业",
        "7. 从上面的【赛道推论】中提取今天LLM的行业分析，用自己的话总结成2-3句话放在理由开头",
        "",
        "输出要求（务必严格遵守）：",
        "1. 只输出一个纯 JSON 对象，不要使用 markdown 代码块（禁止 ```json）。",
        "2. 所有指标数值必须严格使用上方候选名单中提供的真实数据。",
        "3. selected_code 必须是候选列表中真实存在的代码字符串：",
        '{',
        '  "selected_code": "选中的基金代码",',
        '  "selected_name": "选中的基金名称",',
        '  "reason": "用大白话说明推荐理由，包含今天分析结论，禁止专业术语",',
        '  "vetoed": [',
        '    {"code": "被否决代码", "name": "被否决名称", "reason": "否决理由"}',
        '  ]',
        '}',
    ]
    return "\n".join(lines)


def _llm_final_pick(candidates: list[dict], ctx: MacroContext, insights: list) -> dict:
    """LLM 基于重仓股+CLS新闻匹配做最终选择，返回选定基金和否决记录。"""
    from data_foundation import _call_llm

    conn = _get_db()
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
    conn.close()

    prompt = _build_final_prompt(candidates, ctx, insights)
    system_prompt = "你是量化基金推荐决策助手。必须只输出一个纯JSON对象，禁止使用markdown代码块，禁止任何前后说明文字。"

    content = _call_llm(prompt, system_prompt=system_prompt, max_tokens=4096)

    if content is None:
        logger.warning("LLM 调用失败，降级选取第1名")
        top = candidates[0]
        return {
            "selected_code": top["code"], "selected_name": top["name"],
            "reason": "LLM 调用失败，按赛道排序直接选取第1名",
            "vetoed": [],
        }

    valid_codes = {c["code"]: c["name"] for c in candidates}
    result = _parse_llm_result(content, valid_codes)
    if result is not None:
        return result

    logger.error("LLM 返回无法解析，降级选取第1名，原始响应: %s", content[:300])
    top = candidates[0]
    return {
        "selected_code": top["code"], "selected_name": top["name"],
        "reason": "LLM 返回无法解析，按赛道排序选取第1名",
        "vetoed": [],
    }


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
    code = result.get("selected_code")
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


def _dump_recommendation(date_str, code, name, rank, score, regime, candidates, vetoed):
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
    _LAST_RECO_PATH.write_text("\n".join(lines), encoding="utf-8")


def _save_recommendation(date_str: str, selected: dict, candidates: list[dict],
                          vetoed: list, regime: str, feature_snapshot: str = "") -> None:
    conn = _get_db()
    exists = conn.execute(
        "SELECT id, entry_nav FROM recommend_log WHERE recommend_date = ? LIMIT 1", (date_str,)
    ).fetchone()
    if exists and exists[1] is not None:
        logger.info("当日 %s 已存在推荐记录且 entry_nav 不为空，跳过", date_str)
        conn.close()
        return
    # 补 entry_nav（首次插入或补充已有记录）
    entry_nav_row = conn.execute(
        "SELECT cum_nav FROM fund_nav WHERE code=? AND date=? LIMIT 1",
        (selected["selected_code"], date_str),
    ).fetchone()
    entry_nav = entry_nav_row[0] if entry_nav_row else None
    if exists:
        conn.execute(
            "UPDATE recommend_log SET entry_nav = ? WHERE id = ?",
            (entry_nav, exists[0]),
        )
        conn.commit()
        conn.close()
        logger.info("补写 entry_nav=%s 到 id=%s", entry_nav, exists[0])
        return
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
    conn.execute(
        "INSERT INTO recommend_log "
        "(recommend_date, code, name, rank, score, combo, regime, buy_reason, status, feature_snapshot, entry_nav) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'HOLD', ?, ?)",
        (date_str, selected["selected_code"], real_name,
         rank, score, combo, regime, reason, feature_snapshot, entry_nav),
    )
    conn.commit()
    conn.close()
    logger.info("推荐入库: %s %s (排名%d, 分数%.4f)",
                selected["selected_code"], real_name, rank, score or 0.0)
    _dump_recommendation(date_str, selected["selected_code"], real_name, rank, score,
                         regime, candidates, vetoed)


def run_recommendation(retrain: bool = False, force: bool = False) -> None:
    """推荐引擎主入口：LLM 选赛道 → 赛道内排序 → LLM 定论 → 入库。

    force=True 时跳过宏观缓存，强制实时抓取新闻+LLM 重新选赛道。
    """
    date_str = datetime.now().strftime("%Y-%m-%d")
    conn = _get_db()
    insights = _load_insights(conn)
    conn.close()

    if retrain or not MODEL_PATH.exists():
        logger.info("=== 准备训练数据并训练 LightGBM ===")
        X, y = prepare_lgb_training_data()
        if len(X) == 0:
            logger.error("训练样本为空，终止推荐")
            return
        model = train_lgb_model(X, y)
    else:
        logger.info("=== 加载已保存模型 ===")
        model = lgb.Booster(model_file=str(MODEL_PATH))

    logger.info("=== LLM 宏观分析 + 选赛道 ===")
    ctx = build_macro_context(date_str, force=force)
    llm_regime = ctx.regime_label.upper()
    llm_regime = llm_regime if llm_regime in ("BULL", "BEAR") else "NEUTRAL"
    logger.info("选定赛道: %s | 回避: %s | 大盘: %s",
                ctx.recommended_sectors, ctx.risk_sectors, ctx.regime_label)

    logger.info("=== 赛道内相对化排序 ===")
    finalists = _rank_within_sectors(ctx, model)
    if not finalists:
        logger.error("无候选基金，终止推荐")
        return
    logger.info("候选 %d 只: %s",
                len(finalists),
                ", ".join(f"{f['code']}({f.get('sector','?')},combo={f['combo']:.3f})"
                          for f in finalists))

    logger.info("=== LLM 最终定论（持仓+新闻交叉验证）===")
    result = _llm_final_pick(finalists, ctx, insights)

    selected = {
        "selected_code": result["selected_code"],
        "selected_name": result["selected_name"],
        "reason": result.get("reason", ""),
    }
    vetoed = result.get("vetoed", [])
    logger.info("LLM 选定: %s %s | 否决 %d 只",
                selected["selected_code"], selected["selected_name"], len(vetoed))

    guard = _load_ranking_cfg()["momentum_guard_pct"]
    sel_momentum = next(
        (float(c.get("momentum_20d", 0)) for c in finalists
         if c["code"] == selected["selected_code"]), None)
    if sel_momentum is not None and sel_momentum < guard:
        logger.warning("风控拦截: %s 近20日动量 %.1f%% 低于阈值 %.0f%%",
                       selected["selected_code"], sel_momentum, guard)
        conn = _get_db()
        conn.execute(
            "INSERT INTO recommend_log "
            "(recommend_date, code, name, rank, score, combo, regime, buy_reason, status) "
             "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'REJECT')",
             (date_str, selected["selected_code"], selected["selected_name"],
              0, 0.0, 0.0, llm_regime,
             f"风控拦截: 20日动量{sel_momentum:.1f}% 低于阈值{guard:.0f}%"),
        )
        conn.commit()
        conn.close()
        logger.info("风控拦截已入库: %s", selected["selected_code"])
        return

    # 特征快照（演化回看用）
    sel_features = next(
        (c for c in finalists if c["code"] == selected["selected_code"]), {})
    feature_snapshot = json.dumps({
        "sector": sel_features.get("sector", ""),
        "momentum_20d": sel_features.get("momentum_20d", 0),
        "hurst_60d": sel_features.get("hurst_60d", 0),
        "calmar": sel_features.get("calmar", 0),
        "sector_rel_momentum": sel_features.get("sector_rel_momentum", 0),
        "sector_rel_calmar": sel_features.get("sector_rel_calmar", 0),
    }, ensure_ascii=False)

    # 同步选中基金的净值（确保 entry_nav 能取到当日盘后数据）
    conn = _get_db()
    new_rows = fetch_fund_nav_incremental(selected["selected_code"], conn)
    if new_rows:
        conn.commit()
        logger.info("净值同步: %s 新增 %d 条", selected["selected_code"], new_rows)
    conn.close()

    _save_recommendation(date_str, selected, finalists, vetoed, llm_regime, feature_snapshot)

    # 记录赛道选择（关联推荐日志，供进化闭环分析）
    _write_sector_selection(date_str, ctx, json.loads(feature_snapshot))

    logger.info("推荐流程完成")


def _write_sector_selection(date_str: str, ctx: MacroContext,
                            sel_features: dict) -> None:
    conn = _get_db()
    log_id = conn.execute(
        "SELECT id FROM recommend_log ORDER BY recommend_date DESC LIMIT 1"
    ).fetchone()
    log_id = log_id[0] if log_id else None
    # ponytail: 每天只保留一条赛道选择，先清旧再写入避免重复累积
    conn.execute("DELETE FROM sector_selections WHERE date = ?", (date_str,))
    conn.execute(
        "INSERT INTO sector_selections (date, recommend_log_id, recommended_sectors, "
        "risk_sectors, sector_reasoning, regime_label) VALUES (?, ?, ?, ?, ?, ?)",
        (date_str, log_id,
         json.dumps(ctx.recommended_sectors, ensure_ascii=False),
         json.dumps(ctx.risk_sectors, ensure_ascii=False),
         ctx.sector_reasoning, ctx.regime_label),
    )
    conn.commit()
    conn.close()


if __name__ == "__main__":
    import sys
    retrain = "--retrain" in sys.argv
    run_recommendation(retrain=retrain)

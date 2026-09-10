"""推荐引擎：LLM 选赛道 → LightGBM 赛道内排 → LLM 定论（Phase 2 重构）。

漏斗：准备标注数据 → 训练 LightGBM → 宏观LLM选赛道
      → 赛道内相对化排序 → 持仓+新闻交叉验证 → LLM终选定论 → 入库。

依赖 data_foundation 的 DB 连接与特征计算结果（fund_features 表）。
运行：uv run python recommend.py
"""

import json
from datetime import datetime
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd

import app.repo as repo
from app import domain
from app.data.nav import fetch_fund_nav_incremental
from app.engine.macro_agent import MacroContext, build_macro_context
from app.features.calculator import (
    apply_momentum_guard,
    latest_below_ema60,
    market_state_features,
    score_frame,
)
from app.llm.client import call_llm_json
from app.llm.prompts import final_pick_prompt, final_pick_system_prompt
from app.model import get_or_train, model_version
from app.repo import meta_keys as META
from app.utils.log import get_logger
from app.utils.trading_calendar import trading_day_lag  # 滞后交易日数单一来源

logger = get_logger("recommend")

FEATURE_COLS = repo.FEATURE_COLS
_FORWARD_WINDOW = repo.FORWARD_WINDOW


# ========== 2.1 标注数据准备 ==========


# ========== 赛道内排序 ==========

# 赛道名解析与锚定已收敛为 domain.SectorPolicy（推荐/监控/宏观共用单一来源，
# 见架构深化候选 1）；本引擎只消费判定结果，不再自行 resolve。


def _inject_market_cols(df: pd.DataFrame) -> pd.DataFrame:
    """注入市场状态列（R1）：指数 20 日动量/波动率 + 当日大盘状态，全行共享。

    打分与训练同口径；regime 同步刷为当日状态机——特征快照滞后数日时
    （数据基座停摆后）fund_features.regime 是旧状态，权重调整会错配。
    """
    df = df.copy()
    df["regime"] = repo.get_market_regime()
    idx_rows = repo.get_index_series("sh000300", ("date", "close", "volume"))
    if idx_rows:
        closes = np.array([r[1] for r in idx_rows], dtype=float)
        vols = np.array([r[2] for r in idx_rows], dtype=float)
        mkt = market_state_features(closes, vols)
        for c, v in mkt.items():
            df[c] = v
    else:
        # 审计 P1-2/P2-4：指数缺失不能静默 0——模型会在分布外输入上打分
        logger.error("指数数据缺失（sh000300 无行）：市场状态列填 0，模型打分可能严重失真，请检查数据基座")
        for c in repo.MARKET_COLS:
            df[c] = 0.0
    return df


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


def _dedup_fund_name(name: str) -> str:
    """去掉基金份额后缀（A/C/E/D 等单字母），识别同一基金的不同份额。

    候选池放大后 A/C 份额同池会稀释 LLM 定论质量（如 015412/015413 为同一基金
    两份额，特征几乎相同却占两个候选名额）；按去份额名保留 combo 最高者。
    """
    import re as _re
    # 份额后缀紧贴基金类型词（如"混合A"），前一个字符须为非字母/下划线/数字；
    # 避免误伤代码型结尾（如测试名"基金SC_A"的 A 不是份额）。
    return _re.sub(r"(?<![A-Za-z_0-9])(A|B|C|D|E|F|H|I|O|Y|Z)$", "", (name or "").strip())


def _below_ema60(codes: list[str], recovery_map: dict[str, float] | None = None) -> set[str]:
    """现价跌破自身 EMA60 的基金集合（推荐侧 R1 对齐门槛，B 修复 2026-09）。

    推荐只看短期动量特征，可能选到“中期趋势向下、短期反弹”的基金——监控
    R1（EMA60 趋势退出）入场当天即判 EXIT，推荐/监控矛盾。前置过滤掉最新
    净值 < EMA60 的基金（与 calculator.latest_below_ema60 同口径）。
    熊市反转入围（方向 1+2）：recovery_map 提供每基金 reversal_20d（正=下跌
    减速/企稳），跌破 EMA60 但 reversal_20d > 0 的反转基金放行——熊市中找
    底部反转潜力基金，正是核心目的（牛熊市都推荐）。监控 R1 已同步加 entry_idx
    豁免（入场后净值不足不判），放行后不会“推荐当天即 EXIT”。
    数据不足 60 条的基金不滤（保守，交回防线判定）。
    """
    below: set[str] = set()
    for code in codes:
        navs = [v for _, v in repo.nav.series(code, limit=62)]
        if latest_below_ema60(navs):
            recovery = (recovery_map or {}).get(code, 0.0)
            if recovery > 0:
                continue  # 跌破但企稳反转 → 放行（熊市潜力点）
            below.add(code)
    return below


def _filter_sector_candidates(df: pd.DataFrame, sectors: list[str],
                             risk_set: set[str]) -> pd.DataFrame:
    """赛道候选过滤链：dropna → 纯度门槛 → 第一行业锚定 → 回避剔除。

    返回过滤后 df（可能 empty，调用方判断降级）。sector 锚定 rbsa_industry_1。
    """
    df = df.dropna(subset=FEATURE_COLS)
    if df.empty:
        return df
    # C4 赛道纯度门槛：第一行业暴露 <10% 的基金不视为赛道基金（口径见 domain）。
    df = df[df["rbsa_weight_1"] >= domain.MIN_SECTOR_EXPOSURE]
    if df.empty:
        logger.info("赛道纯度门槛后无候选，降级为全市场 Top 10")
        return df
    # 赛道归属锚定第一行业（推荐/监控同口径）：基金只有第一行业命中推荐赛道才入选，
    # 避免基金以次要行业入选、监控按第一行业否决的错配。
    df = df[df["rbsa_industry_1"].isin(sectors)]
    if df.empty:
        logger.info("第一行业无匹配赛道，降级为全市场 Top 10")
        return df
    expanded = []
    for _, r in df.iterrows():
        row = r.to_dict()
        row["sector"] = row["rbsa_industry_1"]
        row["rbsa_weight"] = row.get("rbsa_weight_1", 0) or 0
        expanded.append(row)
    df = pd.DataFrame(expanded)
    # 回避赛道整体过滤：第一行业命中回避赛道的基金直接剔除
    df = df[~df["sector"].isin(risk_set)]
    if df.empty:
        logger.info("回避赛道过滤后无候选，降级为全市场 Top 10")
    return df


def _score_sector_candidates(df: pd.DataFrame, model: lgb.Booster) -> pd.DataFrame:
    """赛道相对化打分：sector_relatives + momentum_guard + market_cols + score_frame。"""
    cfg = repo.get_ranking_cfg()
    df = _add_sector_relatives(df)
    df = apply_momentum_guard(df, cfg)
    df = _inject_market_cols(df)
    return score_frame(
        df, model, cfg, repo.get_index_momentum(),
        default_regime=repo.get_market_regime(),
        sector_rel_momentum_col="sector_rel_momentum",
        sector_rel_calmar_col="sector_rel_calmar",
        rbsa_weight_col="rbsa_weight",
    )


def _select_top_per_sector(df: pd.DataFrame, sectors: list[str]) -> list[dict]:
    """候选构建：前 2 赛道各取 Top2，其余各 Top1；按 combo 截断到 MAX_CANDIDATES。

    同基金多份额只保留 combo 最高者（去重）；保底防高热度赛道被全局挤出。
    """
    MAX_CANDIDATES = 8
    core: list[dict] = []  # 前 2 赛道各 2 只
    rest: list[dict] = []  # 其余赛道各 1 只
    for idx, sector in enumerate(sectors):
        sdf = df[df["sector"] == sector].sort_values("combo", ascending=False)
        take = 2 if idx < 2 else 1
        head = []
        seen_funds: set[str] = set()
        for rec in sdf.to_dict("records"):
            key = _dedup_fund_name(rec["name"])
            if key in seen_funds:
                continue
            seen_funds.add(key)
            head.append(rec)
            if len(head) >= take:
                break
        (core if idx < 2 else rest).extend(head)
    core.sort(key=lambda x: x["combo"], reverse=True)
    rest.sort(key=lambda x: x["combo"], reverse=True)
    return core + rest[:MAX_CANDIDATES - len(core)]


def _build_candidate_record(f: dict) -> dict:
    """单条候选结果装配（rbsa 分解 + 量化分，下游入库/展示共用 schema）。

    主路径与降级路径（rank_funds）共用：降级无赛道分解，sector 留空、
    sector_rel_* 取中性默认值——两路径候选 schema 归一，下游不再用
    or 兑底补 schema 洞。
    """
    return {
        "code": f["code"], "name": f["name"],
        "sector": f.get("sector", ""),
        "rbsa_industry_1": f.get("rbsa_industry_1", ""),
        "rbsa_industry_2": f.get("rbsa_industry_2", ""),
        "rbsa_industry_3": f.get("rbsa_industry_3", ""),
        "rbsa_weight_1": float(f.get("rbsa_weight_1", 0) or 0),
        "rbsa_weight_2": float(f.get("rbsa_weight_2", 0) or 0),
        "rbsa_weight_3": float(f.get("rbsa_weight_3", 0) or 0),
        "score": float(f["score"]), "combo": float(f["combo"]),
        "hurst_60d": float(f["hurst_60d"]), "momentum_20d": float(f["momentum_20d"]),
        "calmar": float(f["calmar"]),
        # 抗跌性四件套：LLM 终选素材用（带口径白话展示在 final_pick_prompt），不进模型
        "capture_up": round(float(f.get("capture_up", 1.0)), 2),
        "capture_down": round(float(f.get("capture_down", 1.0)), 2),
        "downside_vol": round(float(f.get("downside_vol", 0.0)), 4),
        "drawdown_60d": round(float(f.get("drawdown_60d", 0.0)), 1),
        "sector_rel_momentum": round(float(f.get("sector_rel_momentum", 0)), 1),
        "sector_rel_calmar": round(float(f.get("sector_rel_calmar", 0)), 1),
    }


def _rank_within_sectors(ctx: MacroContext, model: lgb.Booster) -> tuple[list[dict], str]:
    """在 LLM 选中的赛道内，用赛道相对化特征排序，前 2 赛道各取 Top 2、其余各 Top 1。

    返回 (candidates, reco_path)：path='sector' 为主路径，'degrade' 为降级路径
    （分口径度量：质量度量按 path 分组评估两条路径的赚钱质量差异）。
    """
    raw_sectors = ctx.recommended_sectors
    if not raw_sectors:
        logger.info("无指定赛道，降级为全市场 Top 10")
        return rank_funds(model), "degrade"

    policy = domain.SectorPolicy(repo.get_available_sectors())
    sectors = policy.resolve(raw_sectors)
    if not sectors:
        logger.info("所有赛道均未匹配RBSA行业，降级为全市场 Top 10")
        return rank_funds(model), "degrade"
    dropped = set(raw_sectors) - set(sectors)
    if dropped:
        logger.info("赛道 %s 未匹配到RBSA行业，跳过", "、".join(sorted(dropped)))
    logger.info("LLM赛道 %s → 匹配到 %s", raw_sectors, sectors)
    risk_set = policy.resolve_set(ctx.risk_sectors)

    rows = repo.get_sector_candidates(sectors)
    if not rows:
        logger.info("赛道内无匹配基金，降级为全市场 Top 10")
        return rank_funds(model), "degrade"

    df = pd.DataFrame(rows)
    # 单基金特征新鲜度闸门（审计 P1-1）：滞后 > MAX_FEATURE_LAG_TRADE_DAYS 的候选剔除
    stale_before = len(df)
    df = _drop_stale_feature_rows(df)
    dropped = stale_before - len(df)
    if dropped:
        logger.warning("单基金特征新鲜度闸门：剔除 %d 只特征陈旧候选（滞后 > %d 交易日）",
                       dropped, domain.MAX_FEATURE_LAG_TRADE_DAYS)
    if df.empty:
        logger.info("赛道内候选全部特征陈旧，降级为全市场 Top 10")
        return rank_funds(model), "degrade"
    df = _filter_sector_candidates(df, sectors, risk_set)
    if df.empty:
        return rank_funds(model), "degrade"
    # B 修复（2026-09）+ 熊市反转入围（方向 1+2）：EMA60 趋势门槛——
    # 现价跌破自身 EMA60 且未企稳（reversal_20d<=0）的基金剔除；
    # 跌破但 reversal_20d>0（下跌减速/反转）的放行（熊市潜力点）。
    below = _below_ema60(df["code"].tolist(),
                         recovery_map=df.set_index("code")["reversal_20d"].to_dict())
    if below:
        df = df[~df["code"].isin(below)]
        if df.empty:
            logger.info("赛道内候选全部跌破自身EMA60，降级为全市场 Top 10")
            return rank_funds(model), "degrade"
        logger.info("EMA60 趋势门槛剔除 %d 只（现价<自身EMA60，R1 会立即退出）", len(below))

    df = _score_sector_candidates(df, model)
    # Ticket 05：恢复入场质量门槛（移除 R1"全天候出手"）——模型预测 > 赚钱阈值才可进入
    # 终选候选。D 冲突修复：门槛对齐 PROFIT_THRESHOLD（扣费后赚钱），不再用 0。
    # 监控 ModelSignalRule 出场阈值用 MIN_PREDICTED_ALPHA（转负边界），两者语义不同。
    df = df[df["score"] > domain.MIN_ENTRY_ALPHA]
    if df.empty:
        logger.info("赛道内无正预测候选，降级为全市场 Top 10（降级路径同样受门槛约束）")
        return rank_funds(model), "degrade"

    top_per_sector = _select_top_per_sector(df, sectors)
    if not top_per_sector:
        return rank_funds(model), "degrade"
    return [_build_candidate_record(f) for f in top_per_sector], "sector"


def rank_funds(model: lgb.Booster) -> list[dict]:
    """全市场排名（降级备选），返回 Top 10。"""
    cfg = repo.get_ranking_cfg()
    rows = repo.get_all_ranking_rows()
    df = pd.DataFrame(rows)
    # 单基金特征新鲜度闸门（同主路径）：陈旧特征不出现在降级推荐里
    df = _drop_stale_feature_rows(df)
    if df.empty:
        logger.info("全市场候选特征陈旧/缺失，返回空（空推荐日）")
        return []
    df = df.dropna(subset=FEATURE_COLS)
    if df.empty:
        return []
    df = apply_momentum_guard(df, cfg)

    idx_mom = repo.get_index_momentum()
    df = _inject_market_cols(df)
    df = score_frame(df, model, cfg, idx_mom,
                     default_regime=repo.get_market_regime(),
                     rbsa_weight_col="rbsa_weight_1")
    # Ticket 05：降级路径同样恢复入场门槛——全市场无正预测候选时返回空，
    # 由上游 record_empty_recommendation 记空推荐日（熊市不硬推负期望基金）。
    # D 冲突修复：门槛对齐 MIN_ENTRY_ALPHA（扣费后赚钱），与主路径同口径。
    df = df[df["score"] > domain.MIN_ENTRY_ALPHA]
    if df.empty:
        logger.info("全市场无正预测候选，返回空（空推荐日）")
        return []
    top = df.sort_values("combo", ascending=False).head(10)
    # B 修复（2026-09）+ 熊市反转入围（方向 1+2）：EMA60 趋势门槛（与主路径
    # 同口径）——全市场 Top10 逐只查净值，滤掉现价跌破自身 EMA60 且未企稳的；
    # reversal_20d>0（企稳反转）的放行。
    below = _below_ema60(top["code"].tolist(),
                         recovery_map=top.set_index("code")["reversal_20d"].to_dict())
    if below:
        top = top[~top["code"].isin(below)]
    if top.empty:
        logger.info("全市场 Top10 全部跌破自身EMA60，返回空（空推荐日）")
        return []
    # schema 归一：与主路径共用 _build_candidate_record（sector 留空、
    # sector_rel_* 取中性默认）。此前内联重建 dict 缺 sector/sector_rel_*，
    # 下游用 or 兑底；降级候选因此曾被消费端赛道过滤错误裁剪。
    return [_build_candidate_record(r.to_dict()) for _, r in top.iterrows()]


# ========== LLM 最终定论 ==========

def _load_insights() -> list[str]:
    """读取活跃洞察（定论 prompt 用）并标记 apply（Q4：进入 prompt 即 apply_count+1）。"""
    rows = repo.get_active_insights(8)
    if rows:
        repo.mark_insights_applied([i for i, _ in rows], datetime.now().strftime("%Y-%m-%d"))
    return [t for _, t in rows]



def _sector_candidates(finalists: list[dict], sector: str,
                       excluded_codes: set[str]) -> list[dict]:
    """整理赛道候选：过滤 + 多行业展开去重 + 剔除已选定基金。

    一只基金可同时命中多个赛道（rbsa_industry_1/2/3），展开后同一基金
    会以多条记录进入 finalists。若不处理，同日不同赛道可能重复推荐同一基金
    （8-04 线上复现：半导体/通信设备赛道都选了 012428）。
    - 同赛道按 code 去重，优先保留 sector 与目标赛道一致的记录，
      保证 feature_snapshot 赛道归属正确、与监控判定一致；
    - excluded_codes 为前序赛道已选定基金，保证一次推荐不出现重复基金。
    """
    by_code: dict[str, dict] = {}
    for c in finalists:
        if c.get("sector") != sector and c.get("rbsa_industry_1") != sector:
            continue
        code = c["code"]
        if code in by_code:
            if c.get("sector") == sector:
                by_code[code] = c
        else:
            by_code[code] = c
    return [c for c in by_code.values() if c["code"] not in excluded_codes]


def _pick_group_candidates(finalists: list[dict], sector: str,
                           reco_path: str, excluded_codes: set[str]) -> list[dict]:
    """单分组的候选整理：主路径按赛道过滤，降级路径全市场直通。

    降级语义单一承载点：reco_path == "degrade" 时全市场候选
    （schema 无赛道归属）不受 LLM 赛道约束，只做已选去重。
    此前该语义只存在于 _rank_within_sectors 返回点注释，消费端
    仍按赛道过滤，导致降级候选被裁剪、优质候选日被错误记成空推荐日。
    """
    if reco_path == "degrade":
        return [c for c in finalists if c["code"] not in excluded_codes]
    return _sector_candidates(finalists, sector, excluded_codes)


def _assemble_finalist_material(candidates: list[dict],
                                latest_feature_date: str | None) -> list[dict]:
    """终选素材装配：DB 取数 + 口径计算（持仓/申购/赛道中位数/mom_gap）。

    对齐 monitor 的 DefenseContext 形状——装配与判定分离：本函数是
    终选素材口径（持仓月龄、mom_gap、限购标注）的唯一归属，可脱离
    LLM 调用独立测试；llm/context.py 只负责文案格式化，_llm_final_pick
    只消费已装配素材。不变异入参（返回拷贝），主循环后续消费原字段不受影响。
    """
    codes = [c["code"] for c in candidates]
    holdings_mat = repo.get_holdings_summaries(codes, limit=5)
    purchase_map = repo.get_purchase_statuses(codes)
    sector_median_cache: dict[str, float | None] = {}

    assembled = []
    for c in candidates:
        c = dict(c)
        code = c["code"]
        mat = holdings_mat.get(code, {"holdings": [], "report_date": None})
        c["holdings"] = mat.get("holdings", [])
        report_date = mat.get("report_date")
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
        # T10 限购检测：申购状态标注（未知/正常不阻塞；限购/暂停在素材中提示）
        c["purchase_status"] = purchase_map.get(code, "unknown")
        if sector and latest_feature_date:
            if sector not in sector_median_cache:
                median = repo.get_sector_momentum_median(sector, latest_feature_date)
                sector_median_cache[sector] = (
                    round(float(median), 1) if median is not None else None)
            median = sector_median_cache[sector]
            if median is not None:
                c["sector_median_mom"] = median
                c["mom_gap"] = round(float(fund_mom) - median, 1)
            else:
                c["sector_median_mom"] = None
                c["mom_gap"] = None
        else:
            c["sector_median_mom"] = None
            c["mom_gap"] = None
        assembled.append(c)
    return assembled


def _llm_final_pick(candidates: list[dict], ctx: MacroContext, insights: list) -> dict:
    """LLM 基于重仓股+CLS新闻匹配+持仓时效性做最终选择，返回选定基金和否决记录。"""
    latest_feature_date = repo.get_latest_feature_date()
    # 素材装配与判定分离：本函数只负责 LLM 定论，取数与口径在 _assemble_finalist_material
    candidates = _assemble_finalist_material(candidates, latest_feature_date)

    prompt = final_pick_prompt(candidates, ctx, insights)
    system_prompt = final_pick_system_prompt()
    valid_codes = {c["code"]: c["name"] for c in candidates}

    # 候选3 收敛：统一走 call_llm_json——结构校验入 validator（返回 None 视为解析失败），
    # 审计 ok 语义与选赛道/监控/进化一致；技术失败仍抛 LLMError（候选 7）
    result = call_llm_json(
        prompt, system_prompt=system_prompt, max_tokens=16384,
        fallback=None, caller="recommend_final_pick",
        validator=lambda parsed: _validate_final_pick(parsed, valid_codes),
    )
    if result is not None:
        return result

    raise RuntimeError("LLM最终定论返回无法解析（原始输出见 llm_audit）")


def _validate_final_pick(parsed: Any, valid_codes: dict) -> dict | None:
    """终选定论结构校验（validator core）：非 dict / 代码不在候选 → None（视为解析失败）。"""
    if not isinstance(parsed, dict):
        return None
    result = {str(k).strip(". "): v for k, v in parsed.items()}
    code = str(result.get("selected_code") or "")
    if code in valid_codes:
        # 审计 P2-3：vetoed 条目同样校验——只保留候选池内代码，
        # LLM 幻觉代码不写入 vetoed_json 污染否决审计/面板
        vetoed = [str(v).strip() for v in (result.get("vetoed") or [])
                  if isinstance(v, (str, int)) and str(v).strip() in valid_codes]
        return {
            "selected_code": code,
            "selected_name": result.get("selected_name", valid_codes[code]),
            "reason": result.get("reason", ""),
            # P2-7 决策与文案解耦：decision_logic 为内部决策依据（审计用），
            # 与展示文案 reason 分离；旧 prompt 无该字段时为空串（兼容）
            "decision_logic": result.get("decision_logic", ""),
            "vetoed": vetoed,
        }
    return None


# ========== 推荐入库 ==========

def _save_recommendation(date_str: str, selected: dict, candidates: list[dict],
                           vetoed: list, regime: str, feature_snapshot: str = "",
                           reco_path: str = "sector") -> int:
    """入库推荐记录，返回新插入行的 id。reco_path：推荐来源路径（分口径度量）。"""
    rank = next(
        (i + 1 for i, c in enumerate(candidates) if c["code"] == selected["selected_code"]), 1)
    score = next(
        (c["score"] for c in candidates if c["code"] == selected["selected_code"]), None)
    combo = next(
        (c["combo"] for c in candidates if c["code"] == selected["selected_code"]), None)
    # P2-7 决策与文案解耦（完成态）：buy_reason 只存展示文案；否决已有
    # vetoed_json 列（T07），决策逻辑走 decision_logic 独立列——不再拼
    # "| 否决记录:"/"| 决策逻辑:" 尾巴进文案，展示层魔法分隔符契约退役
    #（split 保留仅为历史行兼容）。
    reason = selected.get("reason", "")
    decision_logic = str(selected.get("decision_logic", ""))[:500]
    real_name = repo.get_fund_name(selected["selected_code"]) or selected["selected_name"]
    entry_nav = repo.nav.latest(selected["selected_code"])
    # Q5 裁决损耗观测：落库当日候选池代码（LLM 面对的选择集），质量度量时回查 40 日收益
    new_id = repo.insert_recommendation(
        date_str, selected["selected_code"], real_name, rank, score or 0.0, combo or 0.0, regime,
        reason, status=domain.SIGNAL_HOLD, feature_snapshot=feature_snapshot,
        entry_nav=entry_nav, candidate_codes=[c["code"] for c in candidates],
        # T07：否决结构化落库（否决审计数据基础）
        vetoed=vetoed, reco_path=reco_path, decision_logic=decision_logic,
    )
    # 同日推荐成功：清掉可能的空推荐残留（同一天先判无赛道、后成功推荐的场景）
    repo.clear_empty_recommendation(date_str)
    logger.info("推荐入库: %s %s (排名%d, 分数%.4f, id=%d)",
                selected["selected_code"], real_name, rank, score or 0.0, new_id)
    return new_id


def _trade_calendar() -> set[str] | None:
    """交易日历缓存（meta 单次解析）；无缓存/解析失败返回 None（调用方不误伤）。

    滞后口径三处（期望日期/全局新鲜度/单基金闸门）共用，替代各自重复
    get_meta + json.loads（原 _drop_stale_feature_rows 逐行重复解析）。
    """
    raw = repo.get_meta(META.TRADE_DATES_CACHE)
    if not raw:
        return None
    try:
        return set(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        return None


def _expected_feature_date() -> str | None:
    """期望特征日期：今天交易日→昨交易日（数据任务盘前拉 T-1 净值）；否则最近交易日。

    无交易日历缓存返回 None（调用方不误报/不误伤）。
    """
    days = _trade_calendar()
    if not days:
        return None
    today = datetime.now().date().isoformat()
    if today in days:
        return max((d for d in days if d < today), default=None)  # 今交易，期望 T-1
    return max((d for d in days if d <= today), default=None)  # 非交易日，期望最近交易日


def _feature_freshness(feat_date: str | None) -> int:
    """特征日期距期望值的滞后交易日数（0=新鲜）。

    滞后计数用 trading_day_lag（与净值停更打标共用单一来源）。
    """
    if not feat_date:
        return 0
    expected = _expected_feature_date()
    days = _trade_calendar()
    if expected is None or feat_date >= expected or not days:
        return 0
    return trading_day_lag(feat_date, expected, days=days)


def _drop_stale_feature_rows(df: pd.DataFrame) -> pd.DataFrame:
    """单基金特征新鲜度闸门（审计 P1-1）：剔除特征日滞后决策日 > N 交易日的候选。

    全局护栏（_feature_freshness）只拦"数据基座整体失败"；单基金因停牌/接口局部失败
    可滞后最多 10 交易日（_STALE_NAV_LAG_DAYS）仍以旧快照入池，与实时市场列、赛道
    中位数构成混合时点假相对值直接进排序与 LLM 终选素材——信息失真（"准确提供信息"
    契约）。特征日缺失/无法判定时保守滤除。*不改变 df 列结构*（仅筛行）。
    """
    if df.empty or "feature_date" not in df.columns:
        return df
    expected = _expected_feature_date()
    days = _trade_calendar()
    if expected is None or not days:
        return df  # 无交易日历缓存：不误伤，全局护栏兜底

    def _lag(fd: object) -> int:
        if not isinstance(fd, str) or not fd:
            return domain.MAX_FEATURE_LAG_TRADE_DAYS + 1  # 无日期 → 视为陈旧
        if fd >= expected:
            return 0
        return trading_day_lag(fd, expected, days=days)

    mask = df["feature_date"].map(_lag) <= domain.MAX_FEATURE_LAG_TRADE_DAYS
    return df[mask]


def check_recommend_ready() -> bool:
    """推荐前置门控：Step 4（持仓下载 + 行业映射）产出就绪才允许推荐。

    推荐依赖“持仓→行业映射→RBSA 赛道”链路：Step 4 缺失/失败时可用赛道为 0，
    推荐必然空转并误记空推荐日（掩盖数据问题）。
    就绪判定经 repo.is_recommend_data_ready 单一谓词（异常兜底 False）；
    未就绪时取计数细节输出修复指引；管线槽位（run/run_recommend）与 CLI 入口共同消费，
    引擎入口不再自审自拦。
    """
    if repo.is_recommend_data_ready():
        return True
    try:
        status = repo.check_data_ready()
    except Exception as e:
        logger.error("数据就绪检查失败: %s，跳过推荐（请检查数据基座）", str(e)[:120])
        return False
    if status["holdings_cnt"] == 0:
        logger.error("持仓数据为空（fund_holdings 无记录）：持仓下载未完成或全部失败，推荐被拦截。"
                     "首次部署需等待数据基座自举（净值全量下载 → 持仓 → 行业映射），"
                     "请手动触发「数据基座」槽位或等待调度器续跑；"
                     "持续失败请查 data_fetch_failures 表定位接口限流/反爬")
        return False
    if status["industry_cnt"] == 0:
        logger.error("行业映射为空（stock_industry_map 无记录）：持仓已就绪但行业映射未成功。"
                     "行业映射依赖持仓股票的东财 F10/push2 接口（云服务器易被反爬限流），"
                     "请手动触发「数据基座」重试，或运行 python -m app.data.foundation --industry-map")
        return False
    if status["feature_cnt"] == 0:
        # 审计 P1-2：特征表为空/最近特征日无数据是数据故障，不是"今日无机会"。
        # 门控拦截（不记空推荐日）——空推荐日只应代表市场判断。
        logger.error("基金特征为空（fund_features 最近特征日无数据）：特征计算未完成或全部失败，"
                     "推荐被拦截（避免数据故障被包装成空推荐日）。请检查 app.data.foundation 特征槽位")
        return False
    return False


def run_recommendation(retrain: bool = False) -> None:
    """推荐引擎主入口：LLM 选赛道 → 赛道内排序 → LLM 定论 → 入库。

    前置数据门控由管线槽位 / CLI 入口执行（check_recommend_ready），
    本入口只消费就绪数据，不再自审自拦（架构深化候选 2）。

    阶段脉络（跨文件）：
    ┌── 阶段1: 宏观分析 + 选赛道 ──────────────────────────────
    │  build_macro_context(date_str) [macro_agent.py]
    │  ← 内部: sector_pool.py → prompts.py → client.call_llm
    │  输出: MacroContext (recommended_sectors, risk_sectors, regime_label)
    ├── 阶段2: 赛道内相对化排序 ───────────────────────────────
    │  _rank_within_sectors(ctx, model) [recommend.py]
    │  ← 内部: calculator.score_frame, domain.SectorPolicy
    │  输出: finalists (各赛道候选基金, 含 combo 跨赛道可比)
    ├── 阶段3: 逐赛道 LLM 终选定论 ────────────────────────────
    │  _llm_final_pick(sector_candidates, ctx, insights) [recommend.py]
    │  ← 内部: prompts.py → client.call_llm
    │  输出: selected_code + 论点锚点 snapshot
    ├── 阶段4: 落库 ──────────────────────────────────────────
    │  _save_recommendation(date_str, selected, ...) [recommend.py]
    │  _write_sector_selection(date_str, ctx, saved_id) [recommend.py]
    │  ← repo: insert_recommendation / insert_sector_selection
    └──────────────────────────────────────────────────────────
    各阶段输出经 MacroContext / finalists 等结构化对象传递，
    不依赖模块级全局变量；任一阶段可独立 mock 测试。
    """
    date_str = datetime.now().strftime("%Y-%m-%d")

    # 特征新鲜度护栏：数据基座失败时特征陈旧。陈旧 <=1 天用旧特征兑底（验证：Top-5 重合 80%）；
    # 滞后 >=2 天影响明显（Top-5 掉至 40%），强告警但仍放行（按用户决策：失败后重试仍失败则用旧特征）。
    feat_date = repo.get_latest_feature_date()
    lag = _feature_freshness(feat_date)
    if lag == 1:
        logger.warning("特征新鲜度：最新特征日期 %s 滞后 1 个交易日，用旧特征跑推荐（数据基座可能未更新）", feat_date)
    elif lag >= 2:
        logger.error("特征新鲜度：最新特征日期 %s 滞后 %d 个交易日——数据基座连续失败，推荐将基于严重陈旧特征",
                     feat_date, lag)

    insights = _load_insights()

    model = get_or_train(retrain)
    if model is None:
        return

    logger.info("=== LLM 宏观分析 + 选赛道 ===")
    ctx = build_macro_context(date_str)
    llm_regime = domain.normalize_regime_label(ctx.regime_label)
    logger.info("选定赛道: %s | 回避: %s | 大盘: %s",
                ctx.recommended_sectors, ctx.risk_sectors, ctx.regime_label)

    # LLM 推荐的赛道全部纳入考虑（不截断前 2 个）：无候选/质量不达标的赛道顺延补位，
    # 避免"前 2 个赛道恰无候选或候选质量差"时白白浪费推荐名额、甚至只推全池最弱基金。
    MAX_PICKS = 2
    target_sectors = [s for s in ctx.recommended_sectors if s != "其他"]
    if not target_sectors:
        # 审计 P1-2：LLM 判定无适合赛道 = 市场判断（合法空推荐日）
        repo.record_empty_recommendation(
            date_str, ctx.sector_reasoning or "今日无合适机会", reason_type="no_opportunity")
        logger.info("今日无合适机会，记录空推荐日（no_opportunity）")
        return
    logger.info("=== 赛道内相对化排序 ===")
    finalists, reco_path = _rank_within_sectors(ctx, model)
    if not finalists:
        # 空推原因分层（审计 P1-2）：特征严重陈旧（数据基座连续失败）= data_failure，
        # 引擎侧此前不可达——全市场无正预测/候选全被滤时被误记 no_opportunity；
        # 赛道正常但无候选/无正预测 = no_opportunity（合法决策）。
        reason_type = "data_failure" if lag >= 2 else "no_opportunity"
        repo.record_empty_recommendation(
            date_str, ctx.sector_reasoning or "候选基金为空（赛道无匹配基金或动量护栏过滤）",
            reason_type=reason_type)
        logger.info("无候选基金（无匹配赛道或动量护栏过滤），记录空推荐日（%s，特征滞后 %d 交易日）",
                    reason_type, lag)
        return
    logger.info("候选 %d 只: %s",
                len(finalists),
                ", ".join(f"{f['code']}({f.get('sector','?')},combo={f['combo']:.3f})"
                          for f in finalists))

    count = 0
    selected_codes: set[str] = set()
    # 全池最优 combo（跨赛道可比）：赛道候选显著低于该值时视为质量不达标，跳过该赛道。
    best_combo = max((f["combo"] for f in finalists), default=0.0)
    for idx, sector in enumerate(target_sectors):
        if count >= MAX_PICKS:
            break
        # 降级路径只在首个分组执行一次（全市场池无赛道分组，后续赛道无新候选）
        if reco_path == "degrade" and idx > 0:
            break
        sector_candidates = _pick_group_candidates(finalists, sector, reco_path, selected_codes)
        if not sector_candidates:
            logger.warning("赛道 [%s] 无可投基金，跳过", sector)
            continue
        # 候选质量门槛：赛道内最佳 combo 低于全池最优的 60% 时放弃该赛道，
        # 避免"赛道顺序 + 唯一候选"推选出全池最弱基金（如赛道仅 1 只候选且 combo 垫底）。
        sector_best = max((c["combo"] for c in sector_candidates), default=0.0)
        if sector_best < best_combo * domain.RankingConfig.QUALITY_RATIO:
            logger.warning("赛道 [%s] 候选质量偏低(combo %.3f < 全池最优的%.0f%% %.3f)，跳过",
                           sector, sector_best,
                           domain.RankingConfig.QUALITY_RATIO * 100, best_combo * domain.RankingConfig.QUALITY_RATIO)
            continue

        logger.info("=== LLM 最终定论 [%d/2 %s] (%d 只候选) ===",
                    idx + 1, sector, len(sector_candidates))
        # 终选定论恒由 LLM 执行：发挥宏观/持仓/新闻综合判断优势。
        # 裁决损耗观测（选中 vs 候选池均值）只回流元分析自省，不做"纯量化降级"
        # ——自动降级会让系统突然失去 LLM 判断，违背设计初衷（Q8 曾讨论后否定）。
        result = _llm_final_pick(sector_candidates, ctx, insights)
        # 一旦被某赛道选定，后续赛道不再重复推荐该基金（同日去重）
        selected_codes.add(result["selected_code"])

        selected = {
            "selected_code": result["selected_code"],
            "selected_name": result["selected_name"],
            "reason": result.get("reason", ""),
        }
        vetoed = result.get("vetoed", [])
        logger.info("LLM 选定 [%s]: %s %s | 否决 %d 只",
                    sector, selected["selected_code"], selected["selected_name"], len(vetoed))

        guard_cfg = repo.get_ranking_cfg()
        # 冗余防御：候选池已在 _rank_within_sectors 按 guard 过滤，LLM 只能从池内选择，
        # 此拦截正常情况下永不触发；保留作为纵深防御（若未来 LLM 候选池外选择）。
        sel_momentum = next(
            (float(c.get("momentum_20d", 0)) for c in sector_candidates
             if c["code"] == selected["selected_code"]), None)
        if sel_momentum is not None and not guard_cfg.passes_momentum_guard(sel_momentum):
            logger.warning("风控拦截 [%s]: %s 近20日动量 %.1f%% 低于阈值 %.0f%%",
                           sector, selected["selected_code"], sel_momentum, guard_cfg.momentum_guard_pct)
            repo.insert_recommendation(
                date_str, selected["selected_code"], selected["selected_name"],
                0, 0.0, 0.0, llm_regime,
                f"风控拦截: 20日动量{sel_momentum:.1f}% 低于阈值{guard_cfg.momentum_guard_pct:.0f}%",
                status=domain.SIGNAL_REJECT, reco_path=reco_path,
            )
            logger.info("风控拦截已入库: %s", selected["selected_code"])
            continue

        sel_features = next(
            (c for c in sector_candidates if c["code"] == selected["selected_code"]), {})
        # D3：推荐时持久化论点锚点（核心重仓股 + 报告期），供 R4 结构证伪比对
        anchor_holdings = repo.get_holdings(selected["selected_code"], 5)
        feature_snapshot = json.dumps({
            "sector": sel_features.get("sector", ""),
            "rbsa_industry_1": sel_features.get("rbsa_industry_1", ""),
            "rbsa_weight_1": sel_features.get("rbsa_weight_1", 0) or 0,
            "rbsa_industry_2": sel_features.get("rbsa_industry_2", ""),
            "rbsa_weight_2": sel_features.get("rbsa_weight_2", 0) or 0,
            "rbsa_industry_3": sel_features.get("rbsa_industry_3", ""),
            "rbsa_weight_3": sel_features.get("rbsa_weight_3", 0) or 0,
            "momentum_20d": sel_features.get("momentum_20d", 0),
            "hurst_60d": sel_features.get("hurst_60d", 0),
            "calmar": sel_features.get("calmar", 0),
            "sector_rel_momentum": sel_features.get("sector_rel_momentum", 0),
            "sector_rel_calmar": sel_features.get("sector_rel_calmar", 0),
            "top_holdings": [
                {"stock_code": h["stock_code"], "stock_name": h["stock_name"], "weight": h["weight"]}
                for h in anchor_holdings
            ],
            "holdings_report_date": repo.get_latest_holdings_date(selected["selected_code"]),
            # R2c 版本边界：记录买入时模型版本，监控侧跨版本时跳过相对买入分比较
            "model_version": model_version(),
        }, ensure_ascii=False)

        new_rows = fetch_fund_nav_incremental(selected["selected_code"])
        if new_rows:
            logger.info("净值同步: %s 新增 %d 条", selected["selected_code"], new_rows)

        saved_id = _save_recommendation(
            date_str, selected, sector_candidates, vetoed, llm_regime, feature_snapshot,
            reco_path=reco_path,
        )
        _write_sector_selection(date_str, ctx, saved_id)
        count += 1

    if count == 0:
        # 全部赛道均无候选或被质量门槛过滤：记录空推荐日（reasoning 说明原因便于回溯）
        repo.record_empty_recommendation(
            date_str, ctx.sector_reasoning or "全部推荐赛道无候选或候选质量不达标")
        logger.info("全部赛道无候选或候选质量不达标，记录空推荐日")

    logger.info("推荐流程完成: 赛道 %d 个 → 入库 %d 条",
                len(target_sectors), count)


def _write_sector_selection(date_str: str, ctx: MacroContext,
                            log_id: int, sector_name: str | None = None) -> None:
    # P1-5 否决反事实度量：量化池内全部候选赛道随赛道选择一并持久化，
    # 结算时逐赛道回看 40 日收益，度量 LLM 否决/未选是否系统性错过上涨赛道。
    pool_sectors = ([c["sector"] for c in ctx.candidate_sectors]
                    if getattr(ctx, "candidate_sectors", None) else None)
    repo.insert_sector_selection(
        date_str, log_id, ctx.recommended_sectors, ctx.risk_sectors,
        ctx.sector_reasoning, ctx.regime_label,
        used_insight_ids=ctx.used_sector_insight_ids,
        pool_sectors=pool_sectors,
    )


if __name__ == "__main__":
    import sys
    retrain = "--retrain" in sys.argv
    if check_recommend_ready():
        run_recommendation(retrain=retrain)

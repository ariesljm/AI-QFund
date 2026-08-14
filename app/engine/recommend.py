"""推荐引擎：LLM 选赛道 → LightGBM 赛道内排 → LLM 定论（Phase 2 重构）。

漏斗：准备标注数据 → 训练 LightGBM → 宏观LLM选赛道
      → 赛道内相对化排序 → 持仓+新闻交叉验证 → LLM终选定论 → 入库。

依赖 data_foundation 的 DB 连接与特征计算结果（fund_features 表）。
运行：uv run python recommend.py
"""

from app.repo import meta_keys as META
import json
from app.utils.log import get_logger
from datetime import datetime
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

from app.features.calculator import (score_frame,
                                      apply_momentum_guard,
                                      market_state_features)
from app.llm.macro_agent import build_macro_context, MacroContext
from app.llm.client import call_llm, parse_llm_json
from app.llm.prompts import final_pick_prompt, final_pick_system_prompt
from app import domain
from app.utils.trading_calendar import trading_day_lag  # 滞后交易日数单一来源
from app.model import get_or_train
import app.repo as repo

logger = get_logger("recommend")

FEATURE_COLS = repo.FEATURE_COLS
_FORWARD_WINDOW = repo.FORWARD_WINDOW


def _load_ranking_cfg() -> domain.RankingConfig:
    """从 meta 表读取排序权重，找不到则用默认值（返回不可变 RankingConfig）。"""
    return repo.get_ranking_cfg()


# ========== 2.1 标注数据准备 ==========


# ========== 赛道内排序 ==========

# 赛道名解析与锚定已收敛为 domain.SectorPolicy（推荐/监控/宏观共用单一来源，
# 见架构深化候选 1）；本引擎只消费判定结果，不再自行 resolve。

def _index_momentum() -> float:
    return repo.get_index_momentum()


def _inject_market_cols(df: pd.DataFrame) -> pd.DataFrame:
    """注入市场状态列（R1）：指数 20 日动量/波动率 + 当日大盘状态，全行共享。

    打分与训练同口径；regime 同步刷为当日状态机——特征快照滞后数日时
    （数据基座停摆后）fund_features.regime 是旧状态，权重调整会错配。
    """
    df = df.copy()
    df["regime"] = _get_market_regime()
    idx_rows = repo.get_index_series("sh000300", ("date", "close", "volume"))
    if idx_rows:
        closes = np.array([r[1] for r in idx_rows], dtype=float)
        vols = np.array([r[2] for r in idx_rows], dtype=float)
        mkt = market_state_features(closes, vols)
        for c, v in mkt.items():
            df[c] = v
    else:
        for c in repo.MARKET_COLS:
            df[c] = 0.0
    return df


def _get_market_regime() -> str:
    """从沪深300收盘价 vs MA60 判断大盘状态：BULL/BEAR。"""
    return repo.get_market_regime()


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


def _rank_within_sectors(ctx: MacroContext, model: lgb.Booster) -> list[dict]:
    """在 LLM 选中的赛道内，用赛道相对化特征排序，前 2 赛道各取 Top 2、其余各 Top 1。"""
    raw_sectors = ctx.recommended_sectors
    if not raw_sectors:
        logger.info("无指定赛道，降级为全市场 Top 10")
        return rank_funds(model)

    policy = domain.SectorPolicy(repo.get_available_sectors())
    sectors = policy.resolve(raw_sectors)
    if not sectors:
        logger.info("所有赛道均未匹配RBSA行业，降级为全市场 Top 10")
        return rank_funds(model)
    dropped = set(raw_sectors) - set(sectors)
    if dropped:
        logger.info("赛道 %s 未匹配到RBSA行业，跳过", "、".join(sorted(dropped)))
    logger.info("LLM赛道 %s → 匹配到 %s", raw_sectors, sectors)
    risk_set = policy.resolve_set(ctx.risk_sectors)

    rows = repo.get_sector_candidates(sectors)
    if not rows:
        logger.info("赛道内无匹配基金，降级为全市场 Top 10")
        return rank_funds(model)

    df = pd.DataFrame(rows)
    df = df.dropna(subset=FEATURE_COLS)
    if df.empty:
        return rank_funds(model)

    # C4 赛道纯度门槛：第一行业暴露 <10% 的基金不视为赛道基金（口径见 domain）。
    df = df[df["rbsa_weight_1"] >= domain.MIN_SECTOR_EXPOSURE]
    if df.empty:
        logger.info("赛道纯度门槛后无候选，降级为全市场 Top 10")
        return rank_funds(model)

    # 赛道归属锚定第一行业（推荐/监控同口径）：
    # 基金只有第一行业命中推荐赛道才入选，feature_snapshot.sector = rbsa_industry_1；
    # 监控 R3a 以第一行业锚定 —— 避免基金以次要行业入选、监控按第一行业否决的
    # 错配（8-05 004936 实例：第3行业=贵金属入选，第一行业=基本金属被监控误伤 WARNING）。
    df = df[df["rbsa_industry_1"].isin(sectors)]
    if df.empty:
        logger.info("第一行业无匹配赛道，降级为全市场 Top 10")
        return rank_funds(model)
    expanded = []
    for _, r in df.iterrows():
        row = r.to_dict()
        row["sector"] = row["rbsa_industry_1"]
        row["rbsa_weight"] = row.get("rbsa_weight_1", 0) or 0
        expanded.append(row)
    df = pd.DataFrame(expanded)

    # 回避赛道整体过滤：第一行业命中回避赛道的基金直接剔除，
    # 防止基金以次要行业身份入选、监控按第一行业判定后被否决（推荐/监控赛道不一致）
    df = df[~df["rbsa_industry_1"].isin(risk_set)]
    df = df[~df["sector"].isin(risk_set)]
    if df.empty:
        logger.info("回避赛道过滤后无候选，降级为全市场 Top 10")
        return rank_funds(model)
    df = _add_sector_relatives(df)
    cfg = _load_ranking_cfg()
    df = apply_momentum_guard(df, cfg)

    idx_mom = _index_momentum()
    df = _inject_market_cols(df)
    df = score_frame(
        df, model, cfg, idx_mom,
        default_regime=_get_market_regime(),
        sector_rel_momentum_col="sector_rel_momentum",
        sector_rel_calmar_col="sector_rel_calmar",
        rbsa_weight_col="rbsa_weight",
    )
    # 全天候出手：不做预测分硬过滤（R1 目标=绝对收益，按 r̂ 横截面取 TopN；
    # 熊市不因"预测收益为负"清空候选池，风险由监控防线兜底）
    if df.empty:
        logger.info("赛道内无候选基金，降级为全市场 Top 10")
        return rank_funds(model)

    # 候选构建：每赛道取 Top2；前 2 赛道（run_recommendation 的 LLM 定论对象）
    # 保底 2 只且不参与全局截断，保证 LLM 终选定论面对真实选择（候选池 ≥2）而非 1 选 1 盖章；
    # 其余赛道各保底 1 只，按 combo 排序后截断（只影响后序赛道，LLM 定论不受影响）。
    # 保底防挤出：高热度赛道可能因量化 combo 略低被全局 topN 整体挤出，
    # 导致下游误判"赛道无可投基金"（历史根因 2026-08-02）。
    MAX_CANDIDATES = 8
    core: list = []  # 前 2 赛道各 2 只
    rest: list = []  # 其余赛道各 1 只
    for idx, sector in enumerate(sectors):
        sdf = df[df["sector"] == sector].sort_values("combo", ascending=False)
        take = 2 if idx < 2 else 1
        # 同基金多份额只保留 combo 最高者（去份额名去重），再取前 take 只
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
    top_per_sector = core + rest[:MAX_CANDIDATES - len(core)]
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
    rows = repo.get_all_ranking_rows()
    df = pd.DataFrame(rows)
    df = df.dropna(subset=FEATURE_COLS)
    if df.empty:
        return []
    df = apply_momentum_guard(df, cfg)

    idx_mom = _index_momentum()
    df = _inject_market_cols(df)
    df = score_frame(df, model, cfg, idx_mom,
                     default_regime=_get_market_regime(),
                     rbsa_weight_col="rbsa_weight_1")
    # 全天候出手：与赛道内排序一致，不做预测分硬过滤（风险由监控防线兜底）
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


def _llm_final_pick(candidates: list[dict], ctx: MacroContext, insights: list) -> dict:
    """LLM 基于重仓股+CLS新闻匹配+持仓时效性做最终选择，返回选定基金和否决记录。"""
    latest_feature_date = repo.get_latest_feature_date()

    for c in candidates:
        c["holdings"] = repo.get_holdings(c["code"], 5)

        report_date = repo.get_latest_holdings_date(c["code"])
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
        # 近1月涨幅（22 交易日，与前端 period_returns 展示口径一致，供 LLM reason 文案引用）
        try:
            _nav_rows = repo.nav.series(c["code"], limit=30)
            if len(_nav_rows) >= 23 and _nav_rows[-1][1]:
                c["ret_1m"] = round((_nav_rows[-1][1] / _nav_rows[-23][1] - 1) * 100, 1)
            else:
                c["ret_1m"] = None
        except Exception:
            c["ret_1m"] = None
        if sector and latest_feature_date:
            median = repo.get_sector_momentum_median(sector, latest_feature_date)
            if median is not None:
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

    content = call_llm(prompt, system_prompt=system_prompt, max_tokens=16384,
                       caller="recommend_final_pick")
    # call_llm 技术失败已统一抛 LLMError（候选 7），此处不再自行判断 None

    valid_codes = {c["code"]: c["name"] for c in candidates}
    result = _parse_llm_result(content, valid_codes)
    if result is not None:
        return result

    raise RuntimeError(f"LLM最终定论返回无法解析: {content[:300]}")


def _parse_llm_result(content: str, valid_codes: dict) -> dict | None:
    parsed = parse_llm_json(content)
    if not isinstance(parsed, dict):
        return None
    result = {str(k).strip(". "): v for k, v in parsed.items()}
    code = str(result.get("selected_code") or "")
    if code in valid_codes:
        return {
            "selected_code": code,
            "selected_name": result.get("selected_name", valid_codes[code]),
            "reason": result.get("reason", ""),
            # P2-7 决策与文案解耦：decision_logic 为内部决策依据（审计用），
            # 与展示文案 reason 分离；旧 prompt 无该字段时为空串（兼容）
            "decision_logic": result.get("decision_logic", ""),
            "vetoed": result.get("vetoed", []),
        }
    return None


# ========== 推荐入库 ==========

_LAST_RECO_PATH = Path("data/last_recommendation.txt")


def _dump_recommendation(date_str: str, code: str, name: str, rank: int, score: float,
                        regime: str, candidates: list[dict], vetoed: list[dict],
                        clear: bool = False) -> None:
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
    # P2-7：决策逻辑字段单独追加保存（展示层读 buy_reason 时按 ' | 否决记录:' 截断，
    # 决策逻辑不会误入前端文案）
    decision_logic = selected.get("decision_logic", "")
    if decision_logic:
        reason = reason + " | 决策逻辑: " + str(decision_logic)[:500]
    real_name = repo.get_fund_name(selected["selected_code"]) or selected["selected_name"]
    entry_nav = repo.nav.latest(selected["selected_code"])
    # Q5 裁决损耗观测：落库当日候选池代码（LLM 面对的选择集），质量度量时回查 20 日收益
    new_id = repo.insert_recommendation(
        date_str, selected["selected_code"], real_name, rank, score, combo, regime,
        reason, status=domain.SIGNAL_HOLD, feature_snapshot=feature_snapshot,
        entry_nav=entry_nav, candidate_codes=[c["code"] for c in candidates],
    )
    # 同日推荐成功：清掉可能的空推荐残留（同一天先判无赛道、后成功推荐的场景）
    repo.clear_empty_recommendation(date_str)
    logger.info("推荐入库: %s %s (排名%d, 分数%.4f, id=%d)",
                selected["selected_code"], real_name, rank, score or 0.0, new_id)
    _dump_recommendation(date_str, selected["selected_code"], real_name, rank, score,
                          regime, candidates, vetoed, clear=clear)
    return new_id


def _feature_freshness(feat_date: str | None) -> int:
    """特征日期距期望值的滞后交易日数（0=新鲜）。

    基准取交易日历缓存：今天为交易日时期望特征日期=昨交易日（盘前数据任务拉的是
    T-1 净值）；今天非交易日时期望=之前最近交易日。无缓存/解析失败返回 0（不误报）。
    滞后计数用 trading_day_lag（与净值停更打标共用单一来源）。
    """
    if not feat_date:
        return 0
    raw = repo.get_meta(META.TRADE_DATES_CACHE)
    if not raw:
        return 0
    try:
        days = set(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        return 0
    today = datetime.now().date().isoformat()
    if today in days:
        expected = max((d for d in days if d < today), default=None)  # 今交易，期望 T-1
    else:
        expected = max((d for d in days if d <= today), default=None)  # 非交易日，期望最近交易日
    if expected is None or feat_date >= expected:
        return 0
    return trading_day_lag(feat_date, expected, days=days)


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
    return False


def run_recommendation(retrain: bool = False) -> None:
    """推荐引擎主入口：LLM 选赛道 → 赛道内排序 → LLM 定论 → 入库。

    前置数据门控由管线槽位 / CLI 入口执行（check_recommend_ready），
    本入口只消费就绪数据，不再自审自拦（架构深化候选 2）。
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
        repo.record_empty_recommendation(date_str, ctx.sector_reasoning or "今日无合适机会")
        logger.info("今日无合适机会，记录空推荐日")
        return
    logger.info("=== 赛道内相对化排序 ===")
    finalists = _rank_within_sectors(ctx, model)
    if not finalists:
        repo.record_empty_recommendation(
            date_str, ctx.sector_reasoning or "候选基金为空（赛道无匹配基金或动量护栏过滤）")
        logger.info("无候选基金（无匹配赛道或动量护栏过滤），记录空推荐日")
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
        sector_candidates = _sector_candidates(finalists, sector, selected_codes)
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

        guard_cfg = _load_ranking_cfg()
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
                status=domain.SIGNAL_REJECT,
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
        }, ensure_ascii=False)

        new_rows = repo.refresh_nav(selected["selected_code"])
        if new_rows:
            logger.info("净值同步: %s 新增 %d 条", selected["selected_code"], new_rows)

        saved_id = _save_recommendation(
            date_str, selected, sector_candidates, vetoed, llm_regime, feature_snapshot,
            clear=(idx == 0),
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
    # 结算时逐赛道回看 20 日收益，度量 LLM 否决/未选是否系统性错过上涨赛道。
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

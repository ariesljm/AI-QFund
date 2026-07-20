"""推荐引擎：LightGBM 打分 + LLM 否决链路（Phase 2 实现）。

漏斗：准备标注数据 → 训练 LightGBM → 对所有可投基金打分取 Top 10
      → 宏观摘要 + LLM 顺位否决 → 唯一推荐写入 recommend_log。

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

from data_foundation import _get_db, _load_settings, calc_regime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("recommend")

# 模型与特征定义
MODEL_PATH = Path("models/lgb_model.txt")
FEATURE_COLS = [
    "hurst_60d", "momentum_20d", "calmar", "downside_vol",
    "capture_up", "capture_down", "bias_60d", "rbsa_weight_1",
    "etf_flow_slope_5d",
]
# 每只基金用于训练的历史样本点上限（控制规模，避免全历史爆炸）
_MAX_SAMPLES_PER_FUND = 60
# 标签前瞻窗口（交易日）
_FORWARD_WINDOW = 20


# ========== 2.1 标注数据准备 ==========

def _features_from_window(navs: np.ndarray, idx_closes: np.ndarray,
                          idx_volumes: np.ndarray) -> dict | None:
    """从截至样本点的净值/指数窗口计算 FEATURE_COLS 特征。

    与 data_foundation.calc_features 口径一致，但输入为截取窗口，
    用于历史样本点配对标签。特征不足时返回 None。
    """
    if len(navs) < 60:
        return None
    with np.errstate(divide="ignore", invalid="ignore"):
        returns = np.diff(navs) / navs[:-1]
    returns = returns[np.isfinite(returns)]

    feat: dict = {}
    window = min(60, len(returns))
    feat["hurst_60d"] = float(_calc_hurst(returns[-window:]))
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
    if len(idx_volumes) >= 5:
        vw = idx_volumes[-5:][idx_volumes[-5:] > 0]
        if len(vw) >= 2:
            feat["etf_flow_slope_5d"] = float(np.polyfit(np.arange(len(vw), dtype=float), np.log(vw), 1)[0])
        else:
            feat["etf_flow_slope_5d"] = 0.0
    else:
        feat["etf_flow_slope_5d"] = 0.0
    feat["rbsa_weight_1"] = 0.0  # 训练期无持仓快照，置 0（推理期用最新持仓）
    return feat


def _calc_hurst(series: np.ndarray, max_lag: int = 20) -> float:
    """重算 Hurst（与 data_foundation.calc_hurst 同口径，避免跨模块耦合）。"""
    lags = range(2, min(max_lag, len(series)) + 1)
    rs = []
    for lag in lags:
        sub = series[:lag]
        mean = np.mean(sub)
        dev = sub - mean
        cumdev = np.cumsum(dev)
        r = np.max(cumdev) - np.min(cumdev)
        s = np.std(sub)
        if r > 0 and s > 0:
            rs.append(np.log(r / s))
    if len(rs) < 2:
        return 0.5
    x = np.log(list(lags)[:len(rs)])
    return float(np.polyfit(x, rs, 1)[0])


def prepare_lgb_training_data() -> tuple[pd.DataFrame, pd.Series]:
    """构建 LightGBM 训练集。

    ponytail: fund_features 仅存每只基金最新一天特征，无法历史配对。
    改为每只基金取「历史上能算出未来20日标签的最近时点」，用该时点前的
    净值窗口重算特征作为 X，Y = 该时点后20日相对沪深300超额收益。
    每只基金贡献 1 个严格配对的样本，规模约万级且避免全历史重算爆炸。
    """
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
        # 历史上能算标签的最近时点（pos 为样本日位置）
        last_pos = len(g) - 1 - _FORWARD_WINDOW
        d = g.index[last_pos]
        d20 = g.index[last_pos + _FORWARD_WINDOW]
        fund_fwd = g[d20] / g[d] - 1.0
        if d not in idx_ret_fwd.index or pd.isna(idx_ret_fwd[d]):
            continue
        y = fund_fwd - idx_ret_fwd[d]
        # 对齐该时点的指数窗口（取 d 及之前60日 + 用于 etf 斜率的近5日）
        idx_pos = idx_close.index.get_indexer([d])[0]
        if idx_pos < 0 or idx_pos < 60:
            continue
        idx_closes_w = idx_close.iloc[idx_pos - 59: idx_pos + 1].to_numpy(dtype=float)
        idx_vols_w = idx_vol.iloc[idx_pos - 59: idx_pos + 1].to_numpy(dtype=float)
        feat = _features_from_window(g.iloc[: last_pos + 1].to_numpy(dtype=float),
                                     idx_closes_w, idx_vols_w)
        if feat is None or any(pd.isna(v) for v in feat.values()):
            continue
        X_list.append(feat)
        y_list.append(y)

    X = pd.DataFrame(X_list, columns=FEATURE_COLS)
    y = pd.Series(y_list, name="alpha_20d")
    logger.info("训练集构建完成: 样本 %d 条, 特征 %d 维", len(X), len(FEATURE_COLS))
    return X, y


# ========== 2.2 模型训练 ==========

def train_lgb_model(X: pd.DataFrame, y: pd.Series) -> lgb.Booster:
    """训练 LightGBM 排序模型并保存为文件。"""
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    params = {
        "objective": "regression",
        "metric": "rmse",
        "learning_rate": 0.05,
        "num_leaves": 31,
        "min_data_in_leaf": 20,
        "feature_fraction": 0.9,
        "verbose": -1,
        "seed": 42,
    }
    # 轻量训练：全量即训练集（无独立验证集，规模已受样本点上限约束）
    train_data = lgb.Dataset(X, label=y)
    booster = lgb.train(params, train_data, num_boost_round=200)
    booster.save_model(str(MODEL_PATH))
    logger.info("LightGBM 模型已保存: %s", MODEL_PATH)
    return booster


# ========== 2.3 Top 10 筛选 ==========

def rank_funds(model: lgb.Booster) -> list[dict]:
    """对所有可投基金用模型打分，返回 Top 10（剔除特征缺失/异常）。"""
    conn = _get_db()
    rows = conn.execute(
        "SELECT ff.code, fb.name, ff.regime, "
        f"{', '.join('ff.' + c for c in FEATURE_COLS)} "
        "FROM fund_features ff "
        "JOIN fund_basic fb ON fb.code = ff.code "
        "WHERE fb.is_buyable = 1"
    ).fetchall()

    cols = ["code", "name", "regime"] + FEATURE_COLS

    df = pd.DataFrame(rows, columns=cols)
    # 剔除特征缺失或无穷大的行
    df = df.dropna(subset=FEATURE_COLS)
    if df.empty:
        return []

    X = df[FEATURE_COLS].astype(float)
    scores = model.predict(X)
    df = df.copy()
    df["score"] = scores
    # 剔除明显异常（score 非有限值）
    df = df[np.isfinite(df["score"])]
    top = df.sort_values("score", ascending=False).head(10)
    candidates = []
    for _, r in top.iterrows():
        candidates.append({
            "code": r["code"],
            "name": r["name"],
            "regime": r["regime"],
            "score": float(r["score"]),
            "hurst_60d": float(r["hurst_60d"]),
            "momentum_20d": float(r["momentum_20d"]),
            "calmar": float(r["calmar"]),
            "rbsa_industry_1": r.get("rbsa_industry_1", ""),
            "rbsa_weight_1": float(r.get("rbsa_weight_1", 0.0) or 0.0),
        })
    return candidates


# ========== 2.4 宏观摘要 ==========

import requests as _requests

# 实时财经数据源（免密钥，直接抓取）
_BOARD_URL = ("https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=30&po=1&np=1"
              "&fltt=2&invt=2&fid=f3&fs=m:90+t:2&fields=f12,f14,f3,f62")
_KUAXUN_URL = "https://newsapi.eastmoney.com/kuaixun/v1/getlist_102_1_20_1_.html"


def _http_get(url: str, timeout: float = 12) -> str:
    s = _requests.Session()
    s.trust_env = False
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": "https://finance.eastmoney.com/",
    })
    return s.get(url, timeout=timeout).text


def fetch_daily_news(date_str: str) -> dict:
    """抓取当日行业涨跌排行 + 财经快讯，写入 macro_news 表并返回摘要。

    数据来源：东方财富板块排行（领涨/领跌行业）、东方财富快讯（政策/新闻）。
    返回结构与 get_macro_summary 一致。
    """
    # 1. 行业板块排行（f14=行业名, f3=涨跌幅%, f62=主力净流入额）
    top_gainers, top_losers, etf_net_flow = "", "", ""
    try:
        txt = _http_get(_BOARD_URL)
        data = json.loads(txt)
        diff = data.get("data", {}).get("diff", [])
        if diff:
            sorted_by_chg = sorted(diff, key=lambda x: x.get("f3", 0), reverse=True)
            gainers = sorted_by_chg[:5]
            losers = sorted_by_chg[-5:][::-1]
            top_gainers = "、".join(f"{d['f14']}({d.get('f3',0):+.2f}%)" for d in gainers)
            top_losers = "、".join(f"{d['f14']}({d.get('f3',0):+.2f}%)" for d in losers)
            # ETF净流入代理：取主力净流入额最大的行业
            by_flow = max(diff, key=lambda x: x.get("f62", 0) or 0)
            etf_net_flow = f"{by_flow['f14']}: {by_flow.get('f62',0):,.0f}元"
    except Exception as e:
        logger.warning("板块排行抓取失败: %s", e)

    # 2. 财经快讯（政策/新闻摘要）
    news = ""
    try:
        txt = _http_get(_KUAXUN_URL)
        # 响应形如 var 1={...}
        if txt.startswith("var"):
            txt = txt.split("=", 1)[1]
        data = json.loads(txt)
        items = data.get("LivesList", []) or []
        headlines = []
        for it in items[:15]:
            title = it.get("title") or it.get("content") or ""
            if title:
                headlines.append(title.strip())
        news = "；".join(headlines)
    except Exception as e:
        logger.warning("财经快讯抓取失败: %s", e)

    conn = _get_db()
    conn.execute(
        "INSERT OR REPLACE INTO macro_news "
        "(date, news_summary, top_gainers, top_losers, etf_net_flow) "
        "VALUES (?, ?, ?, ?, ?)",
        (date_str, news, top_gainers, top_losers, etf_net_flow),
    )
    conn.commit()
    conn.close()
    logger.info("宏观摘要入库: 领涨[%s] 领跌[%s] 新闻%d字",
                top_gainers[:40], top_losers[:40], len(news))
    return {
        "news": news,
        "top_gainers": top_gainers,
        "top_losers": top_losers,
        "etf_net_flow": etf_net_flow,
    }


def get_macro_summary(date_str: str) -> dict:
    """获取当日宏观摘要与资金流向。

    优先读取 macro_news 表当日记录；无记录则实时抓取东财行业排行与快讯并入库。
    """
    conn = _get_db()
    row = conn.execute(
        "SELECT news_summary, top_gainers, top_losers, etf_net_flow "
        "FROM macro_news WHERE date = ?", (date_str,)
    ).fetchone()
    conn.close()
    if row and (row[0] or row[1] or row[2]):
        return {
            "news": row[0] or "",
            "top_gainers": row[1] or "",
            "top_losers": row[2] or "",
            "etf_net_flow": row[3] or "",
        }
    # 无当日数据 → 实时抓取
    return fetch_daily_news(date_str)


# ========== 2.5 LLM 顺位否决 ==========

def _load_rules(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT rule FROM evolution_rules WHERE active = 1 ORDER BY id"
    ).fetchall()
    return [r[0] for r in rows]


def _build_prompt(candidates: list[dict], regime: str, macro: dict, rules: list) -> str:
    lines = [
        "【系统状态与规则】",
        f"大盘状态: {regime}",
        "策略侧重: " + (
            "赫斯特指数/向上捕获率/动量因子" if regime == "BULL"
            else "卡玛比率/向下捕获率/BIAS超跌反弹"
        ),
        "核心宪法（进化规则）:",
    ]
    lines += [f"  - {r}" for r in rules] or ["  - （暂无）"]
    lines += [
        "",
        "【今日宏观摘要与资金流向】",
        f"财经新闻: {macro['news']}",
        f"领涨行业: {macro['top_gainers']}",
        f"领跌行业: {macro['top_losers']}",
        f"ETF净流入: {macro['etf_net_flow']}",
        "",
        "【候选名单（按超额收益动能排序）】",
    ]
    for i, c in enumerate(candidates, 1):
        lines.append(
            f"第{i}名: {c['code']} {c['name']} | RBSA行业: {c.get('rbsa_industry_1','')}"
            f":{c.get('rbsa_weight_1',0):.2f} | 卡玛: {c['calmar']:.2f} | "
            f"Hurst: {c['hurst_60d']:.2f} | 20日动量: {c['momentum_20d']:.1f}%"
        )
    lines += [
        "",
        "【任务指令】",
        "从第1名开始向下逐位验证：",
        "1. 其重仓行业是否符合当日宏观资金流向？",
        "2. 是否存在明确的政策利空或行业性风险？",
        "3. 是否违背核心宪法中的任何一条？",
        "若验证通过（无利空+行业符合），立即选定该基金并停止。",
        "若有明确利空或违背宪法，行使否决权并记录理由，继续验证下一位。",
        "请严格输出以下JSON格式：",
        '{',
        '  "selected_code": "选中的基金代码",',
        '  "selected_name": "选中的基金名称",',
        '  "reason": "选定理由",',
        '  "vetoed": [',
        '    {"code": "被否决代码", "name": "被否决名称", "reason": "否决理由"}',
        '  ]',
        '}',
    ]
    return "\n".join(lines)


def llm_veto(candidates: list[dict], regime: str, macro: dict, rules: list) -> dict:
    """调用 LLM 执行顺位否决，返回选定基金和否决记录。

    若未配置 api_key，则降级：直接选第1名，reason 标注 LLM 未配置。
    """
    settings = _load_settings()
    llm_cfg = settings.get("llm", {})
    api_key = llm_cfg.get("api_key", "")
    if not api_key:
        logger.warning("LLM api_key 未配置，降级为直接选取 Top1")
        top = candidates[0]
        return {
            "selected_code": top["code"],
            "selected_name": top["name"],
            "reason": "LLM 未配置，按模型打分直接选取第1名",
            "vetoed": [],
        }

    from openai import OpenAI
    client = OpenAI(base_url=llm_cfg.get("base_url"), api_key=api_key)
    prompt = _build_prompt(candidates, regime, macro, rules)
    valid_codes = {c["code"]: c["name"] for c in candidates}
    last_err = None
    # 限流重试：429 指数退避，最多 3 次
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=llm_cfg.get("model", "gpt-4o-mini"),
                messages=[
                    {"role": "system", "content": "你是量化基金推荐决策助手，只输出JSON。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=llm_cfg.get("temperature", 0.2),
                max_tokens=llm_cfg.get("max_tokens", 1024),
                response_format={"type": "json_object"},
            )
            content = resp.choices[0].message.content
            result = _parse_llm_result(content, valid_codes)
            if result is not None:
                return result
            raise ValueError("LLM 返回无法解析为有效选定")
        except Exception as e:
            last_err = e
            if "429" in str(e) or "RateLimit" in type(e).__name__:
                import time
                time.sleep(2 ** attempt * 2)
                continue
            break
    logger.error("LLM 调用失败: %s，降级选取 Top1", last_err)
    top = candidates[0]
    return {
        "selected_code": top["code"],
        "selected_name": top["name"],
        "reason": f"LLM 调用失败({last_err})，按模型打分选取第1名",
        "vetoed": [],
    }


def _parse_llm_result(content: str, valid_codes: dict) -> dict | None:
    """解析 LLM 返回的 JSON，清洗键名并从文本兜底抽取 selected_code。"""
    # 优先按 JSON 解析
    parsed = None
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        result = {k.strip(". "): v for k, v in parsed.items()}
        code = result.get("selected_code")
        if code in valid_codes:
            return {
                "selected_code": code,
                "selected_name": result.get("selected_name", valid_codes[code]),
                "reason": result.get("reason", ""),
                "vetoed": result.get("vetoed", []),
            }
    # 兜底：从文本中用正则抽取候选代码
    import re
    for code in valid_codes:
        if re.search(rf"\b{code}\b", content or ""):
            return {
                "selected_code": code,
                "selected_name": valid_codes[code],
                "reason": "LLM 未返回规范JSON，按文本命中代码选取",
                "vetoed": [],
            }
    return None


# ========== 2.6 推荐入库 ==========

def _save_recommendation(date_str: str, selected: dict, candidates: list[dict],
                          vetoed: list, regime: str) -> None:
    conn = _get_db()
    # 每日最多一条：同日期已存在则不再插入
    exists = conn.execute(
        "SELECT 1 FROM recommend_log WHERE recommend_date = ?", (date_str,)
    ).fetchone()
    if exists:
        logger.info("当日 %s 已存在推荐记录，跳过", date_str)
        conn.close()
        return
    # 排名：在候选名单中的位置
    rank = next(
        (i + 1 for i, c in enumerate(candidates) if c["code"] == selected["selected_code"]),
        1,
    )
    score = next(
        (c["score"] for c in candidates if c["code"] == selected["selected_code"]),
        None,
    )
    veto_json = json.dumps(vetoed, ensure_ascii=False)
    reason = selected.get("reason", "")
    if vetoed:
        reason = reason + " | 否决记录: " + veto_json
    conn.execute(
        "INSERT INTO recommend_log "
        "(recommend_date, code, name, rank, score, regime, buy_reason, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'HOLD')",
        (date_str, selected["selected_code"], selected["selected_name"],
         rank, score, regime, reason),
    )
    conn.commit()
    conn.close()
    logger.info("推荐入库: %s %s (排名%d, 分数%.4f)",
                selected["selected_code"], selected["selected_name"], rank, score or 0.0)


def run_recommendation(retrain: bool = False) -> None:
    """推荐引擎主入口：打分 → 否决 → 入库。

    retrain=True 时重新训练模型；否则复用已保存模型（不存在则训练）。
    """
    date_str = datetime.now().strftime("%Y-%m-%d")
    conn = _get_db()
    regime = calc_regime(conn)
    rules = _load_rules(conn)
    conn.close()

    if retrain or not MODEL_PATH.exists():
        logger.info("=== 准备训练数据并训练 LightGBM ===")
        X, y = prepare_lgb_training_data()
        if len(X) == 0:
            logger.error("训练样本为空，无法训练，终止推荐")
            return
        model = train_lgb_model(X, y)
    else:
        logger.info("=== 加载已保存模型 ===")
        model = lgb.Booster(model_file=str(MODEL_PATH))

    logger.info("=== 对所有可投基金打分取 Top 10 ===")
    candidates = rank_funds(model)
    if not candidates:
        logger.error("无候选基金（特征数据缺失），终止推荐")
        return
    logger.info("Top 10 候选: %s",
                ", ".join(f"{c['code']}({c['score']:.3f})" for c in candidates))

    logger.info("=== 获取宏观摘要 + LLM 顺位否决 ===")
    macro = get_macro_summary(date_str)
    result = llm_veto(candidates, regime, macro, rules)

    selected = {
        "selected_code": result["selected_code"],
        "selected_name": result["selected_name"],
        "reason": result.get("reason", ""),
    }
    vetoed = result.get("vetoed", [])
    logger.info("LLM 选定: %s %s | 否决 %d 只",
                selected["selected_code"], selected["selected_name"], len(vetoed))

    _save_recommendation(date_str, selected, candidates, vetoed, regime)
    logger.info("推荐流程完成")


if __name__ == "__main__":
    import sys
    retrain = "--retrain" in sys.argv
    run_recommendation(retrain=retrain)

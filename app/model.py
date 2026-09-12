"""模型生命周期 module：训练数据准备 / 训练 / 加载缓存 / 打分 / 重训判定单一来源。

三个引擎（推荐/监控/回测）此前各自内联模型路径、加载逻辑与重训判定，
此模块收敛为一个 interface，避免模型知识在多处漂移。
"""

import json
from datetime import date, datetime
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

import app.repo as repo
from app import domain
from app.features.calculator import (
    compute_fund_features,
    latest_sector_heat,
    load_sector_pct_frame,
    market_state_features,
    sector_heat_from_frame,
)
from app.repo import meta_keys as META
from app.repo.nav import forward_return_from_navs
from app.utils.log import get_logger

logger = get_logger("model")

MODEL_PATH = Path("models/lgb_model.txt")
FEATURE_COLS = repo.FEATURE_COLS
MARKET_COLS = repo.MARKET_COLS
_FORWARD_WINDOW = repo.FORWARD_WINDOW

# 训练标签版本：目标 = 风险调整收益（40 日绝对收益 − λ×最大回撤），替换纯 abs_ret_40d_v2。
# 模型 meta 记录训练时的标签版本；加载路径校验不一致 → 强制重训，
# 防止新逻辑（入场门槛/排序/监控退出）跑在旧标签模型输出上。
LABEL_VERSION = "risk_adj_40d_v2"

# 重训间隔（天）：每周一次。
# 验证依据（2026-08 实测）：标签是未来 40（FORWARD_DAYS）个交易日收益，今天训练时最新可用样本
# 已在 40 个交易日前；面板采样步长 20 天，相邻两次采样训练集差异仅 ~0.3%。每天重训
# 近乎空转，且头部基金预测分接近时模型微调会翻转 Top 排序（K=1 重训 Top-10 重合
# 仅 ~40%），改为每周重训：既持续纳入新样本，又不给推荐排序引入不必要的日间抖动。
_RETRAIN_INTERVAL_DAYS = 7

# 全量12K基金特征计算太慢，限2000只代表性样本训练。
_MAX_TRAIN_FUNDS = 2000

# 进程内模型缓存：只加载一次（监控逐持仓打分场景复用）。
_model_cache: lgb.Booster | None = None
_model_cache_loaded = False


def risk_adjusted_return(navs: np.ndarray, pos: int, forward: int,
                         lambda_: float | None = None) -> float:
    """风险调整收益标签 = forward 日绝对收益 − λ × 同期最大回撤（单一来源）。

    navs: 复权净值序列；pos: 决策日索引；forward: 前向交易日窗口。
    最大回撤取 [pos, pos+forward] 窗口内净值相对历史高点的最大回撤（正数）。
    窗口越界或含非有限值 → nan（调用方跳过该样本）。
    lambda_: λ 标定用覆盖（backtest_model 扫描）；缺省用全局 RISK_ADJ_DD_LAMBDA。
    """
    if pos < 0 or pos + forward >= len(navs):
        return float("nan")
    window = np.asarray(navs[pos: pos + forward + 1], dtype=float)
    if window.size < forward + 1 or not np.all(np.isfinite(window)) or window[0] <= 0:
        return float("nan")
    lam = domain.RISK_ADJ_DD_LAMBDA if lambda_ is None else lambda_
    ret = window[-1] / window[0] - 1.0
    peak = np.maximum.accumulate(window)
    max_dd = float(np.max((peak - window) / peak))
    return float(ret - lam * max_dd)


# LightGBM 树结构超参默认值（GA 月度寻优前的基线，ticket 10）。
# 可调基因：learning_rate / num_leaves / max_depth；
# min_data_in_leaf / feature_fraction 固定（面板样本量约束，不入搜索空间）。
_DEFAULT_LGB_PARAMS = {
    "learning_rate": 0.03,
    "num_leaves": 16,
    "max_depth": 8,
    "min_data_in_leaf": 20,
    "feature_fraction": 0.9,
}
LGB_TUNABLE_KEYS = ("learning_rate", "num_leaves", "max_depth")
# max_depth 显式取 8（而非 LightGBM 默认 -1 无限制）：num_leaves=16 时几乎不构成
# 额外约束（实践行为等价），但使默认值落在 GA 搜索区间内，保证 encode/decode 无损。


def get_lgb_params() -> dict:
    """LightGBM 训练参数：meta 快照（GA 月度寻优）优先，无则默认（单一来源）。"""
    params = {
        "objective": "regression_l1", "metric": "l1",
        "verbose": -1, "seed": 42,
    }
    params.update(_DEFAULT_LGB_PARAMS)
    raw = repo.get_meta(META.LGB_PARAMS_SNAPSHOT)
    if raw:
        try:
            tuned = json.loads(raw)
        except (ValueError, TypeError):
            logger.warning("lgb_params_snapshot 解析失败，用默认超参")
            return params
        for k in LGB_TUNABLE_KEYS:
            if k in tuned:
                params[k] = tuned[k]
    return params


def evaluate_lgb_params(params: dict, data: tuple | None = None) -> float:
    """用 walk-forward 验证集 L1 loss 评估树结构超参，返回负 loss（GA 最大化）。

    超参改变必须重训模型才能评估（回测路径复用已训模型，故不适用）。
    data: prepare_training_data 的返回值；GA 复用同一份样本，避免每次重算。
    """
    if data is None:
        data = prepare_training_data()
    if not data or len(data[1]) == 0:
        return -1e9
    X_tr, y_tr, w_tr, X_val, y_val, _ = data
    merged = get_lgb_params()
    merged.update(params)
    booster = lgb.train(merged, lgb.Dataset(X_tr, label=y_tr, weight=w_tr),
                        num_boost_round=50)
    pred = np.asarray(booster.predict(X_val), dtype=float)
    return -float(np.mean(np.abs(pred - np.asarray(y_val, dtype=float))))


def model_version() -> str:
    """当前模型版本指纹：训练时间戳 + 模型文件 mtime。

    监控预测序列（monitor_scores）记录打分时的版本；确认期跨版本时
    序列不可比，状态机按版本边界重置连续计数。
    """
    trained = repo.get_model_last_trained()
    mtime = ""
    try:
        if MODEL_PATH.exists():
            mtime = datetime.fromtimestamp(MODEL_PATH.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    except OSError:
        pass
    return f"{trained or 'unknown'}|{mtime}"


def retrain_due(last_trained: str | None, today: date | None = None) -> bool:
    """距上次训练是否已满 _RETRAIN_INTERVAL_DAYS 天。无记录视为到期（首次部署）。"""
    if not last_trained:
        return True
    try:
        last = datetime.strptime(last_trained, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return True
    today = today or date.today()
    return (today - last).days >= _RETRAIN_INTERVAL_DAYS


def panel_samples(fund_codes: list[str], window_end: str | None = None,
                  window_days: int = 365,
                  lambdas: tuple[float, ...] | None = None) -> list:
    """面板采样（单一来源，架构审查候选 6）：生产训练与研究回测共用同一循环。

    逐基金净值 → 60 日预热 → 每 20 步采样 → 特征现算（含 style_r2 反推与市场
    状态注入）→ 样本 (date, feat, y_abs, y_adj)。口径变更只改此处：
    - y_abs：未来 FORWARD 日实际收益（主标尺 forward_return_from_navs，研究评估用）
    - y_adj：λ→风险调整标签；lambdas 省略时仅含默认 λ（键 None，生产训练用）
    window_end: 回测按决策日截断（严格无前视）；window_days: 滚动窗口天数。
    """
    idx_rows = repo.get_index_series("sh000300", ("date", "close", "volume"))
    if not idx_rows:
        raise RuntimeError("沪深300指数数据缺失，无法准备训练数据")
    idx_df = pd.DataFrame(idx_rows, columns=["date", "close", "volume"])
    idx_df["date"] = pd.to_datetime(idx_df["date"])
    idx_df = idx_df.set_index("date").sort_index()
    idx_close = idx_df["close"]
    idx_vol = idx_df["volume"]
    we = None
    if window_end is not None:
        # 回测：指数截断到决策日，特征窗口/前向收益均不越界（严格无前视）
        we = pd.Timestamp(window_end)
        idx_close = idx_close[idx_close.index <= we]
        idx_vol = idx_vol[idx_vol.index <= we]
    idx_ret_fwd = idx_close.shift(-_FORWARD_WINDOW) / idx_close - 1.0
    # 滚动窗口：只取最近 window_days 内样本（时间衰减权重软降远端，窗口作硬边界）
    valid_dates = idx_ret_fwd.dropna().index
    if window_end is not None:
        window_start = we - pd.Timedelta(days=window_days)
    elif len(valid_dates) > window_days - 105:
        window_start = valid_dates[-1] - pd.Timedelta(days=window_days)
    else:
        window_start = valid_dates[0]
    logger.info("训练滚动窗口: %s 起（%d 个可用决策日）",
                window_start.date() if hasattr(window_start, "date") else window_start,
                len(valid_dates))

    # 板块日涨幅宽表（一次加载，供逐决策日算赛道热度/反推；避免样本级 N+1 查询）
    _STEP = 20
    sector_frame = load_sector_pct_frame()
    lam_list = tuple(lambdas) if lambdas else (None,)
    samples: list = []
    for code in fund_codes:
        rows = repo.nav.series(code)
        dates = [pd.Timestamp(r[0]) for r in rows]
        navs = [r[1] for r in rows]
        if len(dates) < 60 + _FORWARD_WINDOW:
            continue
        navs_arr = np.array(navs, dtype=float)
        max_pos = len(dates) - 1 - _FORWARD_WINDOW
        for pos in range(60, max_pos + 1, _STEP):
            d = dates[pos]
            if d not in idx_ret_fwd.index or pd.isna(idx_ret_fwd[d]) or d < window_start:
                continue
            if window_end is not None and d > we:
                continue  # 回测：样本不晚于决策日
            idx_pos = idx_close.index.get_indexer([d])[0]
            if idx_pos < 0 or idx_pos < 60:
                continue
            idx_closes_w = idx_close.iloc[domain.index_window_slice(idx_pos)].to_numpy(dtype=float)
            idx_vols_w = idx_vol.iloc[domain.index_window_slice(idx_pos)].to_numpy(dtype=float)
            feat = compute_fund_features(
                navs_arr[:pos + 1], idx_closes_w, idx_vols_w,
                nav_dates=[dd.strftime("%Y-%m-%d") for dd in dates[:pos + 1]],
                sector_frame=sector_frame)
            if feat is None or any(pd.isna(v) for v in feat.values()):
                continue
            # R1：注入市场状态列（全基金共享），让模型感知 beta 分量以预测绝对收益
            # ticket 08：赛道热度按决策日切片计算（严格无前视）
            feat.update(market_state_features(
                idx_closes_w, idx_vols_w,
                sector_heat=sector_heat_from_frame(
                    sector_frame, d.strftime("%Y-%m-%d"))))
            # 主标尺单一来源（候选 3）：窗口不足 → None 跳过，不混入短窗口收益
            y_abs = forward_return_from_navs(navs_arr, pos, _FORWARD_WINDOW)
            if y_abs is None:
                continue
            y_adj = {lam: risk_adjusted_return(navs_arr, pos, _FORWARD_WINDOW,
                                               lambda_=lam) for lam in lam_list}
            if not all(np.isfinite(list(y_adj.values()))):
                continue
            samples.append((d, feat, y_abs, y_adj))
    return samples


def prepare_training_data(window_end: str | None = None,
                          fund_codes: list[str] | None = None,
                          window_days: int = 365) -> tuple[pd.DataFrame, pd.Series, np.ndarray,
                                     pd.DataFrame, pd.Series, np.ndarray]:
    """面板样本 + 时间衰减权重，返回 (X_train, y_train, w_train, X_val, y_val, w_val)。

    样本按时间排序后取前 80% 训练、最新 20% 验证（walk-forward，验证集严格在训练集之后）；
    权重按样本日期指数衰减（半衰期 90 天），让模型更适应当前市场而非远古 regime。

    window_end: 训练截止决策日（回测按决策日重训传参，严格无前视）；缺省 = 最新数据（线上）。
                窗口起点随之为 window_end 前 window_days 天。
    fund_codes: 基金池覆盖（回测传按决策日动态采样池，防幸存者偏差）；
                缺省 = get_train_fund_codes 随机采样。
    window_days: 滚动窗口天数（生产默认 365 = 最近 12 个月；历史验收脚本可传更长窗口）。
    采样循环收敛于 panel_samples（架构审查候选 6），本节只做时间切分与权重。
    """
    if fund_codes is None:
        fund_codes = repo.get_train_fund_codes(60 + _FORWARD_WINDOW, _MAX_TRAIN_FUNDS)
    if not fund_codes:
        logger.warning("训练集为空")
        empty = pd.DataFrame(columns=FEATURE_COLS + MARKET_COLS)
        return empty, pd.Series(dtype=float, name="abs_ret_40d"), np.array([], dtype=float), \
            empty, pd.Series(dtype=float, name="abs_ret_40d"), np.array([], dtype=float)
    samples = panel_samples(fund_codes, window_end=window_end,
                            window_days=window_days)
    if not samples:
        logger.warning("训练集为空")
        empty = pd.DataFrame(columns=FEATURE_COLS + MARKET_COLS)
        return empty, pd.Series(dtype=float, name="abs_ret_40d"), np.array([], dtype=float), \
            empty, pd.Series(dtype=float, name="abs_ret_40d"), np.array([], dtype=float)

    # walk-forward：按时间排序，最后 20% 样本作验证集；权重按日期指数衰减（半衰期 90 天）
    samples.sort(key=lambda x: x[0])
    split_idx = int(len(samples) * 0.8)
    train_s, val_s = samples[:split_idx], samples[split_idx:]

    t_max = samples[-1][0]

    X_train = pd.DataFrame([s[1] for s in train_s], columns=FEATURE_COLS + MARKET_COLS)
    y_train = pd.Series([s[3][None] for s in train_s], name="abs_ret_40d")
    w_train = np.array([np.exp(-(t_max - s[0]).days / 90.0) for s in train_s], dtype=float)
    X_val = pd.DataFrame([s[1] for s in val_s], columns=FEATURE_COLS + MARKET_COLS)
    y_val = pd.Series([s[3][None] for s in val_s], name="abs_ret_40d")
    w_val = np.array([np.exp(-(t_max - s[0]).days / 90.0) for s in val_s], dtype=float)

    logger.info("训练集构建完成: %d只基金, 训练 %d 条, 验证 %d 条, 特征 %d 维, 时间衰减权重(半衰期90天)",
                len(fund_codes), len(X_train), len(X_val), len(FEATURE_COLS + MARKET_COLS))
    return X_train, y_train, w_train, X_val, y_val, w_val


def train(X_train: pd.DataFrame, y_train: pd.Series,
          w_train: np.ndarray | None = None,
          X_val: pd.DataFrame | None = None,
          y_val: pd.Series | None = None,
          w_val: np.ndarray | None = None,
          save_path: str | Path | None = MODEL_PATH,
          params_override: dict | None = None) -> lgb.Booster:
    """训练 LightGBM：L1 回归 + 低学习率/少叶子 + 固定 50 轮。

    树结构超参来自 get_lgb_params()（meta 快照优先，默认兜底）；params_override
    供 GA 评估临时覆盖（不落库）。

    面板样本训练集与验证集存在分布漂移（时间衰减权重 + 验证期行情差异），
    early stopping 在验证 L1 上从第 2 轮起就单调恶化而失效；
    回测对比固定 1/50/150 轮后，50 轮 IC 与 ic_ir 最优，故固定轮数训练。

    save_path=None 时只训练不落盘（回测每决策日重训临时模型用，避免覆盖生产模型）。
    """
    params = get_lgb_params()
    if params_override:
        params.update(params_override)
    train_data = lgb.Dataset(X_train, label=y_train, weight=w_train)
    booster = lgb.train(params, train_data, num_boost_round=50)
    if save_path is not None:
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        booster.save_model(str(save_path))
        # T01：训练成功即记录标签版本（回测临时模型 save_path=None 不写 meta）
        repo.set_model_label_version(LABEL_VERSION)
        logger.info("LightGBM 模型已保存: %s (固定 50 轮, %d 特征, 目标=风险调整收益, 标签版本=%s)",
                    save_path, len(FEATURE_COLS + MARKET_COLS), LABEL_VERSION)
    return booster


def load() -> lgb.Booster | None:
    """加载模型（进程内缓存一次）；缺失/损坏返回 None（调用方据此跳过或回退）。"""
    global _model_cache, _model_cache_loaded
    if _model_cache_loaded:
        return _model_cache
    _model_cache_loaded = True
    try:
        if not MODEL_PATH.exists():
            logger.warning("模型文件缺失，模型信号防线跳过: %s", MODEL_PATH)
            _model_cache = None
        else:
            booster = lgb.Booster(model_file=str(MODEL_PATH))
            expected = len(FEATURE_COLS + MARKET_COLS)
            if booster.num_feature() != expected:
                logger.warning(
                    "模型特征数不匹配（模型 %d vs 代码 %d），忽略旧模型并触发重训",
                    booster.num_feature(), expected)
                _model_cache = None
                return None
            _model_cache = booster
    except Exception as e:
        logger.warning("模型加载失败，模型信号防线跳过: %s", str(e)[:120])
        _model_cache = None
    return _model_cache


# 监控打分用的市场状态列缓存：同一时刻全市场共享，避免逐持仓重复拉指数
_mkt_state_cache: dict | None = None
_mkt_state_cache_date: str = ""


def latest_market_state() -> dict:
    """最新市场状态列（指数 20 日动量/波动率），按天缓存。调用方显式传入 score()。"""
    global _mkt_state_cache, _mkt_state_cache_date
    today = datetime.now().strftime("%Y-%m-%d")
    if _mkt_state_cache is None or _mkt_state_cache_date != today:
        idx_rows = repo.get_index_series("sh000300", ("date", "close", "volume"))
        if idx_rows:
            closes = np.array([r[1] for r in idx_rows], dtype=float)
            vols = np.array([r[2] for r in idx_rows], dtype=float)
            _mkt_state_cache = market_state_features(closes, vols,
                                                     sector_heat=latest_sector_heat())
        else:
            # 审计 P2-4：指数缺失显式告警（不静默 0）——模型在分布外输入打分
            logger.warning("指数数据缺失（sh000300 无行）：市场状态列填 0，模型打分失真风险")
            _mkt_state_cache = {c: 0.0 for c in MARKET_COLS}
        _mkt_state_cache_date = today
    return _mkt_state_cache


def score(features: dict, market_state: dict | None = None) -> float | None:
    """用模型对特征 dict 打分，返回预测风险调整收益；无模型/特征不全/异常返回 None。

    market_state 由调用方显式传入（与特征日期对齐的市场状态列），score 保持纯函数、
    无隐式全局依赖；缺省时回退 latest_market_state() 以保持向后兼容。
    """
    model = load()
    if model is None:
        return None
    try:
        row = {c: float(features[c]) for c in FEATURE_COLS if features.get(c) is not None}
        if len(row) != len(FEATURE_COLS):
            return None
        # 市场状态列：显式注入（调用方持有日期上下文）；缺省回退最新市场状态
        row.update(market_state if market_state is not None else latest_market_state())
        val = float(model.predict(
            pd.DataFrame([row], columns=FEATURE_COLS + MARKET_COLS))[0])
        return val if np.isfinite(val) else None
    except Exception as e:
        logger.debug("模型打分失败: %s", str(e)[:120])
        return None


def label_version_mismatch() -> bool:
    """模型标签版本与当前代码不一致（缺失/旧标签）→ 需重训。

    老模型无 meta 元信息（None）视为不一致：宁可多训一次，不可静默用错标签。
    """
    return repo.get_model_label_version() != LABEL_VERSION


def feature_dim_mismatch() -> bool:
    """已存模型的特征数 ≠ 当前代码的特征列数 → 需重训。

    特征列变更（如新增 sharpe/sortino/ttr）后，旧 Booster 的输入维度与新
    `FEATURE_COLS + MARKET_COLS` 不一致：直接交付给 predict 会报错或静默
    错位取列。此处主动识别并触发重训，避免旧模型污染推荐排序。
    """
    try:
        if not MODEL_PATH.exists():
            return False
        return (lgb.Booster(model_file=str(MODEL_PATH)).num_feature()
                != len(FEATURE_COLS + MARKET_COLS))
    except Exception as e:
        # 读取失败（文件损坏/假模型）时保守返回 False：交由 load() 的异常路径处理，
        # 避免与"维度确实不符"混淆而误触发重训。
        logger.warning("模型特征数校验失败（按未变更处理）: %s", str(e)[:80])
        return False


def get_or_train(retrain: bool = False) -> lgb.Booster | None:
    """准备模型：到期/标签错配/特征维度变更重训或加载现有；无可用时返回 None。"""
    if retrain or not MODEL_PATH.exists() or retrain_due(repo.get_model_last_trained()) \
            or label_version_mismatch() or feature_dim_mismatch():
        logger.info("=== 准备训练数据并训练 LightGBM ===")
        try:
            X_train, y_train, w_train, X_val, y_val, w_val = prepare_training_data()
            if len(X_train) == 0:
                if MODEL_PATH.exists():
                    logger.warning("训练样本为空，回退使用现有模型")
                    return load()
                logger.warning("训练样本为空且无现有模型，跳过本次推荐")
                return None
            model = train(X_train, y_train, w_train, X_val, y_val, w_val)
            repo.set_model_last_trained(datetime.now().strftime("%Y-%m-%d"))
            _model_cache, _model_cache_loaded = model, True  # 训练完成即替换缓存
            return model
        except Exception as e:
            logger.error("模型重训失败: %s", e, exc_info=True)
            if not MODEL_PATH.exists():
                logger.warning("无可用模型，跳过本次推荐")
                return None
            logger.warning("回退使用现有模型")
            return load()
    logger.info("=== 加载已保存模型 ===")
    return load()

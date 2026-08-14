"""模型生命周期 module：训练数据准备 / 训练 / 加载缓存 / 打分 / 重训判定单一来源。

三个引擎（推荐/监控/回测）此前各自内联模型路径、加载逻辑与重训判定，
此模块收敛为一个 interface，避免模型知识在多处漂移。
"""

from datetime import date, datetime
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

from app.features.calculator import compute_fund_features, market_state_features
from app.utils.log import get_logger
from app import domain
import app.repo as repo

logger = get_logger("model")

MODEL_PATH = Path("models/lgb_model.txt")
FEATURE_COLS = repo.FEATURE_COLS
MARKET_COLS = repo.MARKET_COLS
_FORWARD_WINDOW = repo.FORWARD_WINDOW

# 重训间隔（天）：每周一次。
# 验证依据（2026-08 实测）：标签是未来 20 个交易日收益，今天训练时最新可用样本
# 已在 20 个交易日前；面板采样步长 20 天，相邻两天训练集差异仅 ~0.3%。每天重训
# 近乎空转，且头部基金预测分接近时模型微调会翻转 Top 排序（K=1 重训 Top-10 重合
# 仅 ~40%），改为每周重训：既持续纳入新样本，又不给推荐排序引入不必要的日间抖动。
_RETRAIN_INTERVAL_DAYS = 7

# 全量12K基金特征计算太慢，限2000只代表性样本训练。
_MAX_TRAIN_FUNDS = 2000

# 进程内模型缓存：只加载一次（监控逐持仓打分场景复用）。
_model_cache: lgb.Booster | None = None
_model_cache_loaded = False


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
    """
    idx_rows = repo.get_index_series("sh000300", ("date", "close", "volume"))
    if not idx_rows:
        raise RuntimeError("沪深300指数数据缺失，无法准备训练数据")
    idx_df = pd.DataFrame(idx_rows, columns=["date", "close", "volume"])
    idx_df["date"] = pd.to_datetime(idx_df["date"])
    idx_df = idx_df.set_index("date").sort_index()
    idx_close = idx_df["close"]
    idx_vol = idx_df["volume"]
    if window_end is not None:
        # 回测：指数截断到决策日，特征窗口/前向收益均不越界（严格无前视）
        we = pd.Timestamp(window_end)
        idx_close = idx_close[idx_close.index <= we]
        idx_vol = idx_vol[idx_vol.index <= we]
    idx_ret_fwd = idx_close.shift(-_FORWARD_WINDOW) / idx_close - 1.0

    # 12 个月滚动窗口：只取最近 ~250 个交易日样本，避免远古 regime 参与训练
    # （时间衰减权重已软性降低远端权重，窗口作硬性边界；数据不足 12 个月时退化为全量）
    valid_dates = idx_ret_fwd.dropna().index
    if window_end is not None:
        window_start = we - pd.Timedelta(days=window_days)
    elif len(valid_dates) > window_days - 105:
        window_start = valid_dates[-1] - pd.Timedelta(days=window_days)
    else:
        window_start = valid_dates[0]
    logger.info("训练滚动窗口: %s 起（12个月窗口，%d 个可用决策日）",
                window_start.date(), len(valid_dates))

    if fund_codes is None:
        fund_codes = repo.get_train_fund_codes(60 + _FORWARD_WINDOW, _MAX_TRAIN_FUNDS)
    if not fund_codes:
        logger.warning("训练集为空")
        empty = pd.DataFrame(columns=FEATURE_COLS + MARKET_COLS)
        return empty, pd.Series(dtype=float, name="abs_ret_20d"), np.array([], dtype=float), \
            empty, pd.Series(dtype=float, name="abs_ret_20d"), np.array([], dtype=float)

    # 面板采样：每只基金沿时间轴每 _STEP 天取一个样本
    _STEP = 20
    samples = []
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
            y = navs_arr[pos + _FORWARD_WINDOW] / navs_arr[pos] - 1.0
            if not np.isfinite(y):
                continue
            idx_pos = idx_close.index.get_indexer([d])[0]
            if idx_pos < 0 or idx_pos < 60:
                continue
            idx_closes_w = idx_close.iloc[domain.index_window_slice(idx_pos)].to_numpy(dtype=float)
            idx_vols_w = idx_vol.iloc[domain.index_window_slice(idx_pos)].to_numpy(dtype=float)
            feat = compute_fund_features(navs_arr[:pos + 1], idx_closes_w, idx_vols_w)
            if feat is None or any(pd.isna(v) for v in feat.values()):
                continue
            # R1：注入市场状态列（全基金共享），让模型感知 beta 分量以预测绝对收益
            feat.update(market_state_features(idx_closes_w, idx_vols_w))
            samples.append((d, feat, y))

    if not samples:
        logger.warning("训练集为空")
        empty = pd.DataFrame(columns=FEATURE_COLS + MARKET_COLS)
        return empty, pd.Series(dtype=float, name="abs_ret_20d"), np.array([], dtype=float), \
            empty, pd.Series(dtype=float, name="abs_ret_20d"), np.array([], dtype=float)

    # walk-forward：按时间排序，最后 20% 样本作验证集；权重按日期指数衰减（半衰期 90 天）
    samples.sort(key=lambda x: x[0])
    split_idx = int(len(samples) * 0.8)
    train_s, val_s = samples[:split_idx], samples[split_idx:]

    t_max = samples[-1][0]

    X_train = pd.DataFrame([s[1] for s in train_s], columns=FEATURE_COLS + MARKET_COLS)
    y_train = pd.Series([s[2] for s in train_s], name="abs_ret_20d")
    w_train = np.array([np.exp(-(t_max - s[0]).days / 90.0) for s in train_s], dtype=float)
    X_val = pd.DataFrame([s[1] for s in val_s], columns=FEATURE_COLS + MARKET_COLS)
    y_val = pd.Series([s[2] for s in val_s], name="abs_ret_20d")
    w_val = np.array([np.exp(-(t_max - s[0]).days / 90.0) for s in val_s], dtype=float)

    logger.info("训练集构建完成: %d只基金, 训练 %d 条, 验证 %d 条, 特征 %d 维, 时间衰减权重(半衰期90天)",
                len(fund_codes), len(X_train), len(X_val), len(FEATURE_COLS + MARKET_COLS))
    return X_train, y_train, w_train, X_val, y_val, w_val


def train(X_train: pd.DataFrame, y_train: pd.Series,
          w_train: np.ndarray | None = None,
          X_val: pd.DataFrame | None = None,
          y_val: pd.Series | None = None,
          w_val: np.ndarray | None = None,
          save_path: str | None = MODEL_PATH) -> lgb.Booster:
    """训练 LightGBM：L1 回归 + 低学习率/少叶子 + 固定 50 轮。

    面板样本训练集与验证集存在分布漂移（时间衰减权重 + 验证期行情差异），
    early stopping 在验证 L1 上从第 2 轮起就单调恶化而失效；
    回测对比固定 1/50/150 轮后，50 轮 IC 与 ic_ir 最优，故固定轮数训练。

    save_path=None 时只训练不落盘（回测每决策日重训临时模型用，避免覆盖生产模型）。
    """
    params = {
        "objective": "regression_l1", "metric": "l1",
        "learning_rate": 0.03, "num_leaves": 16,
        "min_data_in_leaf": 20, "feature_fraction": 0.9,
        "verbose": -1, "seed": 42,
    }
    train_data = lgb.Dataset(X_train, label=y_train, weight=w_train)
    booster = lgb.train(params, train_data, num_boost_round=50)
    if save_path is not None:
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        booster.save_model(str(save_path))
        logger.info("LightGBM 模型已保存: %s (固定 50 轮, %d 特征, 目标=20日绝对收益)",
                    save_path, len(FEATURE_COLS + MARKET_COLS))
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
            _model_cache = lgb.Booster(model_file=str(MODEL_PATH))
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
            _mkt_state_cache = market_state_features(closes, vols)
        else:
            _mkt_state_cache = {c: 0.0 for c in MARKET_COLS}
        _mkt_state_cache_date = today
    return _mkt_state_cache


def score(features: dict, market_state: dict | None = None) -> float | None:
    """用模型对特征 dict 打分，返回预测 20 日绝对收益；无模型/特征不全/异常返回 None。

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


def get_or_train(retrain: bool = False) -> lgb.Booster | None:
    """准备模型：到期重训或加载现有；无可用时返回 None（跳过本次推荐）。"""
    if retrain or not MODEL_PATH.exists() or retrain_due(repo.get_model_last_trained()):
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

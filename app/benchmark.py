"""主标尺：超额收益口径的单一来源（2.0 唯一验收标尺）。

**口径**：推荐后 `FORWARD_DAYS` 个交易日的收益 − 同期同类平均收益。用超额而非
绝对收益守门，是因为绝对收益混入市场 beta——单边下跌段里"整体 40 日胜率 36%"
既可能是模型无能，也可能只是市场拖累，两者无法区分（见
`docs/backtest/model_walk_forward_40d.md`：验证期单边下跌，两模型 IC 均 ≈ 0）。

**同类**：RBSA 第一行业（持仓聚合口径，Q3/Q19）。两条口径细节都是**标尺版本契约的组成部分**，改动即需改版本号：

1. **同类成员不含自身**——否则小同类组里基金会部分地拿自己当基准，把超额机械地拉向 0。
   自排除是**调用方职责**（本模块只算给定样本），所以装配同类时务必排掉自己。
2. **同类宇宙 = 同一 RBSA 第一行业的全部基金，不按基金类型过滤**——理由：行业均值
   是“行业 beta”的近似，而超额收益要减的正是行业 beta；且它不随候选池定义
   （主动权益 / 指数通道）变化而变化。

同类组有效样本不足 `MIN_PEER_SAMPLES` 时**剔除该样本**，不回退到基金类型——
回退只损失约 1.7% 的基金，却会让模型学会追行业热度（Q4 否决了小样本回退）。
RBSA 为空的基金直接出局。

**标尺版本号**：口径一旦冻结即带版本号。改标尺等于换裁判，因此历史记录不得与
当前标尺混算——`is_current_benchmark` 是唯一准入判定。

**边界**：本模块只做纯计算。数据装配（取同类成员的 FORWARD_DAYS 日收益）在引擎层，
`repo.nav.forward_return` 是区间收益的单一来源。
"""

import math
from collections.abc import Iterable, Mapping
from typing import Any

from app import domain

# 主标尺口径版本。改动 FORWARD_DAYS / 同类定义 / 剔除门槛即必须改版本号，
# 否则新旧记录会被混在一起聚合（这正是版本号存在的意义）。
BENCHMARK_VERSION = "excess_rbsa120d_v2"

# 同类组最小有效样本数：不足则剔除该样本（不回退到基金类型）
MIN_PEER_SAMPLES = 10

# 与领域常量同源（不另立一份字面量）
FORWARD_DAYS = domain.FORWARD_DAYS
PROFIT_THRESHOLD = domain.PROFIT_THRESHOLD
is_profit = domain.is_profit


def peer_group(features: Mapping[str, Any] | None) -> str | None:
    """基金所属同类 = RBSA 第一行业。缺失/空白 → None（出局，不回退）。"""
    if not features:
        return None
    raw = features.get("rbsa_industry_1")
    if not isinstance(raw, str):
        return None
    return raw.strip() or None


def is_valid_return(value: Any) -> bool:
    """可用收益值：非 None、可转 float、非 NaN。

    公开给结算账本用：样本量判定与均值计算必须**共用同一过滤**，否则会出现
    “12 个样本里 9 个有值”被算成 12 个样本的错。
    """
    if value is None:
        return False
    try:
        return not math.isnan(float(value))
    except (TypeError, ValueError):
        return False


def valid_returns(values: Iterable[Any]) -> list[float]:
    """过滤出可用收益值（剔 None / NaN / 非数）。"""
    return [float(v) for v in values if is_valid_return(v)]


def peer_mean(peer_returns: Iterable[Any]) -> float | None:
    """同类平均收益；有效样本 < MIN_PEER_SAMPLES → None。

    无效值（无净值/NaN）**先从样本里剔除再判样本量**——12 个里只有 9 个有净值
    时样本量是 9，不是 12。

    注意：`peer_returns` 应**不含被测基金自身**（标尺版本契约，见模块头）。
    """
    valid = valid_returns(peer_returns)
    if len(valid) < MIN_PEER_SAMPLES:
        return None
    return sum(valid) / len(valid)


def excess_return(own_return: Any, peer_returns: Iterable[Any]) -> float | None:
    """超额收益 = 本基金 FORWARD_DAYS 日收益 − 同期同类平均（同类不含自身）。

    任一侧不可用（自身无收益、同类样本不足）→ None。
    """
    if not is_valid_return(own_return):
        return None
    mean = peer_mean(peer_returns)
    if mean is None:
        return None
    return float(own_return) - mean


def is_current_benchmark(version: str | None) -> bool:
    """标尺版本守卫：只有当前版本的记录才允许与当前标尺聚合/比对。"""
    return version == BENCHMARK_VERSION


def window_return(navs_by_date: Mapping[str, float], start: str, end: str) -> float | None:
    """同窗口收益：**要求两端日期都有净值**，否则 None。

    为什么不能用“按行数取第 FORWARD_DAYS+1 条”（`repo.nav.forward_return_from_navs` 的索引口径）：
    "FORWARD_DAYS 个交易日”指的是**市场**的 FORWARD_DAYS 个交易日，而不是该基金的 FORWARD_DAYS 条净值。净值有洞时
    索引口径会把 50 个自然日当成 FORWARD_DAYS 个交易日，让一只停更中的基金看起来“窗口已满”。
    实测（2026-09-14）：全市场一半基金的最新净值日比另一半早 6 个交易日，故索引
    口径会静默地拿不同长度的窗口做截面比较——**这类偏差不会报错，只会让结论失真**。

    本函数是超额收益的组成部分：只有全部同类成员都走完**同一个日历窗口**，均值
    才有意义。start == end 或 start > end 视为调用方错误 → None。
    """
    if start >= end:
        return None
    a, b = navs_by_date.get(start), navs_by_date.get(end)
    if not is_valid_return(a) or not is_valid_return(b):
        return None
    a_f, b_f = float(a), float(b)  # type: ignore[arg-type]
    if a_f <= 0 or b_f <= 0:
        return None
    return b_f / a_f - 1.0

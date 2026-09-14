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

**边界**：本模块只做纯计算。数据装配（取同类成员的 40 日收益）在引擎层，
`repo.nav.forward_return` 是区间收益的单一来源。
"""

import math
from collections.abc import Iterable, Mapping
from typing import Any

from app import domain

# 主标尺口径版本。改动 FORWARD_DAYS / 同类定义 / 剔除门槛即必须改版本号，
# 否则新旧记录会被混在一起聚合（这正是版本号存在的意义）。
BENCHMARK_VERSION = "excess_rbsa40d_v1"

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


def _valid(value: Any) -> bool:
    """可用收益值：非 None、可转 float、非 NaN。"""
    if value is None:
        return False
    try:
        return not math.isnan(float(value))
    except (TypeError, ValueError):
        return False


def peer_mean(peer_returns: Iterable[Any]) -> float | None:
    """同类平均收益；有效样本 < MIN_PEER_SAMPLES → None。

    无效值（无净值/NaN）**先从样本里剔除再判样本量**——12 个里只有 9 个有净值
    时样本量是 9，不是 12。

    注意：`peer_returns` 应**不含被测基金自身**（标尺版本契约，见模块头）。
    """
    valid = [float(r) for r in peer_returns if _valid(r)]
    if len(valid) < MIN_PEER_SAMPLES:
        return None
    return sum(valid) / len(valid)


def excess_return(own_return: Any, peer_returns: Iterable[Any]) -> float | None:
    """超额收益 = 本基金 40 日收益 − 同期同类平均（同类不含自身）。

    任一侧不可用（自身无收益、同类样本不足）→ None。
    """
    if not _valid(own_return):
        return None
    mean = peer_mean(peer_returns)
    if mean is None:
        return None
    return float(own_return) - mean


def is_current_benchmark(version: str | None) -> bool:
    """标尺版本守卫：只有当前版本的记录才允许与当前标尺聚合/比对。"""
    return version == BENCHMARK_VERSION

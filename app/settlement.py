"""结算账本：推荐结果的记账与三统计量分职。

三个统计量**分列存储、各自聚合、互不混用**（Q11）：

| 角色 | 统计量 | 用途 |
|---|---|---|
| 主标尺（守门 / 验收） | **超额收益期望** | 判断系统有没有本事、是否该降级 |
| 过程标尺（诊断） | **校准准确率** | 判断系统知不知道自己准不准 |
| 用户口径（展示） | 绝对赚钱胜率 | 只展示，**绝不作 fitness** |

**为什么必须物理分开**：1.x 的失效模式是拿自己的验收标尺优化自己——排序权重的
GA fitness 就是 `profit_rate*2 + 期望收益%`，即主标尺本身；于是"系统变好"与
"系统被优化"再也无法区分。把三者分成不同的字段与不同的聚合函数，是切断这个
自证循环的结构性解法。任何把某一个统计量拿去当优化目标的改动，都必须先在这里
被显式讨论。

**标尺版本守卫**：每条记录携带 `benchmark_version`，版本不一致的记录**不得**与当前
标尺聚合。三个聚合函数一律先过滤再计算，因此"忘了加守卫"在结构上不可能发生。

**本模块只做纯计算**（不含任何库访问）：数据装配（取同类成员的窗口收益）在引擎层，
区间收益用 `repo.nav.forward_return`，回撤用 `domain.window_max_drawdown`。
写库见 `app/repo/ledger.py`。
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app import benchmark, domain


@dataclass(frozen=True)
class Settlement:
    """一条结算记录：一次推荐在一个 40 日窗口上的结果。

    `excess_return` 为 None 表示同类样本不足（`peer_n` 记录了实际有效样本数）——
    此时绝对收益仍然保留，因为主标尺缺样本不等于这笔推荐没有结果。
    `confidence` 是推荐时系统自陈的置信度，是校准准确率的原料。
    """

    reco_date: str
    code: str
    benchmark_version: str
    settle_date: str
    abs_return: float
    excess_return: float | None
    peer_n: int
    max_drawdown: float | None
    confidence: float | None = None


def build(
    reco_date: str,
    code: str,
    settle_date: str,
    own_return: Any,
    max_drawdown: Any,
    peer_returns: Iterable[Any],
    confidence: float | None = None,
    benchmark_version: str | None = None,
) -> Settlement | None:
    """结算入口（单一来源，生产与回测共用）。

    只接受**已经算好**的收益与回撤，不读库。自身窗口未满（`own_return` 不可用）
    返回 None——宁可不记账，不可记错账。
    """
    if not benchmark.is_valid_return(own_return):
        return None
    peers = benchmark.valid_returns(peer_returns)
    return Settlement(
        reco_date=reco_date,
        code=code,
        benchmark_version=benchmark_version or benchmark.BENCHMARK_VERSION,
        settle_date=settle_date,
        abs_return=float(own_return),
        excess_return=benchmark.excess_return(own_return, peers),
        peer_n=len(peers),
        max_drawdown=float(max_drawdown) if max_drawdown is not None else None,
        confidence=float(confidence) if confidence is not None else None,
    )


def _current(rows: Iterable[Settlement]) -> list[Settlement]:
    """标尺版本守卫：只保留当前标尺口径的记录（唯一准入判定）。"""
    return [r for r in rows if benchmark.is_current_benchmark(r.benchmark_version)]


def benchmark_expectation(rows: Iterable[Settlement]) -> float | None:
    """主标尺：超额收益期望。无可用样本 → None（不是 0——0 会被读成"中性表现"）。"""
    vals = [r.excess_return for r in _current(rows) if r.excess_return is not None]
    return sum(vals) / len(vals) if vals else None


def absolute_win_rate(rows: Iterable[Settlement]) -> float | None:
    """用户口径：绝对赚钱胜率（>1%，`domain.is_profit` 单一来源）。只展示。"""
    vals = [r.abs_return for r in _current(rows) if r.abs_return is not None]
    if not vals:
        return None
    return sum(1 for v in vals if domain.is_profit(v)) / len(vals)


def calibration_gap(rows: Iterable[Settlement]) -> float | None:
    """过程标尺：|平均自陈置信度 − 实际赚钱命中率|，单位与置信度同（0.3 = 偏 30pp）。

    解读："系统说 80% 会赚钱，实际只有 50%"，偏差 0.30。数值越大越不可信。
    这是**诊断指标**，不参与守门。更细的分桶 ECE 与信号级命中率见校准层工单。
    """
    pairs = [
        (r.confidence, r.abs_return)
        for r in _current(rows)
        if r.confidence is not None and r.abs_return is not None
    ]
    if not pairs:
        return None
    stated = sum(c for c, _ in pairs) / len(pairs)
    hit = sum(1 for _, a in pairs if domain.is_profit(a)) / len(pairs)
    return abs(stated - hit)


def from_mapping(row: Mapping[str, Any]) -> Settlement:
    """库行（dict）→ Settlement。写库侧见 `app/repo/ledger.py`。"""
    return Settlement(
        reco_date=row["reco_date"],
        code=row["code"],
        benchmark_version=row["benchmark_version"],
        settle_date=row["settle_date"],
        abs_return=row["abs_return"],
        excess_return=row["excess_return"],
        peer_n=row["peer_n"] or 0,
        max_drawdown=row["max_drawdown"],
        confidence=row["confidence"],
    )


def as_row(row: Settlement) -> tuple:
    """Settlement → 库行（`app/repo/ledger.py` 的写入顺序）。"""
    return (
        row.reco_date,
        row.code,
        row.benchmark_version,
        row.settle_date,
        row.abs_return,
        row.excess_return,
        row.peer_n,
        row.max_drawdown,
        row.confidence,
    )


def load(rows: Sequence[Mapping[str, Any]]) -> list[Settlement]:
    """库行列表 → Settlement 列表（聚合前的统一转换）。"""
    return [from_mapping(r) for r in rows]

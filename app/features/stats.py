"""统计检验原语（深模块，架构审查候选 2）。

ADR-0007 把 scipy/sklearn 的豁免限定在 features/ 域；此前引擎研究工具
（walk_forward / backtest_model）直接 import scipy（违反豁免边界），而
quality.py 为守边界手写 25 行 rankdata——同一"秩相关/显著性检验"概念两套
实现共存。本模块在豁免域内暴露语义化原语，引擎层只消费命名函数、不再
直接接触 scipy，让豁免边界可执行（test_scipy_exemption 守住依赖边界）。

本模块依赖 scipy（features 域豁免内），对外接口均为 numpy 输入/标量返回。
"""

import numpy as np
import scipy.stats as sps


def rankdata(x) -> np.ndarray:
    """平均秩（tie 取平均），与 scipy.stats.rankdata(average) 一致。"""
    return sps.rankdata(np.asarray(x, dtype=float))


def spearman(x, y) -> tuple[float, float] | None:
    """Spearman 秩相关 (rho, p)；常数序列（零秩差）→ None（无信息）。"""
    rx = rankdata(x)
    ry = rankdata(y)
    if float(np.std(rx)) == 0.0 or float(np.std(ry)) == 0.0:
        return None
    rho, p = sps.spearmanr(rx, ry)
    return float(rho), float(p)


def pearson(x, y) -> tuple[float, float]:
    """Pearson 线性相关 (r, p)。"""
    rho, p = sps.pearsonr(np.asarray(x, dtype=float), np.asarray(y, dtype=float))
    return float(rho), float(p)


def t_tail_p(t, dof: int) -> np.ndarray:
    """t 分布双侧尾概率（回归系数显著性；输入标量或数组）。"""
    t = np.asarray(t, dtype=float)
    return np.asarray(2.0 * (1.0 - sps.t.cdf(np.abs(t), dof)))
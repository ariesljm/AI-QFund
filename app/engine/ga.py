"""遗传算法：自动寻优推荐排序配置（自我进化的一部分）。

基因 = 5 个排序参数（model/rel_strength/calmar/hurst 权重 + momentum_guard），
适应度 = 快速回测的 IC（排序能力主指标）与 Top-Bottom spread（区分度辅助）。

流程：当前配置为精英种子 + 随机初始化 → 锦标赛选择 → 算术交叉 → 高斯变异
      → 精英保留，迭代若干代，返回最优配置（不落库，由调用方决定是否应用）。

运行成本：pop 6 × gen 3 ≈ 24 次 fast 回测（约 3~4 分钟），供月度进化使用
（evolve 每月 1 号调用，非每周）。
"""

import numpy as np

from app.utils.log import get_logger
import app.repo as repo
from backtest.backtest import run_backtest

logger = get_logger("ga")

# 参数搜索边界（按经验缩窄到合理区间，避免搜到退化配置）
# 注意：momentum_guard_pct 是风控防线，不作为 GA 优化基因——
# 放开 guard 会因候选池扩大而虚高 IC fitness，GA 会把它往负方向推坏（线上曾调到 -28.7%）。
_BOUNDS = {
    "model_weight": (0.3, 0.8),
    "rel_strength_weight": (0.0, 0.3),
    "calmar_weight": (0.0, 0.3),
    "hurst_weight": (0.0, 0.3),
}
_GENE_KEYS = list(_BOUNDS.keys())
_MUT_SIGMA = 0.15
_ELITE = 2
_TOURNAMENT_K = 3


def _encode(cfg: dict) -> np.ndarray:
    """配置 → [0,1]^5 基因向量（按边界线性映射）。"""
    v = np.zeros(len(_GENE_KEYS))
    for i, k in enumerate(_GENE_KEYS):
        lo, hi = _BOUNDS[k]
        cur = cfg.get(k, (lo + hi) / 2)
        v[i] = np.clip((cur - lo) / (hi - lo), 0.0, 1.0)
    return v


def _decode(v: np.ndarray) -> dict:
    """基因向量 → 排序配置。"""
    cfg = {}
    for i, k in enumerate(_GENE_KEYS):
        lo, hi = _BOUNDS[k]
        cfg[k] = round(lo + float(v[i]) * (hi - lo), 3)
    return cfg


def fitness(cfg: dict, repeats: int = 1) -> float:
    """快速回测适应度（阶段5 赚钱口径）：赚钱胜率主导 + 期望收益辅助。

    评估区间用近 24 个月（13 个 fast 回测点），比 12 个月（7 点）更稳，
    避免寻优权重过拟合近期单段 regime。fitness = profit_rate*2 + 期望绝对收益%：
    胜率为主（每 1pp ≈ 2 分），期望收益为次（每 1% ≈ 1 分），
    两者均来自回测 Top 组合的 20 日绝对收益（与主目标"推荐后能赚钱"对齐）。

    P2-10：repeats>1 时重复评估取中位数——fast 回测 profit_rate 噪声 ≈±8pp
    （fitness ±16），单次评估的选择偏差大；月度重量活可设 repeats=3 降噪（成本 ×3）。
    """
    s = run_backtest(cfg_override=cfg, fast=True, lookback_days=730)
    if not s:
        return -1e9
    profit = float(s.get("profit_rate_pct", 0.0))
    abs_ret = float(s.get("mean_top_abs_pct", 0.0))
    return profit * 2.0 + abs_ret


def _tournament(pop_fit: list[tuple[np.ndarray, float]], rng: np.random.Generator) -> np.ndarray:
    idx = rng.choice(len(pop_fit), _TOURNAMENT_K, replace=False)
    best = max(idx, key=lambda i: pop_fit[i][1])
    return pop_fit[best][0]


def _crossover(p1: np.ndarray, p2: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    alpha = rng.random()
    return alpha * p1 + (1 - alpha) * p2


def _mutate(v: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    return np.clip(v + rng.normal(0, _MUT_SIGMA, size=v.shape), 0.0, 1.0)


def ga_optimize_ranking(population: int = 4, generations: int = 2,
                        seed: int | None = None) -> tuple[dict, float]:
    """遗传算法寻优排序配置。

    返回 (最优配置 dict, 最优适应度)；调用方决定是否写入 meta。
    初始种群含当前配置（精英保留机制保证结果不劣于当前），
    精英个体不重复评估（每次代只评估新子代）。

    P2-10 稳健化：seed 默认 None → 时间种子（每次寻优探索不同邻域，避免固定 seed
    退化为确定性扰动）；显式传 seed 保持可复现（测试/审计用）。日志记录实际 seed。
    """
    rng = np.random.default_rng(seed)
    logger.info("GA 寻优启动: population=%d, generations=%d, seed=%s",
                population, generations, seed if seed is not None else "time-random")
    cur = repo.get_ranking_cfg()

    # 初始化：当前配置为种子 + 随机个体
    pop = [_encode(cur)]
    pop += [rng.random(len(_GENE_KEYS)) for _ in range(population - 1)]

    pop_fit: list[tuple[np.ndarray, float]] = []
    for v in pop:
        cfg = _decode(v)
        f = fitness(cfg)
        pop_fit.append((v, f))
        logger.info("GA 初始个体 %s → fitness=%.3f", cfg, f)

    for gen in range(generations):
        pop_fit.sort(key=lambda x: x[1], reverse=True)
        elite = pop_fit[:_ELITE]  # 精英保留（不重复评估）
        next_pop = [v for v, _ in elite]

        while len(next_pop) < population:
            p1 = _tournament(pop_fit, rng)
            p2 = _tournament(pop_fit, rng)
            child = _mutate(_crossover(p1, p2, rng), rng)
            next_pop.append(child)

        # 只评估新子代
        new_fit = []
        for v in next_pop[len(elite):]:
            cfg = _decode(v)
            f = fitness(cfg)
            new_fit.append((v, f))
            logger.info("GA 第 %d 代新个体 %s → fitness=%.3f", gen + 1, cfg, f)
        pop_fit = elite + new_fit
        best_fit = max(f for _, f in pop_fit)
        logger.info("GA 第 %d 代完成: 最优 fitness=%.3f", gen + 1, best_fit)

    pop_fit.sort(key=lambda x: x[1], reverse=True)
    best_v, best_f = pop_fit[0]
    best_cfg = _decode(best_v)
    # 风控参数不参与寻优：guard 沿用当前配置值（get_ranking_cfg 已与默认值合并）
    best_cfg["momentum_guard_pct"] = cur["momentum_guard_pct"]
    logger.info("GA 寻优完成: 最优配置 %s, fitness=%.3f (原配置 %s)", best_cfg, best_f, cur)
    return best_cfg, best_f


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cfg, f = ga_optimize_ranking()
    print("最优配置:", cfg)
    print("适应度:", f)

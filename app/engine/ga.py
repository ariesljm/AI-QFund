"""遗传算法：自动寻优推荐排序配置（自我进化的一部分）。

基因 = 5 个排序参数（model/rel_strength/calmar/hurst 权重 + momentum_guard），
适应度 = 快速回测的 IC（排序能力主指标）与 Top-Bottom spread（区分度辅助）。

流程：当前配置为精英种子 + 随机初始化 → 锦标赛选择 → 算术交叉 → 高斯变异
      → 精英保留，迭代若干代，返回最优配置（不落库，由调用方决定是否应用）。

运行成本：pop 6 × gen 3 ≈ 24 次 fast 回测（约 3~4 分钟），供月度进化使用
（evolve 每月 1 号调用，非每周）。
"""

import numpy as np

import app.repo as repo
from app import domain
from app.model import evaluate_lgb_params, get_lgb_params, prepare_training_data
from app.utils.log import get_logger
from backtest.backtest import run_backtest

logger = get_logger("ga")

# 参数搜索边界（按经验缩窄到合理区间，避免搜到退化配置）
# 注意：momentum_guard_pct 是风控防线，不作为 GA 优化基因——
# 放开 guard 会因候选池扩大而虚高 IC fitness，GA 会把它往负方向推坏（线上曾调到 -28.7%）。
_BOUNDS = {
    # 上界 0.30 = 结构性保证“风险调整三指标优先”：
    # 三项下界合计 0.35 > model 上界 0.30，故 GA 无论怎么搜都不能把三项压到 model 之下。
    "model_weight": (0.0, 0.30),
    "rel_strength_weight": (0.0, 0.3),
    "calmar_weight": (0.0, 0.3),
    "hurst_weight": (0.0, 0.3),
    # 风险调整三指标：下界守住“优先考虑”的业务约束（GA 不得把权重压掉），
    # 上界限制过度集中——三项都是一年期风险调整口径，过高会让排序押单一指标。
    "sharpe_weight": (0.15, 0.35),
    "sortino_weight": (0.15, 0.35),
    "ttr_weight": (0.05, 0.20),
}
_GENE_KEYS = list(_BOUNDS.keys())
_MUT_SIGMA = 0.15
_ELITE = 2
_TOURNAMENT_K = 3


def _encode(cfg: dict | domain.RankingConfig) -> np.ndarray:
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
    两者均来自回测 Top 组合的 40 日绝对收益（与主目标"推荐后能赚钱"对齐）。

    P2-10：repeats>1 时重复评估取中位数——fast 回测 profit_rate 噪声 ≈±8pp
    （fitness ±16），单次评估的选择偏差大；月度重任务可设 repeats=3 降噪（成本 ×3）。
    """
    vals: list[float] = []
    for _ in range(repeats):
        s = run_backtest(cfg_override=cfg, fast=True, lookback_days=730)
        if not s:
            return -1e9
        profit = float(s.get("profit_rate_pct", 0.0))
        abs_ret = float(s.get("mean_top_abs_pct", 0.0))
        vals.append(profit * 2.0 + abs_ret)
    return float(np.median(vals)) if len(vals) > 1 else vals[0]


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
                        seed: int | None = None, repeats: int = 1) -> tuple[dict, float]:
    """遗传算法寻优排序配置。

    返回 (最优配置 dict, 最优适应度)；调用方决定是否写入 meta。
    初始种群含当前配置（精英保留机制保证结果不劣于当前），
    精英个体不重复评估（每次代只评估新子代）。

    P2-10 稳健化：seed 默认 None → 时间种子（每次寻优探索不同邻域，避免固定 seed
    退化为确定性扰动）；显式传 seed 保持可复现（测试/审计用）。日志记录实际 seed。
    repeats>1：每次适应度评估取多次回测中位数降噪（月度重任务设 3，成本 ×3）。
    """
    rng = np.random.default_rng(seed)
    logger.info("GA 寻优启动: population=%d, generations=%d, repeats=%d, seed=%s",
                population, generations, repeats, seed if seed is not None else "time-random")
    cur = repo.get_ranking_cfg()

    # 初始化：当前配置为种子 + 随机个体
    pop = [_encode(cur)]
    pop += [rng.random(len(_GENE_KEYS)) for _ in range(population - 1)]

    pop_fit: list[tuple[np.ndarray, float]] = []
    for v in pop:
        cfg = _decode(v)
        f = fitness(cfg, repeats=repeats)
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
            f = fitness(cfg, repeats=repeats)
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


# ── 树结构超参 GA（ticket 10）──
# 与排序权重 GA 分离：排序权重的 fitness 走回测（模型固定，重排序即可）；
# 树结构超参改变必须重训模型才能评估，故 fitness 用 walk-forward 验证集 L1 loss
# （复用 prepare_training_data 的前 80%/后 20% 切分），而非回测。
_LGB_BOUNDS = {
    "learning_rate": (0.01, 0.1),
    "num_leaves": (8, 64),
    "max_depth": (3, 12),
}
_LGB_KEYS = list(_LGB_BOUNDS.keys())
_LGB_INT_KEYS = ("num_leaves", "max_depth")


def _encode_lgb(params: dict) -> np.ndarray:
    v = np.zeros(len(_LGB_KEYS))
    for i, k in enumerate(_LGB_KEYS):
        lo, hi = _LGB_BOUNDS[k]
        cur = params.get(k, (lo + hi) / 2)
        v[i] = np.clip((cur - lo) / (hi - lo), 0.0, 1.0)
    return v


def _decode_lgb(v: np.ndarray) -> dict:
    out: dict = {}
    for i, k in enumerate(_LGB_KEYS):
        lo, hi = _LGB_BOUNDS[k]
        val = lo + float(v[i]) * (hi - lo)
        out[k] = int(round(val)) if k in _LGB_INT_KEYS else round(val, 4)
    return out


def ga_optimize_lgb_params(population: int = 4, generations: int = 2,
                           seed: int | None = None,
                           data: tuple | None = None) -> tuple[dict, float]:
    """遗传算法寻优 LightGBM 树结构超参，返回 (最优参数, 最优 fitness)。

    fitness = 负验证集 L1 loss（越大越好）。种群含当前参数作精英种子，
    保证结果不劣于现状。data 可由调用方传入复用（避免与当前参数评估重复准备）。
    """
    if data is None:
        data = prepare_training_data()
    if not data or len(data[1]) == 0:
        logger.warning("训练样本不足，跳过超参寻优")
        return {}, -1e9

    rng = np.random.default_rng(seed)
    logger.info("超参 GA 启动: population=%d, generations=%d, seed=%s",
                population, generations, seed)

    seed_vec = _encode_lgb(get_lgb_params())
    pop_fit = [(seed_vec, evaluate_lgb_params(_decode_lgb(seed_vec), data))]
    for _ in range(population - 1):
        v = rng.random(len(_LGB_KEYS))
        pop_fit.append((v, evaluate_lgb_params(_decode_lgb(v), data)))

    for gen in range(generations):
        pop_fit.sort(key=lambda t: t[1], reverse=True)
        elites = pop_fit[:_ELITE]
        children = []
        while len(children) < population - _ELITE:
            p1 = _tournament(pop_fit, rng)
            p2 = _tournament(pop_fit, rng)
            child = _mutate(_crossover(p1, p2, rng), rng)
            children.append((child, evaluate_lgb_params(_decode_lgb(child), data)))
        pop_fit = elites + children
        logger.info("超参 GA 第 %d 代完成: 最优 fitness=%.6f", gen + 1, pop_fit[0][1])

    pop_fit.sort(key=lambda t: t[1], reverse=True)
    best_vec, best_fit = pop_fit[0]
    best = _decode_lgb(best_vec)
    logger.info("超参 GA 寻优完成: 最优参数 %s, fitness=%.6f (原参数 %s)",
                best, best_fit, get_lgb_params())
    return best, float(best_fit)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]  # sys.stdout 运行时为 TextIOWrapper
    cfg, f = ga_optimize_ranking()
    print("最优配置:", cfg)
    print("适应度:", f)

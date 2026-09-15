"""票 19：C 层影子闸门（Champion-Challenger 自动切换的判定核心）。

文档的"IR 高 15%"在 ±8pp 测量噪声下不可辨识，闸门替换为：
1. **≥3 个不重叠子区间（牛/熊/震荡）同向不劣**——任何一段劣化即拒绝
2. 合并区间 IR 提升通过**非重叠窗口的配对 t 检验**（单侧显著）

本模块是纯函数判定（注入构造的指标历史即可断言拒/准）；
Champion 继续服务生产、Challenger 影子并行 15 个交易日、自动回滚、
变更台账由接线层负责。

**这是全工程最不能漏的一个"必须能失败"的测试**：噪声场景（看起来更好
但纯噪声的 Challenger）必须被拒。
"""

import math

MIN_REGIMES = 3                # 至少 3 个不重叠子区间（牛/熊/震荡）
DEFAULT_ALPHA = 0.05           # 显著性水平（单侧）


def regime_no_worse(regime_results: list[dict]) -> tuple[bool, str]:
    """多段同向不劣：≥3 段且每段 challenger >= champion（票 19 替换 IR>15%）。

    regime_results: [{"regime": str, "challenger": float, "champion": float}, ...]
    返回 (ok, reason)；任何一段劣化即失败（不只看平均）。
    """
    if len(regime_results) < MIN_REGIMES:
        return False, f"子区间不足 {MIN_REGIMES} 段（须牛/熊/震荡同向覆盖）"
    for r in regime_results:
        if r["challenger"] < r["champion"]:
            return False, f"{r.get('regime', '?')} 段劣化（challenger < champion）"
    return True, ""


def paired_t_pvalue(challenger: list[float], champion: list[float]) -> float:
    """配对差值（challenger − champion）的单侧 t 检验 p 值（非重叠窗口）。

    返回 P(challenger 均值提升为噪声)；窗口 < 2 或零方差 → 1.0（不显著）。
    与 features/stats.t_tail_p 同源（双侧/2 = 单侧）。
    """
    n = len(challenger)
    if n < 2 or n != len(champion):
        return 1.0
    diffs = [c - b for c, b in zip(challenger, champion, strict=True)]
    mean = sum(diffs) / n
    if n == 2:
        sd = abs(diffs[0] - diffs[1]) / math.sqrt(2.0)
    else:
        sd = math.sqrt(sum((d - mean) ** 2 for d in diffs) / (n - 1))
    if sd == 0:
        return 1.0
    t = mean / (sd / math.sqrt(n))
    if t <= 0:
        return 1.0
    from app.features.stats import t_tail_p
    return float(t_tail_p(t, n - 1) / 2.0)   # 双侧/2 = 单侧


def gate_decision(regime_results: list[dict], challenger_windows: list[float],
                  champion_windows: list[float], challenger_version: str,
                  champion_version: str,
                  alpha: float = DEFAULT_ALPHA) -> dict:
    """影子闸门判定。返回 {passed, reasons: [str]}。

    三个门槛全过才放行：
    1. 标尺版本一致（Challenger 与 Champion 必须在同一主标尺版本下比较）
    2. 多段同向不劣（≥3 段，任一劣化拒绝）
    3. 合并区间 IR 提升显著（配对 t 检验单侧 p < alpha）
    """
    reasons: list[str] = []
    if challenger_version != champion_version:
        reasons.append(f"标尺版本不一致（{challenger_version} vs {champion_version}）")
    ok, why = regime_no_worse(regime_results)
    if not ok:
        reasons.append(why)
    p = paired_t_pvalue(challenger_windows, champion_windows)
    if p >= alpha:
        reasons.append(f"合并区间 IR 提升不显著（p={p:.3f} ≥ {alpha}）")
    return {"passed": not reasons, "reasons": reasons, "p": p}

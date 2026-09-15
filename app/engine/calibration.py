"""校准层（票 18）：信号命中率记账与自动降权/停用判定。

每一路信号（状态机各触发条件 / 审计各风险维度）如实记录自己的历史命中率
与样本量；失效信号自动降权/停用——这是**"静默退化"这个失效模式的唯一兜底**
（2.0 文档缺环，见票 18 Comments）。

与调参的本质区别：调参是猜哪个值更好，校准是知道自己准不准——只需诚实记账
+ 保守判定，不需要优化器。

接缝：`assess()` 是纯函数（注入构造的命中历史即可直测）；降权/停用是
**更保守方向**，不违反 ADR-0009 的"规则单向增严"红线。
"""

# 默认配置（票 18"N 进配置"，接线层可覆盖）
MIN_SAMPLES = 10             # 样本量门槛：不足不标注置信度、不触发降权
STREAK_FAIL_TO_DOWNGRADE = 3  # 连续失灵 N 次 → 降权
STREAK_FAIL_TO_DISABLE = 5    # 连续失灵 N 次 → 停用


def hit_rate(hits: int, samples: int, min_samples: int = MIN_SAMPLES) -> float | None:
    """命中率 = hits/samples。样本不足（< min_samples 或 <=0）→ None（不可信）。"""
    if samples < min_samples or samples <= 0:
        return None
    return hits / samples


def confidence_note(hits: int, samples: int, min_samples: int = MIN_SAMPLES) -> str:
    """置信度标注文案：'该信号历史命中 62%（30 样本）'；样本不足 → 空串。"""
    r = hit_rate(hits, samples, min_samples)
    if r is None:
        return ""
    return f"该信号历史命中 {r * 100:.0f}%（{samples} 样本）"


def fail_streak(results: list[bool]) -> int:
    """最近连续失灵次数（结果从新到旧；True = 命中）。"""
    n = 0
    for r in reversed(results):
        if r:
            break
        n += 1
    return n


def action_for_streak(streak: int) -> str:
    """按连续失灵次数给动作：hold / downgrade / disable。"""
    if streak >= STREAK_FAIL_TO_DISABLE:
        return "disable"
    if streak >= STREAK_FAIL_TO_DOWNGRADE:
        return "downgrade"
    return "hold"


def assess(results: list[bool], min_samples: int = MIN_SAMPLES) -> dict:
    """单路信号评估。返回 {hit_rate, streak, action, samples, note}。

    - 样本不足（< min_samples）→ action="hold"（**不降权**：避免用 3 个样本
      停掉一个信号——票 18 明示的陷阱）
    - 样本足够时按连续失灵次数降权（>=3）/停用（>=5）；动作是更保守方向
      （不违反单向增严红线）
    """
    n = len(results)
    hits = sum(1 for r in results if r)
    streak = fail_streak(results)
    rate = hit_rate(hits, n, min_samples)
    action = "hold" if n < min_samples else action_for_streak(streak)
    return {"hit_rate": rate, "streak": streak, "action": action,
            "samples": n, "note": confidence_note(hits, n, min_samples)}

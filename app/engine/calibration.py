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


# ── 票 20：规则退役（依赖校准层数据支撑）────────────────
# 2.0 §5.3 的"只允许新增"只对否决类正确（安全方向单向增严）；信号/特征类
# 必须能退役，否则系统只能变复杂（1.x 的 2 GA/6 防线/12 特征只增不减即证据）。
RETIREMENT_MIN_HIT_RATE = 0.40   # 命中率长期低于该值 → 可提案退役（默认，可配置）
RETIREMENT_MIN_SAMPLES = MIN_SAMPLES  # 复用校准层样本门槛（避免小样本误杀）


def retirement_decision(rule_class: str, hit_rate: float | None,
                        samples: int) -> dict:
    """退役条件判定（票 20）。返回 {eligible, propose, reason}。

    rule_class: "veto"（否决类，单向增严不可逆）/ "signal"（信号/特征类）。
    否决类永不可被退役路径触及；信号类在命中率 < 阈值且样本足够时**提案**
    退役（提案≠执行，执行走留痕流程）。
    """
    if rule_class == "veto":
        return {"eligible": False, "propose": False,
                "reason": "否决类规则单向增严，不可逆（ADR-0009）"}
    if rule_class != "signal":
        raise ValueError(f"未知规则类别: {rule_class!r}")
    if hit_rate is None or samples < RETIREMENT_MIN_SAMPLES:
        return {"eligible": True, "propose": False,
                "reason": "样本不足，不退役（避免小样本误杀）"}
    if hit_rate < RETIREMENT_MIN_HIT_RATE:
        return {"eligible": True, "propose": True,
                "reason": f"命中率 {hit_rate:.0%} 长期低于阈值 "
                          f"{RETIREMENT_MIN_HIT_RATE:.0%}（{samples} 样本）"}
    return {"eligible": True, "propose": False, "reason": "命中率正常"}


def retirement_record(rule_id: str, rule_class: str, hit_rate: float | None,
                      samples: int, reason: str, date: str) -> dict:
    """退役提案留痕（票 20：提案 + 记录，不是静默删除）。

    记录退役对象/依据数据/时间/可恢复性；校准记录保留在 assess 历史中，
    供将来恢复判断（票 20 验收：信号类退役后其校准记录保留）。
    """
    return {"rule_id": rule_id, "rule_class": rule_class,
            "hit_rate": hit_rate, "samples": samples, "reason": reason,
            "date": date, "recoverable": rule_class == "signal"}

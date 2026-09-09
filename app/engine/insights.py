"""进化洞察生命周期 module：查重 → 入库 → 置信度衰减 + 自纠偏记录。

从 evolve.py 拆分（架构深化 ②）：洞察写/去重/衰减是自洽概念，
通过 repo seam 交互、独立可测；evolve 主循环消费本模块公共接口。
"""

import re
from datetime import datetime

import app.repo as repo
from app.utils.log import get_logger

logger = get_logger("insights")

# 元分析新洞察初始置信度（P0-2 试用期：命中胜案例 +0.10、月度 ×0.95 衰减）
_INSIGHT_INITIAL_CONF = 0.5
# 活跃洞察进 prompt 门槛（与 decision.get_active_insights 的 0.3 一致）
_INSIGHT_MIN_CONF = 0.3


def keywords(text: str) -> set:
    """文本关键词集：ASCII 词（len≥2）+ 中文字符 bigram。

    中文无空格分词，整句中文字符串若作为单"词"保留会稀释 Dice 相似度；
    只取中文字符 bigram 作为语义单元，使近似句子的重合度可被度量
    （8-12 曾同日入库 5 条近似洞察而查重不命中）。
    """
    words = set()
    for t in re.sub(r"[^\w\u4e00-\u9fff]", " ", text).split():
        if len(t) >= 2 and not any("\u4e00" <= ch <= "\u9fff" for ch in t):
            words.add(t)
    cjk = re.findall(r"[\u4e00-\u9fff]", text)
    if len(cjk) >= 2:
        words |= {cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1)}
    return words




def insight_conflicts(new_insight: str, existing: list) -> bool:
    """用 Dice 系数判断洞察是否与已有记录重复（中文 bigram 语义单元）。

    Dice = 2|A∩B| / (|A|+|B|)：中文 bigram 下近似句子 Dice≈0.4+、
    不相关句子 <0.15——比 Jaccard 对"部分重叠"更敏感（旧 Jaccard 阈值 0.5
    对整句中文近乎失效，8-12 曾同日入库 5 条近似洞察而查重不命中）。
    """
    new_kw = keywords(new_insight)
    if not new_kw:
        return True
    for ei in existing:
        ei_kw = keywords(ei)
        if not ei_kw:
            continue
        overlap = 2 * len(new_kw & ei_kw) / (len(new_kw) + len(ei_kw))
        if overlap > 0.4:
            return True
    return False




def save_insight(insight: dict, degraded: bool = False) -> bool:
    """入库洞察；质量下行（degraded）时以非活跃状态入库（待审），不自动启用。

    P0-2 试用期：元分析新洞察以 _INSIGHT_INITIAL_CONF（0.5）起步，命中胜案例
    +0.10、月度 ×0.95 衰减；condition（P3-11）透传结构化前置条件。
    """
    existing = repo.get_all_insights()
    if insight_conflicts(insight["insight"], existing):
        return False
    active = 0 if degraded else 1
    condition = insight.get("condition")
    repo.insert_insight(insight["insight"], insight.get("type", "sector"),
                        datetime.now().strftime("%Y-%m-%d"), active,
                        confidence=_INSIGHT_INITIAL_CONF, condition=condition)
    logger.info("新洞察入库: [%s] %s (active=%s, conf=%.2f%s)",
                insight.get("type", "?"), insight["insight"][:60], active,
                _INSIGHT_INITIAL_CONF, f", condition={condition}" if condition else "")
    return True


# ── 置信度衰减 ─────────────────────────────────────────────



def decay_insights() -> int:
    """降低旧洞察置信度，长期无用则标记非活跃。"""
    rows = repo.list_active_insights()
    decayed = 0
    for rid, conf, _cnt in rows:
        # 旧数据 confidence 可能为 NULL（schema DEFAULT 对历史行无效），按初始置信度兜底
        new_conf = float(conf if conf is not None else _INSIGHT_INITIAL_CONF) * 0.95
        # P0-2 阈值统一：与 get_active_insights 的进 prompt 门槛一致（0.3）
        active = 1 if new_conf > _INSIGHT_MIN_CONF else 0
        repo.update_insight_confidence(rid, new_conf, active)
        decayed += 1
    logger.info("置信度衰减: %d 条洞察已更新", decayed)
    return decayed




def fix_key(text: str) -> str:
    """去重键：数字归一化——fitness/配置数值变化不视为新记录。

    历史问题：GA 每次应用的 fitness 值不同，「GA寻优应用: fitness X→Y」文本
    逐次不同导致精确匹配去重永不命中，单日最多累积 43 条重复记录污染。
    """
    return re.sub(r"\d+\.?\d*", "#", text)




def save_self_fix(fix: str) -> None:
    """入库排分自纠偏/GA 应用记录；数字归一化去重。

    重复调用 run_evolve（如排分自纠偏信号误报触发的 force 循环）会把相同
    fitness 的"GA寻优应用"反复入库（8-08 曾单日 43 条重复记录污染）；
    按数字归一化文本去重后同一结论只记一次。
    """
    key = fix_key(fix)
    if any(fix_key(e) == key for e in repo.get_all_insights()):
        return
    # GA 应用/自纠偏是已发生事实，置信度按 1.0 起步（不属元分析试用期范畴）
    # 修复：原 insert 不带 confidence → NULL，decay 时按 0.5 兑底并与注释声称的 1.0 不符
    repo.insert_insight(fix, "ranking", datetime.now().strftime("%Y-%m-%d"), active=1,
                        confidence=1.0)
    logger.info("排分自纠偏: %s", fix[:60])





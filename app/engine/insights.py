"""进化洞察生命周期 module：查重 → 入库 → 置信度衰减 + 自纠偏记录。

从 evolve.py 拆分（架构深化 ②）：洞察写/去重/衰减是自洽概念，
通过 repo seam 交互、独立可测；evolve 主循环消费本模块公共接口。
"""

import json
import re
from datetime import datetime

import app.repo as repo
from app.llm.context import build_sector_vocab, extract_sector_mentions
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
    """入库洞察；质量下行（degraded）时改为 self-fix 报告留痕，不产生死数据。

    P0-2 试用期：元分析新洞察以 _INSIGHT_INITIAL_CONF（0.5）起步，命中胜案例
    +0.10、月度 ×0.95 衰减；condition（P3-11）透传结构化前置条件。
    审计 P1-2（2026-09）：degraded 曾写 active=0 且无激活路径（永久 inactive 死
    数据）——改为 self-fix 留痕（只报告不回流），"因质量下行暂缓的教训"可见可审计。
    """
    existing = repo.get_all_insights()
    if insight_conflicts(insight["insight"], existing):
        return False
    if degraded:
        # 质量下行期 LLM 产出不可信：不固化教训，留审计痕迹（self-fix 只报告不进 prompt）
        save_self_fix(f"[质量下行暂缓] {insight['insight'][:100]}", insight_type="sector")
        return False
    active = 1
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




def save_self_fix(fix: str, insight_type: str = "ranking") -> None:
    """入库排分自纠偏/GA 应用记录；数字归一化去重。

    重复调用 run_evolve（如排分自纠偏信号误报触发的 force 循环）会把相同
    fitness 的"GA寻优应用"反复入库（8-08 曾单日 43 条重复记录污染）；
    按数字归一化文本去重后同一结论只记一次。
    insight_type：默认 ranking（自纠偏/GA）；审计 P1-2 的"质量下行暂缓"
    洞察留痕走 sector，保持审计面板类型可辨。
    """
    key = fix_key(fix)
    if any(fix_key(e) == key for e in repo.get_all_insights()):
        return
    # GA 应用/自纠偏是已发生事实，置信度按 1.0 起步（不属元分析试用期范畴）
    # 修复：原 insert 不带 confidence → NULL，decay 时按 0.5 兑底并与注释声称的 1.0 不符
    repo.insert_insight(fix, insight_type, datetime.now().strftime("%Y-%m-%d"), active=1,
                        confidence=1.0)
    logger.info("排分自纠偏: %s", fix[:60])


def causal_check() -> list[str]:
    """洞察闭环因果验证（#4）：对每条活跃 sector 洞察，比对「生效后 vs 生效前」同类案例胜率。

    描述性反事实（不做统计检验，样本稀疏）：洞察文本命中某赛道 → 找该赛道的
    已结算案例，按洞察 created_date 分「生效前/后」两组算赚钱胜率（胜 = outcome=='胜'）。
    每组 ≥3 样本才出结论：生效后显著提升（>10pp）→ 有效；反而下降 → 疑似无效。
    仅输出 self-fix 报告，不自动改置信度（先观察，避免稀疏样本上伪因果）。
    """
    vocab = build_sector_vocab()
    insights = repo.get_sector_insights_dated(limit=10)
    cases = repo.get_settled_cases_after(0)
    fixes: list[str] = []
    for _iid, text, created in insights:
        sectors = extract_sector_mentions(text, vocab)
        if not sectors:
            continue
        before: list[str] = []
        after: list[str] = []
        for c in cases:
            try:
                rec_sectors = set(json.loads(c.get("recommended_sectors") or "[]"))
            except (json.JSONDecodeError, TypeError):
                continue
            if not (rec_sectors & sectors):
                continue
            if c.get("outcome") not in ("胜", "负"):
                continue
            d = c.get("date") or ""
            if not d or not created:
                continue
            if d < created:
                before.append(c["outcome"])
            else:
                after.append(c["outcome"])
        if len(before) < 3 or len(after) < 3:
            continue
        br = before.count("胜") / len(before)
        ar = after.count("胜") / len(after)
        if ar - br > 0.10:
            fixes.append(f"洞察[{text[:30]}]生效后同类胜率 {br:.0%}→{ar:.0%}（有效）")
        elif ar < br - 0.10:
            fixes.append(f"洞察[{text[:30]}]生效后同类胜率反降 {br:.0%}→{ar:.0%}（疑似无效，建议降置信度）")
    return fixes





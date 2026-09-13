"""B4 LLM prompt 文案 golden 快照 + 字段缺失容错 + JSON schema 一致性（app/llm/prompts.py）。

覆盖：
- final_pick_prompt 全文本 golden 快照：首次运行生成 tests/snapshots/final_pick_prompt.txt，
  后续运行精确比对（同入参两次渲染确定性自检）；
- 其余主要 prompt（sector_selection / monitor_logic / evolution_analysis）
  用关键节锚点断言：各节按序出现、无遗漏；
- final_pick_prompt 候选字段缺失容错（现状契约固化）：
  · .get() 守卫的可选字段（capture_up/report_date/holdings/sector 等）缺失 → 不抛 KeyError；
  · 直接索引字段（calmar/hurst_60d/combo/code/name）缺失 → 当前生产行为抛 KeyError，
    本测试固化该契约（不改 app/ 代码），待生产改为 .get 容错后同步更新；
- prompt 回显 JSON schema 键与解析器/validator（parse_llm_json / _validate_final_pick）
  消费键的一致性断言 + 全链路 roundtrip。
"""

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.engine.recommend import _validate_final_pick
from app.llm.client import parse_llm_json
from app.llm.prompts import (
    evolution_analysis_prompt,
    final_pick_prompt,
    monitor_logic_prompt,
    sector_selection_prompt,
)

SNAPSHOT_DIR = Path(__file__).resolve().parent / "snapshots"
FINAL_PICK_SNAPSHOT = SNAPSHOT_DIR / "final_pick_prompt.txt"


# ---------- 代表性入参 ----------

def _base_candidate() -> dict:
    """final_pick_prompt 代表性候选（含全部生产消费字段，值固定以保证快照确定性）。"""
    return {
        "code": "000001",
        "name": "测试基金A",
        "sector": "半导体",
        "calmar": 1.25,
        "hurst_60d": 0.58,
        "combo": 82.5,
        "capture_up": 1.10,
        "capture_down": 0.65,
        "downside_vol": 0.12,
        "drawdown_60d": -0.08,
        "rbsa_industry_1": "半导体",
        "rbsa_weight_1": 60.0,
        "rbsa_industry_2": "通信设备",
        "rbsa_weight_2": 25.0,
        "holdings": [
            {"stock_name": "中芯国际", "industry": "半导体", "weight": 5.2},
            {"stock_name": "中兴通讯", "industry": "通信设备", "weight": 4.1},
        ],
        "report_date": "2026-06-30",
        "holdings_months": 2,
        "momentum_20d": 3.5,
        "purchase_status": "normal",
        "sector_median_mom": 1.2,
        "mom_gap": 2.3,
    }


def _ctx():
    """final_pick_prompt 消费 ctx 属性的最小桩（生产为 MacroContext，测试用 SimpleNamespace）。"""
    return SimpleNamespace(
        sector_reasoning="半导体国产替代景气度上行",
        recommended_sectors=["半导体"],
        risk_sectors=["白酒"],
        regime_label="bullish",
    )


def _render_final_pick() -> str:
    return final_pick_prompt(
        [_base_candidate()],
        _ctx(),
        ["教训1：不追已连续大涨的赛道", "教训2：警惕高位降权标签"],
    )


def _render_sector_selection() -> str:
    return sector_selection_prompt(
        date_str="2026-08-24",
        pool_text="半导体(5日+3.5%, 20日+8.0%, 60日+15.0%, 基金5只)\n"
                  "通信设备(5日+2.1%, 20日+5.0%, 60日+9.0%, 基金4只)",
        pool_reasoning="量化定池: 2个候选，过热规避剔除白酒",
        top_gainers="半导体 +3.5%",
        top_losers="房地产 -1.2%",
        etf_net_flow="半导体: +8.6亿",
        news_summary="政策支持半导体国产化",
        flow_summary="半导体资金持续流入",
        lessons="避免追高已连续上涨板块",
        market_tech="站上EMA60，中性偏多",
        news_date="2026-08-24",
        news_history="近5日要闻: 半导体设备订单回暖",
    )


def _assert_section_order(text: str, anchors: list[str]) -> None:
    """各节锚点必须全部出现且按序排列（无遗漏、无错序）。"""
    pos = [text.find(a) for a in anchors]
    missing = [a for a, i in zip(anchors, pos) if i < 0]
    assert not missing, f"prompt 缺失节: {missing}"
    assert pos == sorted(pos), f"prompt 节顺序错乱: {list(zip(anchors, pos))}"


def _extract_top_level_json(text: str) -> dict | None:
    """提取文本中首个顶层 JSON 对象（花括号配对扫描）；解析失败返回 None。"""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _extract_schema_keys(text: str, after: str | None = None) -> set[str]:
    """从 prompt 回显的 JSON 模板块提取顶层键名（正则，容忍 `...` 等非合法 JSON 占位）。

    sector_selection_prompt 的 schema 模板含 `["行业1", "行业2", ...]` 占位符，
    整体非合法 JSON，无法 json.loads；顶层键均为行首两空格缩进，用 MULTILINE 行首
    正则提取，嵌套条目（如 vetoed_sectors 内的 sector/reason，4 空格内联）不误匹配。
    """
    start = 0
    if after is not None:
        idx = text.find(after)
        if idx >= 0:
            start = idx
    return set(re.findall(r'^  "([A-Za-z_][A-Za-z_0-9]*)"\s*:', text[start:], re.MULTILINE))


# ---------- final_pick_prompt：golden 快照 + schema 一致性 ----------

class TestFinalPickGolden:
    """final_pick_prompt 全文本 golden 快照与解析器 schema 一致性。"""

    def test_full_text_golden_snapshot(self):
        """全文本快照：首次运行落盘 tests/snapshots/final_pick_prompt.txt，后续精确比对。"""
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        text = _render_final_pick()
        if FINAL_PICK_SNAPSHOT.exists():
            assert FINAL_PICK_SNAPSHOT.read_text(encoding="utf-8") == text
        else:
            FINAL_PICK_SNAPSHOT.write_text(text, encoding="utf-8")
            # 首次落盘自检：非空模板且关键节齐全（防止空/截断文本固化进快照）
            assert "【候选基金（赛道内排序，含重仓股与新闻匹配）】" in text
            assert "selected_code" in text
        # 确定性自检：同入参两次渲染完全一致
        assert _render_final_pick() == text

    def test_schema_keys_consistent_with_validator(self):
        """prompt 回显 JSON schema 键 == _validate_final_pick 消费键（单一来源防漂移）。"""
        schema = _extract_top_level_json(_render_final_pick())
        assert schema is not None
        assert set(schema.keys()) == {
            "selected_code", "selected_name", "decision_logic", "reason", "vetoed",
        }

    def test_roundtrip_parse_and_validate(self):
        """按 prompt schema 构造 LLM 回复 → parse_llm_json → _validate_final_pick 全链路通过。"""
        reply = ('{"selected_code": "000001", "selected_name": "测试基金A", '
                 '"decision_logic": "combo前30%且抗跌性优于同行", '
                 '"reason": "重仓半导体，政策支持国产替代", "vetoed": ["000002"]}')
        parsed = parse_llm_json(reply)
        assert parsed is not None
        result = _validate_final_pick(parsed, {"000001": "测试基金A", "000002": "测试基金B"})
        assert result["selected_code"] == "000001"
        assert result["decision_logic"] == "combo前30%且抗跌性优于同行"
        assert result["vetoed"] == ["000002"]
        # markdown 代码围栏容忍
        assert parse_llm_json("```json\n" + reply + "\n```")["selected_code"] == "000001"


class TestFinalPickMissingFieldTolerance:
    """候选 record 字段缺失容错（现状契约固化，不改生产代码）。"""

    def test_optional_fields_missing_no_keyerror(self):
        """.get() 守卫的可选字段（capture_up/report_date/holdings/sector 等）缺失 → 不抛 KeyError。"""
        base = _base_candidate()
        for key in ("capture_up", "capture_down", "report_date", "holdings", "sector",
                    "momentum_20d", "purchase_status", "downside_vol", "drawdown_60d"):
            cand = {k: v for k, v in base.items() if k != key}
            text = final_pick_prompt([cand], _ctx(), [])
            assert "第1名" in text  # 候选行仍正常渲染

        # 多个可选字段同时缺失（最小可用候选）
        minimal = {k: v for k, v in base.items()
                   if k not in ("capture_up", "capture_down", "report_date", "holdings",
                                "sector", "momentum_20d", "purchase_status",
                                "downside_vol", "drawdown_60d",
                                "sector_median_mom", "mom_gap")}
        text2 = final_pick_prompt([minimal], _ctx(), [])
        assert "第1名" in text2
        assert "000001" in text2

    def test_required_fields_missing_pins_current_contract(self):
        """现状记录：calmar/hurst_60d/combo/code/name 为直接索引（c['x']），缺失抛 KeyError。

        已实测确认：生产代码对 calmar 等字段直接索引，缺字段确实炸（KeyError）。
        本测试只固化现有行为（不改 app/ 代码）；若后续生产改为 .get 容错，
        本断言需同步改为"不抛异常"。
        """
        base = _base_candidate()
        for key in ("code", "name", "calmar", "hurst_60d", "combo"):
            cand = {k: v for k, v in base.items() if k != key}
            with pytest.raises(KeyError):
                final_pick_prompt([cand], _ctx(), [])


# ---------- 其余主要 prompt：节锚点顺序 + schema 回显 ----------

class TestSectorSelectionGolden:
    """sector_selection_prompt 节锚点按序出现 + schema 键回显。"""

    def test_section_order_and_schema_keys(self):
        p = _render_sector_selection()
        _assert_section_order(p, [
            "【量化候选池】",
            "【行业板块排行】",
            "【财经新闻】",
            "【当日新闻使用规则",
            "【数据同源提示",
            "【多日新闻趋势】",
            "【大盘技术面】",
            "【regime_label】",
            "【资金流向】",
            "【历史教训",
            "【否决权】",
            "【高位与资金流出约束",
            "输出以下 JSON",
        ])
        assert "共2行" in p  # 候选池行数声明
        # schema 模板含 `...` 占位（非合法 JSON），用行首缩进正则提取顶层键
        schema_keys = _extract_schema_keys(p, after="输出以下 JSON")
        assert schema_keys == {
            "recommended_sectors", "risk_sectors", "vetoed_sectors", "regime_label", "reasoning",
        }


class TestMonitorLogicGolden:
    """monitor_logic_prompt 节锚点 + R4 量化阈值回显（与常量单一来源一致）。"""

    def test_section_order_and_r4_thresholds(self):
        from app.llm.prompts import R4_EXPOSURE_DROP_PCT, R4_HOLDINGS_LOST, R4_WEIGHT_HALVE

        p = monitor_logic_prompt(
            buy_reason="重仓电源设备，政策利好",
            sector="电源设备",
            anchor_sector="电源设备",
            anchor_report_date="2026-06-30",
            anchor_holdings_text="宁德时代(8.2%), 阳光电源(6.5%)",
            holdings_text="宁德时代(7.1%), 阳光电源(5.0%)",
            rbsa_distribution="电源设备(45%), 半导体(15%), 通信设备(10%)",
        )
        _assert_section_order(p, [
            "买入逻辑:",
            "【推荐时论点锚点】",
            "核心行业:",
            "【最新持仓结构】",
            "输出纯 JSON：",
            "判定规则",
        ])
        assert "最新行业分布: 电源设备(45%), 半导体(15%), 通信设备(10%)" in p
        # R4 阈值来自单一常量源（prompt 与日志共用，防两处口径漂移）
        assert f"下降超过 {R4_EXPOSURE_DROP_PCT:.0f} 个百分点" in p
        assert f"下降超过 {R4_WEIGHT_HALVE * 100:.0f}%" in p
        assert f"≥{R4_HOLDINGS_LOST} 只" in p
        # schema 键回显（模板含 true/false 占位符，非合法 JSON，只断言键名在文本中）
        for key in ("logic_verdict", "signal_hint", "sector_risk", "holding_risk", "reason"):
            assert f'"{key}"' in p


class TestEvolutionAnalysisGolden:
    """evolution_analysis_prompt 节锚点 + 裁决损耗观测格式化。"""

    def test_sections_and_loss_observation(self):
        successes = [{
            "sectors": "半导体", "fund": "000001", "regime": "bullish",
            "outcome": "+8.2%", "note": "景气延续", "reasoning": "动量结构好",
            "signal": "BUY", "signal_triggers": {"sector_mom": 1}, "logic": {"combo": "top"},
        }]
        failures = [{
            "sectors": "白酒", "fund": "000002", "regime": "bearish",
            "outcome": "-5.1%", "note": "政策利空", "reasoning": "逆势",
            "signal": "SELL", "signal_triggers": {}, "logic": {},
        }]
        neutrals = [{
            "sectors": "医药", "fund": "000003", "regime": "neutral",
            "outcome": "+0.2%", "note": "震荡", "reasoning": "观望",
            "signal": "HOLD", "signal_triggers": {}, "logic": {},
        }]
        p = evolution_analysis_prompt(successes, failures, neutrals,
                                      decision_loss=-0.85, loss_streak=3)
        _assert_section_order(p, [
            "成功案例:",
            "失败案例:",
            "中性案例（",
            "【定论环节裁决损耗观测】",
            "对比成功和失败案例",
        ])
        assert "-85.00pp" in p  # -0.85*100 = -85.00pp（负值 = 定论环节拉低质量）
        assert "已连续 3 个月为负" in p
        assert "输出 JSON 数组" in p

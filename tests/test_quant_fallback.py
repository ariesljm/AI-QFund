"""ticket 11 推荐量化降级兜底测试。

覆盖：
- `_maybe_quant_fallback` 触发条件（理由过短 / 排位后 70% / 已在 Top1 不触发 /
  不在候选池 / 空池）；
- `quant_fallback` 作为 reco_path 被 quality 分口径追踪（不污染 sector）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.repo as repo
from app.engine import quality
from app.engine import recommend as rec


def _cand(code: str, combo: float) -> dict:
    return {"code": code, "name": f"基金{code}", "combo": combo, "sector": "半导体"}


class TestMaybeQuantFallback:
    def test_top1_with_valid_reason_no_fallback(self):
        cands = [_cand("A", 0.9), _cand("B", 0.5)]
        result = {"selected_code": "A", "selected_name": "基金A", "reason": "重仓半导体景气上行"}
        assert rec._maybe_quant_fallback(result, cands) is None

    def test_short_reason_triggers_fallback(self):
        cands = [_cand("A", 0.9), _cand("B", 0.5)]
        result = {"selected_code": "B", "selected_name": "基金B", "reason": ""}
        fb = rec._maybe_quant_fallback(result, cands)
        assert fb is not None
        assert fb["selected_code"] == "A"
        assert fb["selected_name"] == "基金A"
        assert fb["quant_fallback"] is True
        assert "buy_reason" in fb["fallback_cause"]

    def test_low_rank_triggers_fallback(self):
        cands = [_cand(f"F{i}", 1.0 - i * 0.05) for i in range(10)]
        result = {"selected_code": "F7", "selected_name": "基金F7", "reason": "理由充分且逻辑自洽的说明"}
        fb = rec._maybe_quant_fallback(result, cands)
        assert fb is not None
        assert fb["selected_code"] == "F0"
        assert "排位" in fb["fallback_cause"]

    def test_rank_exactly_30pct_no_fallback(self):
        cands = [_cand(f"F{i}", 1.0 - i * 0.05) for i in range(10)]
        result = {"selected_code": "F2", "selected_name": "基金F2", "reason": "理由充分且逻辑自洽的说明"}
        # F2 排名 3/10 = 30%，未超阈值 → 不降级
        assert rec._maybe_quant_fallback(result, cands) is None

    def test_fallback_preserves_llm_vetoed(self):
        cands = [_cand("A", 0.9), _cand("B", 0.5)]
        result = {"selected_code": "B", "selected_name": "基金B", "reason": "",
                  "vetoed": ["C", "D"]}
        fb = rec._maybe_quant_fallback(result, cands)
        assert fb["vetoed"] == ["C", "D"]

    def test_empty_candidates_no_fallback(self):
        result = {"selected_code": "A", "selected_name": "基金A", "reason": ""}
        assert rec._maybe_quant_fallback(result, []) is None

    def test_selected_not_in_pool_triggers_fallback(self):
        """LLM 选中不在候选池（防御）：排位视为最末 → 降级。"""
        cands = [_cand("A", 0.9), _cand("B", 0.5)]
        result = {"selected_code": "ZZZ", "selected_name": "基金ZZZ", "reason": "理由充分且逻辑自洽的说明"}
        fb = rec._maybe_quant_fallback(result, cands)
        assert fb is not None
        assert fb["selected_code"] == "A"


class TestQuantFallbackQualityTracking:
    def test_quant_fallback_tracked_in_by_path(self, monkeypatch):
        """quant_fallback 作为 reco_path 被分口径追踪（不污染 sector）。"""
        rows = [
            ("F1", "2026-06-01", 0.5, None, "quant_fallback"),
            ("F2", "2026-06-02", 0.4, None, "sector"),
        ]
        monkeypatch.setattr(repo, "get_quality_sample_rows", lambda a, b: rows)
        monkeypatch.setattr(repo.nav, "forward_return", lambda code, d: 0.03)

        m = quality.compute_quality_metrics("2026-06-01", "2026-06-30")

        assert m["by_path"]["quant_fallback"]["sample_count"] == 1
        assert m["by_path"]["sector"]["sample_count"] == 1
        assert m["points"][0]["reco_path"] == "quant_fallback"

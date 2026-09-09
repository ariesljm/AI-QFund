"""Ticket 05：推荐入场质量门槛测试（S1 接缝的聚焦版）。

回归根因：R1"全天候出手"关闭了 MIN_PREDICTED_ALPHA=0 硬门槛，
熊市里预测为负/趋近 0 的基金照推（实证 28 笔中 6 笔 score≤0.01、1 笔为负）。
契约：
- 赛道内候选按模型预测 >0 硬过滤，负预测不进终选池；
- 过滤后为空 → 不绕过门槛（返回空，由上游空推荐日兜底）；
- 降级路径（全市场 Top10）同样过滤，全负 → 返回空。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from app import domain
from app.engine import recommend
from app.engine.macro_agent import MacroContext


def _feat_row(code, name, sector, score_offset=0.0):
    """构造一张 fund_features 行（FEATURE_COLS 全有值）。"""
    row = {c: 1.0 for c in domain.FEATURE_COLS}
    row.update({
        "code": code, "name": name, "sector": sector,
        "rbsa_industry_1": sector, "rbsa_weight_1": 60.0,
        "rbsa_industry_2": "食品", "rbsa_weight_2": 5.0,
        "rbsa_industry_3": "医药", "rbsa_weight_3": 5.0,
        "regime": "BEAR", "_score_offset": score_offset,
    })
    return row


class FakeModel:
    """固定预测分：按行序返回 scores（忽略特征）。"""

    def __init__(self, scores):
        self._scores = scores

    def predict(self, X):
        return np.array(self._scores, dtype=float)


def _patch_repo(monkeypatch, sector_rows, all_rows=None, regime="BEAR"):
    monkeypatch.setattr(recommend.repo, "get_available_sectors",
                        lambda: ["半导体", "食品"])
    monkeypatch.setattr(recommend.repo, "get_sector_candidates",
                        lambda sectors: sector_rows)
    monkeypatch.setattr(recommend.repo, "get_ranking_cfg",
                        lambda: domain.RankingConfig())
    monkeypatch.setattr(recommend.repo, "get_index_momentum", lambda: -1.0)
    monkeypatch.setattr(recommend.repo, "get_market_regime", lambda: regime)
    # 指数序列：market_state_features 需要 ≤60 根线（50 根平缓线即可）
    n = 50
    monkeypatch.setattr(recommend.repo, "get_index_series",
                        lambda code, cols: [(f"2026-06-{i:02d}", 3000.0 + i, 1e7)
                                            for i in range(n)])
    monkeypatch.setattr(recommend.repo, "get_all_ranking_rows",
                        lambda: all_rows if all_rows is not None else sector_rows)


def _ctx(sectors=("半导体",)):
    return MacroContext(recommended_sectors=list(sectors))


class TestSectorCandidatesPositiveOnly:
    def test_negative_score_filtered_out(self, monkeypatch):
        """两只候选一只预测负 → 终选池只剩正的一只。"""
        rows = [_feat_row("F1", "基金一", "半导体"), _feat_row("F2", "基金二", "半导体")]
        _patch_repo(monkeypatch, rows)
        model = FakeModel([-0.03, 0.05])  # F1 负、F2 正
        out = recommend._rank_within_sectors(_ctx(), model)
        codes = [c["code"] for c in out]
        assert codes == ["F2"]

    def test_all_negative_returns_empty(self, monkeypatch):
        """赛道内全负预测 → 返回空（不降级绕过门槛；上游走空推荐日）。"""
        rows = [_feat_row("F1", "基金一", "半导体"), _feat_row("F2", "基金二", "半导体")]
        _patch_repo(monkeypatch, rows)
        model = FakeModel([-0.02, -0.01])
        out = recommend._rank_within_sectors(_ctx(), model)
        assert out == []


class TestDegradePathPositiveOnly:
    def test_all_market_negative_returns_empty(self, monkeypatch):
        """降级路径（全市场 Top10）：全负预测 → 返回空（空推荐日兜底）。"""
        _patch_repo(monkeypatch, [], all_rows=[
            _feat_row("F3", "基金三", "半导体"), _feat_row("F4", "基金四", "食品")])
        model = FakeModel([-0.01, -0.02])
        out = recommend.rank_funds(model)
        assert out == []

    def test_mixed_keeps_positive_only(self, monkeypatch):
        """降级路径：混合预测分 → 只保留正预测基金。"""
        _patch_repo(monkeypatch, [], all_rows=[
            _feat_row("F3", "基金三", "半导体"), _feat_row("F4", "基金四", "食品")])
        model = FakeModel([-0.01, 0.04])
        out = recommend.rank_funds(model)
        assert [c["code"] for c in out] == ["F4"]

    def test_positive_candidates_bypass_no_threshold(self, monkeypatch):
        """回归：正常正预测候选不受影响（门槛不误伤）。"""
        rows = [_feat_row("F1", "基金一", "半导体", score_offset=None)]
        _patch_repo(monkeypatch, rows)
        model = FakeModel([0.06])
        out = recommend._rank_within_sectors(_ctx(), model)
        assert [c["code"] for c in out] == ["F1"]

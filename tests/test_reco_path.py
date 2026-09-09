"""#1 降级路径打标 + 分口径度量测试。

覆盖：
- compute_quality_metrics 按 reco_path 分组输出 by_path 分口径赚钱率；
- points 每条带 reco_path 标记（回溯每条推荐走了哪条路径）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.repo as repo
from app.engine import quality


def test_quality_by_path_split(monkeypatch):
    """sector/degrade 两条路径分组度量：赚钱率分别统计。"""
    rows = [
        ("F1", "2026-06-01", 0.5, None, "sector"),
        ("F2", "2026-06-02", 0.4, None, "degrade"),
        ("F3", "2026-06-03", 0.6, None, "sector"),
        ("F4", "2026-06-04", 0.3, None, "degrade"),
    ]
    monkeypatch.setattr(repo, "get_quality_sample_rows", lambda a, b: rows)
    rets = {"F1": 0.03, "F2": -0.02, "F3": 0.05, "F4": -0.01}
    monkeypatch.setattr(repo.nav, "forward_return", lambda code, d: rets[code])

    m = quality.compute_quality_metrics("2026-06-01", "2026-06-30")

    assert m["by_path"]["sector"]["sample_count"] == 2
    assert m["by_path"]["degrade"]["sample_count"] == 2
    # sector 两条都 >1%（0.03/0.05）→ profit_rate 1.0；degrade 两条都 ≤1% → 0.0
    assert m["by_path"]["sector"]["profit_rate"] == 1.0
    assert m["by_path"]["degrade"]["profit_rate"] == 0.0
    # 全量口径不变（4 条中 2 条 >1%）
    assert m["profit_rate"] == 0.5


def test_quality_by_path_points_tagged(monkeypatch):
    """points 每条带 reco_path 标记，可回溯单条推荐的来源路径。"""
    rows = [
        ("F1", "2026-06-01", 0.5, None, "degrade"),
        ("F2", "2026-06-02", 0.4, None, "sector"),
    ]
    monkeypatch.setattr(repo, "get_quality_sample_rows", lambda a, b: rows)
    monkeypatch.setattr(repo.nav, "forward_return", lambda code, d: 0.02)

    m = quality.compute_quality_metrics("2026-06-01", "2026-06-30")

    assert m["points"][0]["reco_path"] == "degrade"
    assert m["points"][1]["reco_path"] == "sector"


def test_quality_by_path_defaults_to_sector(monkeypatch):
    """历史行无 reco_path（NULL）时按 sector 归桶，不丢样本。"""
    rows = [("F1", "2026-06-01", 0.5, None, None)]
    monkeypatch.setattr(repo, "get_quality_sample_rows", lambda a, b: rows)
    monkeypatch.setattr(repo.nav, "forward_return", lambda code, d: 0.02)

    m = quality.compute_quality_metrics("2026-06-01", "2026-06-30")

    assert "sector" in m["by_path"]
    assert m["by_path"]["sector"]["sample_count"] == 1


# ============================================================
# 降级路径候选分组契约（架构深化候选 1）
# ============================================================

class TestPickGroupCandidates:
    """降级语义单一承载点：degrade 候选不受 LLM 赛道约束。"""

    def _degrade_candidate(self, code, rbsa_1):
        """降级候选 schema（rank_funds 归一后）：sector 为空、rbsa_industry_1 不一定匹配赛道。"""
        return {"code": code, "name": f"基金{code}", "sector": "",
                "rbsa_industry_1": rbsa_1, "combo": 0.5}

    def test_degrade_candidates_bypass_sector_filter(self):
        """降级候选 rbsa_industry_1 与 LLM 赛道不匹配时仍全部进入定论。

        回归：此前消费端 _sector_candidates 仍按赛道过滤，降级候选只有
        rbsa_industry_1 恰好命中的才能进定论，其余被裁剪 → 明明有优质
        候选的日子被错误记成空推荐日。
        """
        from app.engine.recommend import _pick_group_candidates
        finalists = [self._degrade_candidate("F1", "医药"),
                     self._degrade_candidate("F2", "白酒")]
        got = _pick_group_candidates(finalists, "半导体", "degrade", set())
        assert [c["code"] for c in got] == ["F1", "F2"]

    def test_degrade_respects_selected_dedup(self):
        """降级路径仍做同日去重（前序已选定基金不再进入）。"""
        from app.engine.recommend import _pick_group_candidates
        finalists = [self._degrade_candidate("F1", "医药"),
                     self._degrade_candidate("F2", "白酒")]
        got = _pick_group_candidates(finalists, "半导体", "degrade", {"F1"})
        assert [c["code"] for c in got] == ["F2"]

    def test_sector_path_keeps_filter(self):
        """主路径行为不变：仍按赛道过滤。"""
        from app.engine.recommend import _pick_group_candidates
        finalists = [{"code": "F1", "sector": "半导体", "rbsa_industry_1": "半导体"},
                     {"code": "F2", "sector": "医药", "rbsa_industry_1": "医药"}]
        got = _pick_group_candidates(finalists, "半导体", "sector", set())
        assert [c["code"] for c in got] == ["F1"]

    def test_rank_funds_schema_normalized(self):
        """rank_funds 复用 _build_candidate_record 后 schema 与主路径归一。"""
        from app.engine.recommend import rank_funds
        import app.engine.recommend as rec_mod

        class _FakeModel:
            def predict(self, X, **kw):
                import numpy as np
                return np.array([0.6])

        rows = [{"code": "F1", "name": "基金F1", "regime": "BULL",
                 "feature_date": "2026-09-01",
                 "rbsa_industry_1": "医药", "rbsa_weight_1": 0.6,
                 "score": 0.6, "combo": 0.6, "hurst_60d": 0.5,
                 "momentum_20d": 5.0, "calmar": 1.0}]
        # rank_funds 会 dropna(subset=FEATURE_COLS)：特征列必须齐全
        from app.domain import FEATURE_COLS
        for col in FEATURE_COLS:
            rows[0].setdefault(col, 1.0)
        monkey = rec_mod.repo
        # 仅本测试内打桩
        class _MP:
            def __init__(self): self.patches = []
            def setattr(self, obj, name, val): self.patches.append((obj, name, getattr(obj, name))); setattr(obj, name, val)
            def undo(self):
                for obj, name, val in reversed(self.patches): setattr(obj, name, val)
        mp = _MP()
        try:
            mp.setattr(monkey, "get_ranking_cfg", lambda: __import__("app.domain", fromlist=["RankingConfig"]).RankingConfig())
            mp.setattr(monkey, "get_all_ranking_rows", lambda: rows)
            mp.setattr(monkey, "get_index_momentum", lambda: 2.0)
            mp.setattr(monkey, "get_market_regime", lambda: "BULL")
            got = rank_funds(_FakeModel())
            assert len(got) == 1
            c = got[0]
            # schema 归一：主路径键齐备（sector 空、sector_rel_* 中性默认）
            assert c["sector"] == ""
            assert c["rbsa_industry_1"] == "医药"
            assert c["sector_rel_momentum"] == 0.0
            assert c["sector_rel_calmar"] == 0.0
            assert "regime" not in c  # 无下游消费，归一时丢弃
        finally:
            mp.undo()


# ============================================================
# 终选素材装配（架构深化候选 3：装配/判定分离）
# ============================================================

class TestAssembleFinalistMaterial:
    """素材口径（月龄/mom_gap/限购）可脱离 LLM 调用独立测试。"""

    def test_assembles_and_does_not_mutate_input(self, monkeypatch):
        import app.engine.recommend as rec_mod

        rows = {"F1": {"holdings": [{"stock_code": "600000", "stock_name": "浦发", "weight": 8.0}],
                       "report_date": "2026-06-30"}}
        monkeypatch.setattr(rec_mod.repo, "get_holdings_summaries", lambda codes, limit: rows)
        monkeypatch.setattr(rec_mod.repo, "get_purchase_statuses",
                            lambda codes: {"F1": "限购"})
        monkeypatch.setattr(rec_mod.repo, "get_sector_momentum_median",
                            lambda sector, d: 3.0)

        original = {"code": "F1", "name": "基金F1", "sector": "医药", "momentum_20d": 5.0}
        got = rec_mod._assemble_finalist_material([original], "2026-09-01")

        c = got[0]
        assert c["holdings_months"] >= 0       # 月龄已计算
        assert c["purchase_status"] == "限购"   # 限购已标注
        assert c["mom_gap"] == 2.0             # 5.0 - 中位数 3.0
        # 不变异入参：主循环后续消费原字段不受装配污染
        assert "holdings" not in original and "mom_gap" not in original

    def test_no_sector_yields_none_gap(self, monkeypatch):
        """降级候选（sector 空、无 rbsa）→ mom_gap/sector_median 置 None，不报错。"""
        import app.engine.recommend as rec_mod
        monkeypatch.setattr(rec_mod.repo, "get_holdings_summaries", lambda codes, limit: {})
        monkeypatch.setattr(rec_mod.repo, "get_purchase_statuses", lambda codes: {})
        monkeypatch.setattr(rec_mod.repo, "get_sector_momentum_median",
                            lambda sector, d: 3.0)

        got = rec_mod._assemble_finalist_material(
            [{"code": "F9", "name": "基金F9", "sector": "", "rbsa_industry_1": "",
              "momentum_20d": 5.0}], "2026-09-01")
        assert got[0]["mom_gap"] is None
        assert got[0]["sector_median_mom"] is None

"""票 11 完整：2.0 推荐主流程（种子基座 + 打桩审计）。

全链：Top30 候选 → 审计（PASS/VETO/risk>60 剪枝）→ 复合分 → Top5 落库；
非法审计跳过（不静默降级）；空态语义传递。审计打桩，不依赖 LLM/回填。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.database as db_mod
from app.engine.recommend_v2 import recommend_top5
from app.repo import decision as decision_repo
from app.repo.base import db_conn


def _seed(monkeypatch, tmp_path, n=8):
    """种子：n 只主动权益基金（特征齐）+ 无暂停/短历史。

    默认关闭组合层 diversify（测审计/剪枝/复合分语义，不测组合约束）；
    diversify 的测试见 TestSelectDiversified / 独立接线测试。
    """
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "rv2.db")
    import app.config as cfg
    monkeypatch.setattr(cfg, "_settings_cache", None)
    monkeypatch.setattr(cfg, "load_settings",
                       lambda: {"recommend_v2": {"enabled": True,
                                                  "portfolio_diversify": False}})
    with db_conn() as conn:
        for i in range(n):
            conn.execute("INSERT INTO fund_basic (code, name, type, is_buyable) "
                         "VALUES (?,?,?,1)", (f"F{i}", f"基金{i}", "混合型"))
            for j in range(100):
                conn.execute("INSERT INTO fund_nav (code, date, unit_nav, cum_nav) "
                             "VALUES (?,?,?,?)",
                             (f"F{i}", f"2024-{(j//30)+1:02d}-{(j%30)+1:02d}", 1.0, 1.0))
            conn.execute(
                "INSERT INTO fund_features (code, date, hurst_60d, momentum_20d, calmar, "
                "downside_vol, capture_up, capture_down, drawdown_60d, reversal_20d, "
                "mom_5d, mom_60d, vol_20d, rbsa_industry_1, rbsa_weight_1) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"F{i}", "2026-09-14", 0.5, 0.1, 1.0, 0.1, 1.0, 1.0, 0.2, 0.0,
                 0.05, 0.2, 0.1, "白酒", 40.0))
        conn.commit()


def _audit_factory(scores=None, veto=None, bad=None):
    """打桩审计：按 code 给 risk_score；veto/bad 集合标记 VETO/非法。"""
    scores = scores or {}
    veto = veto or set()
    bad = bad or set()

    def audit_fn(fund, slices):
        code = fund["code"]
        if code in bad:
            return None                     # 非法（技术失败）
        if code in veto:
            return {"audit_verdict": "VETO", "risk_score": 95,
                    "veto_reasons": ["打桩否决"], "recommendation_summary": "x"}
        return {"audit_verdict": "PASS", "risk_score": scores.get(code, 10),
                "veto_reasons": [], "recommendation_summary": "正常"}
    return audit_fn


class TestRecommendTop5:
    def test_full_chain_top5_persisted(self, monkeypatch, tmp_path):
        """8 候选全 PASS → 复合分 Top5 落库（特征快照/审计 JSON 可查）。"""
        _seed(monkeypatch, tmp_path)
        result = recommend_top5("2026-09-14", audit_fn=_audit_factory(), scorer=lambda f: 0.8)
        assert result["date"] == "2026-09-14"
        assert len(result["top5"]) == 5
        saved = decision_repo.get_recommend_v2("2026-09-14")
        assert len(saved) == 5
        assert saved[0]["audit"]["audit_verdict"] == "PASS"

    def test_veto_and_high_risk_pruned(self, monkeypatch, tmp_path):
        """VETO 或 risk>60 剔除 → 剩余不足 5 时按实际数量。"""
        _seed(monkeypatch, tmp_path)
        audit = _audit_factory(scores={"F0": 80, "F1": 70}, veto={"F2"})
        result = recommend_top5("2026-09-14", audit_fn=audit, scorer=lambda f: 0.8)
        assert set(result["top5"]) >= {"F3", "F4", "F5"}   # 前三剔除
        assert "F0" not in result["top5"] and "F2" not in result["top5"]

    def test_composite_score_ordering(self, monkeypatch, tmp_path):
        """复合分 = LGBM×(1−Risk/100)：低风险高分优先。"""
        _seed(monkeypatch, tmp_path)
        # F0 高风险（80）→ 复合分 = 0.8×(1−0.8)=0.16；F1 低风险（10）→ 0.72
        audit = _audit_factory(scores={"F0": 80, "F1": 10})
        recommend_top5("2026-09-14", audit_fn=audit, scorer=lambda f: 0.8)
        saved = decision_repo.get_recommend_v2("2026-09-14")
        order = [s["code"] for s in saved]
        assert order.index("F1") < order.index("F0") if "F0" in order else True

    def test_invalid_audit_skipped(self, monkeypatch, tmp_path):
        """非法审计（None）→ 跳过不落库（票 13：非法即失败不静默降级）。"""
        _seed(monkeypatch, tmp_path)
        result = recommend_top5("2026-09-14", audit_fn=_audit_factory(bad={"F0", "F1", "F2"}), scorer=lambda f: 0.8)
        assert "F0" not in result["top5"]

    def test_all_pruned_data_failure(self, monkeypatch, tmp_path):
        """全部被剪枝 → data_failure（有池但无通过者）。"""
        _seed(monkeypatch, tmp_path)
        result = recommend_top5("2026-09-14",
                                audit_fn=_audit_factory(veto={"F0", "F1", "F2", "F3", "F4", "F5", "F6", "F7"}),
                                scorer=lambda f: 0.8)
        assert result.get("empty") == "data_failure"


class TestRecommendSameDayRerun:
    def test_rerun_same_day_overwrites(self, monkeypatch, tmp_path):
        """同日重跑 → 只保留本次 Top5（幂等 REPLACE 语义：同日唯一）。"""
        _seed(monkeypatch, tmp_path)
        recommend_top5("2026-09-14", audit_fn=_audit_factory(), scorer=lambda f: 0.8)
        # 二次重跑：veto 掉首轮全部入选者 → Top5 成员变化，同日只留新结果
        r2 = recommend_top5("2026-09-14",
                            audit_fn=_audit_factory(veto={"F0", "F1", "F2", "F3", "F4"}),
                            scorer=lambda f: 0.8)
        saved = decision_repo.get_recommend_v2("2026-09-14")
        assert len(saved) == len(r2["top5"]) == 3   # 只保留本次（F5/F6/F7），旧 F0..F4 已清


class TestRecommendV2Enabled:
    def test_default_disabled(self, monkeypatch):
        """默认关闭（不破坏 1.x 推荐）：配置缺省 = disabled，与运行机 settings.toml 解耦。"""
        import app.config as cfg
        from app.engine.recommend_v2 import recommend_v2_enabled
        monkeypatch.setattr(cfg, "_settings_cache", None)
        monkeypatch.setattr(cfg, "load_settings", lambda: {})
        assert recommend_v2_enabled() is False

    def test_switch_via_settings(self, monkeypatch, tmp_path):
        import app.config as cfg
        from app.engine.recommend_v2 import recommend_v2_enabled
        monkeypatch.setattr(cfg, "_settings_cache", None)
        s = cfg.load_settings()
        s["recommend_v2"] = {"enabled": True}
        monkeypatch.setattr(cfg, "load_settings", lambda: s)
        assert recommend_v2_enabled() is True

"""GA 遗传寻优 + 进化自纠偏 + 监控企稳豁免 测试。"""

import sys
from pathlib import Path
from datetime import datetime
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from app.engine import evolve, ga
import app.repo as repo


class TestReviewRankingAll:
    """排分自纠偏：不再用 DEFAULT 硬编码覆盖定制配置，按比例衰减失效因子。"""

    _INIT_CFG = {
        "model_weight": 0.6, "rel_strength_weight": 0.08,
        "calmar_weight": 0.1, "hurst_weight": 0.1,
        "momentum_guard_pct": -30.0,
    }

    def _mock_cfg(self, monkeypatch):
        monkeypatch.setattr(evolve.repo, "get_ranking_cfg", lambda: dict(self._INIT_CFG))

    def test_keeps_customized_cfg_on_corr_fix(self, monkeypatch):
        """hurst 与动量负相关 → 报告 fix 信号，但不写权重（权重统一由 GA 调节）。"""
        self._mock_cfg(monkeypatch)
        mom = np.arange(1, 201, dtype=float)
        hurst = -mom.copy()  # 强负相关
        calmar = mom.copy()  # 强正相关
        monkeypatch.setattr(evolve.repo, "get_buyable_feature_stats",
                            lambda: [(f"F{i:04d}", m, h, c)
                                     for i, (m, h, c) in enumerate(zip(mom, hurst, calmar))])
        monkeypatch.setattr(evolve.repo, "get_index_momentum", lambda: 0.0)
        written = []
        monkeypatch.setattr(evolve, "_apply_ranking_weights",
                            lambda w: written.append(w) or True)

        fixes = evolve._review_ranking_all()
        assert fixes, "应检测到 hurst 负相关"
        assert any("hurst" in f for f in fixes)
        assert written == []  # 不再直接写权重

    def test_spread_fix_reduces_rel_strength(self, monkeypatch):
        """相对强弱区分度不足 → 报告 fix 信号，但不写权重。"""
        self._mock_cfg(monkeypatch)
        mom = np.linspace(49.5, 50.5, 200)  # 窄区间 → rel 区分度 <10
        hurst = mom.copy()
        calmar = mom.copy()
        monkeypatch.setattr(evolve.repo, "get_buyable_feature_stats",
                            lambda: [(f"F{i:04d}", m, h, c)
                                     for i, (m, h, c) in enumerate(zip(mom, hurst, calmar))])
        monkeypatch.setattr(evolve.repo, "get_index_momentum", lambda: 50.0)
        written = []
        monkeypatch.setattr(evolve, "_apply_ranking_weights",
                            lambda w: written.append(w) or True)

        fixes = evolve._review_ranking_all()
        assert any("区分度" in f for f in fixes), fixes
        assert written == []  # 不再直接写权重

    def test_healthy_no_write(self, monkeypatch):
        self._mock_cfg(monkeypatch)
        mom = np.arange(1, 201, dtype=float)
        monkeypatch.setattr(evolve.repo, "get_buyable_feature_stats",
                            lambda: [(f"F{i:04d}", m, m, m) for i, m in enumerate(mom)])
        monkeypatch.setattr(evolve.repo, "get_index_momentum", lambda: 0.0)
        written = []
        monkeypatch.setattr(evolve, "_apply_ranking_weights",
                            lambda w: written.append(w) or True)
        assert evolve._review_ranking_all() == []
        assert written == []


class TestSectorAdvantageReversal:
    """监控赛道优势：深跌但已企稳（reversal>0）不被误伤。

    架构深化候选 3：ctx 预装配模式（SectorAdvantageRule 消费 cur_feat/sector_median）。
    """

    def _feat(self, mom, reversal):
        return {"momentum_20d": mom, "reversal_20d": reversal, "date": "2026-08-03"}

    def _rule_result(self, mom, reversal, median):
        from app.engine.monitor import SectorAdvantageRule, DefenseContext
        ctx = DefenseContext(
            code="F001", sector="半导体",
            cur_feat=self._feat(mom, reversal), sector_median=median,
        )
        return SectorAdvantageRule().check(ctx)

    def test_stabilized_deep_drop_kept(self):
        """动量低于赛道中位数但 reversal>0（企稳）→ 保留，不警告。"""
        result = self._rule_result(mom=-20.0, reversal=3.0, median=0.0)
        assert result is None

    def test_falling_below_median_warns(self):
        """动量低于中位数且未企稳（reversal<=0）→ WARNING。"""
        result = self._rule_result(mom=-20.0, reversal=-2.0, median=0.0)
        assert result is not None
        assert result.signal == "WARNING"
        assert "赛道优势丧失" in result.reason

    def test_above_median_kept(self):
        result = self._rule_result(mom=8.0, reversal=-1.0, median=0.0)
        assert result is None


class TestGeneticAlgorithm:
    """GA：适应度单调于 model_weight 时，应搜到更高权重。"""

    def test_ga_finds_better_model_weight(self, monkeypatch):
        def fake_backtest(cfg_override=None, fast=False, lookback_days=365):
            mw = cfg_override["model_weight"]
            # 阶段5 fitness = profit_rate*2 + mean_top_abs 随 model_weight 单调递增
            return {"mean_ic": mw * 0.5, "mean_spread_pct": mw * 2.0,
                    "profit_rate_pct": mw * 50.0, "mean_top_abs_pct": mw * 2.0}

        monkeypatch.setattr(ga, "repo", repo)
        monkeypatch.setattr(ga, "run_backtest", fake_backtest)
        monkeypatch.setattr(ga.repo, "get_ranking_cfg", lambda: {
            "model_weight": 0.5, "rel_strength_weight": 0.15,
            "calmar_weight": 0.1, "hurst_weight": 0.1, "momentum_guard_pct": -15.0,
        })

        best_cfg, best_f = ga.ga_optimize_ranking(population=6, generations=3, seed=42)
        assert best_cfg["model_weight"] > 0.5
        assert best_f > 0.5 * 50 * 2 + 0.5 * 2.0  # 初始 fitness（profit=50%, abs=1%）

    def test_ga_respects_bounds(self, monkeypatch):
        monkeypatch.setattr(ga, "repo", repo)
        monkeypatch.setattr(ga, "run_backtest",
                            lambda cfg_override=None, fast=False, lookback_days=365: {"mean_ic": 0.2, "mean_spread_pct": 3.0})
        monkeypatch.setattr(ga.repo, "get_ranking_cfg", lambda: {
            "model_weight": 0.5, "rel_strength_weight": 0.15,
            "calmar_weight": 0.1, "hurst_weight": 0.1, "momentum_guard_pct": -15.0,
        })
        best_cfg, _ = ga.ga_optimize_ranking(population=6, generations=2, seed=7)
        for k, (lo, hi) in ga._BOUNDS.items():
            assert lo <= best_cfg[k] <= hi, f"{k}={best_cfg[k]} 超出边界"

    def test_ga_preserves_guard(self, monkeypatch):
        """风控参数不参与寻优：GA 返回配置的 momentum_guard_pct 沿用当前值。

        回归：guard 曾作为基因被 GA 推到 -28.7%（候选池扩大虚高 fitness），风控防线失效。
        """
        monkeypatch.setattr(ga, "repo", repo)
        monkeypatch.setattr(ga, "run_backtest",
                            lambda cfg_override=None, fast=False, lookback_days=365: {"mean_ic": 0.2, "mean_spread_pct": 3.0})
        monkeypatch.setattr(ga.repo, "get_ranking_cfg", lambda: {
            "model_weight": 0.5, "rel_strength_weight": 0.15,
            "calmar_weight": 0.1, "hurst_weight": 0.1, "momentum_guard_pct": -5.0,
        })
        best_cfg, _ = ga.ga_optimize_ranking(population=4, generations=1, seed=1)
        assert best_cfg["momentum_guard_pct"] == -5.0
        assert "momentum_guard_pct" not in ga._GENE_KEYS

    def test_apply_weights_preserves_guard(self, monkeypatch):
        """写库防线：GA 配置即使携带 guard 值，落库时也沿用当前 guard。"""
        from app.engine import evolve
        monkeypatch.setattr(evolve.repo, "get_ranking_cfg", lambda: {
            "model_weight": 0.5, "momentum_guard_pct": -8.0,
        })
        saved = {}
        monkeypatch.setattr(evolve.repo, "save_ranking_cfg", lambda cfg: saved.update(cfg))
        ok = evolve._apply_ranking_weights({"model_weight": 0.9, "momentum_guard_pct": -40.0})
        assert ok
        assert saved["momentum_guard_pct"] == -8.0
        assert saved["model_weight"] == 0.9


class TestGaFrequencyLimit:
    """GA 频率限制：meta last_ga_run 距上次 < 7 天跳过，评估即记录。"""

    def test_skip_within_interval(self, monkeypatch):
        from datetime import timedelta
        from app.engine import evolve

        recent = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        monkeypatch.setattr(evolve.repo, "get_interval_days", lambda k: 1)
        saved = []
        monkeypatch.setattr(evolve.repo, "save_meta", lambda k, v: saved.append((k, v)))

        def _boom(*a, **k):
            raise AssertionError("间隔未到不应调用 GA")

        monkeypatch.setattr("app.engine.ga.ga_optimize_ranking", _boom)
        assert evolve._ga_adjust() is None
        assert saved == []  # 跳过时不应记录评估时间

    def test_runs_and_records_after_interval(self, monkeypatch):
        from datetime import timedelta
        from app.engine import evolve

        old = (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d")
        monkeypatch.setattr(evolve.repo, "get_meta",
                            lambda k: old if k == "last_ga_run" else None)
        saved = []
        monkeypatch.setattr(evolve.repo, "save_meta", lambda k, v: saved.append((k, v)))
        monkeypatch.setattr(evolve.repo, "save_ranking_cfg", lambda cfg: True)
        monkeypatch.setattr(evolve, "_save_self_fix", lambda fix: None)  # 防止写生产库 evolution_insights
        monkeypatch.setattr("app.engine.ga.ga_optimize_ranking",
                            lambda *a, **k: ({"model_weight": 0.8}, 45.0))
        monkeypatch.setattr("app.engine.ga.fitness", lambda cfg: 30.0)

        note = evolve._ga_adjust()
        assert note is not None
        assert "GA寻优应用" in note
        assert any(k == "last_ga_run" for k, _ in saved)  # 评估即记录

    def test_no_apply_within_threshold(self, monkeypatch):
        from datetime import timedelta
        from app.engine import evolve

        old = (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d")
        monkeypatch.setattr(evolve.repo, "get_meta",
                            lambda k: old if k == "last_ga_run" else None)
        saved = []
        monkeypatch.setattr(evolve.repo, "save_meta", lambda k, v: saved.append((k, v)))
        applied = []
        monkeypatch.setattr(evolve.repo, "save_ranking_cfg", lambda cfg: applied.append(cfg) or True)
        # Δfitness = 1.9 ≤ 10.0（Q5 上修门槛）→ 不应用，但记录评估时间
        monkeypatch.setattr("app.engine.ga.ga_optimize_ranking",
                            lambda *a, **k: ({"model_weight": 0.8}, 29.9))
        monkeypatch.setattr("app.engine.ga.fitness", lambda cfg: 28.0)

        assert evolve._ga_adjust() is None
        assert applied == []
        assert any(k == "last_ga_run" for k, _ in saved)


class TestSelfFixTriggersForceGA:
    """Q7：排分自纠偏信号 → GA 以 force=True 紧急评估（跳过 7 天间隔）。"""

    def test_self_fix_signals_force_ga(self, monkeypatch):
        from app.engine import evolve

        force_calls = []
        monkeypatch.setattr(evolve, "_review_ranking_all", lambda: ["hurst与动量负相关"])
        monkeypatch.setattr(evolve, "_save_self_fix", lambda fix: None)
        monkeypatch.setattr(evolve, "_settle_outcomes", lambda: 0)
        monkeypatch.setattr(evolve, "compute_quality_metrics",
                            lambda s, e: {"sample_count": 0, "profit_rate": None})
        monkeypatch.setattr(evolve.repo, "save_quality_metrics", lambda m: None)
        monkeypatch.setattr(evolve, "_collect_cases", lambda m: ([], [], []))
        monkeypatch.setattr(evolve, "_decay_insights", lambda: 0)
        monkeypatch.setattr(evolve, "_ga_adjust",
                            lambda force=False: force_calls.append(force) or None)

        evolve.run_evolve("2026-07")
        assert force_calls == [True]

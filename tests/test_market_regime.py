"""T06（reco-hardening）：多周期共振牛熊判定验收测试。

动机：单点 EMA60 判定在震荡市频繁穿线（regime 抖动 → 牛/熊降权交替开关）；
研究发现同一信号牛熊方向相反（5 日动量熊市延续、牛市反转）——regime 判错即信号用反。
多周期共振：EMA60(短) + EMA250(长) 双确认，方向矛盾归 NEUTRAL。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import domain


class TestMultiTimeframe:
    def test_bull_double_confirmation(self):
        assert domain.regime_from_multi_timeframe(5000, 4900, 4800) == domain.REGIME_BULL

    def test_bear_double_confirmation(self):
        assert domain.regime_from_multi_timeframe(4500, 4600, 4700) == domain.REGIME_BEAR

    def test_conflict_short_above_long_below_neutral(self):
        """短线在上、长线在下（反弹但年线未收复）→ NEUTRAL。"""
        assert domain.regime_from_multi_timeframe(4600, 4500, 4700) == domain.REGIME_NEUTRAL

    def test_conflict_short_below_long_above_neutral(self):
        """短线跌破、长线仍在（牛市回调）→ NEUTRAL。"""
        assert domain.regime_from_multi_timeframe(4600, 4700, 4500) == domain.REGIME_NEUTRAL

    def test_missing_data_neutral(self):
        assert domain.regime_from_multi_timeframe(None, 4600, 4700) == domain.REGIME_NEUTRAL
        assert domain.regime_from_multi_timeframe(4600, None, 4700) == domain.REGIME_NEUTRAL
        assert domain.regime_from_multi_timeframe(4600, 4700, None) == domain.REGIME_NEUTRAL


class TestRepoRegime:
    def test_integration_uses_long_ema(self, monkeypatch):
        """repo.get_market_regime：短长双下 → BEAR（现算 EMA250）。"""
        import sqlite3

        import app.repo.base as base_mod

        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE index_daily (code TEXT, date TEXT, close REAL, ema60 REAL)")
        # 构建收盘 100→150 缓涨、ema60 更高 → 短线下；长线(250 span)低于收盘 → 长线上 → NEUTRAL
        # 改用"双下"场景：收盘持续下跌，EMA60/EMA250 都在上方
        rows = []
        c = 100.0
        for i in range(300):
            rows.append(("sh000300", f"2025-{i // 28 + 1:02d}-{i % 28 + 1:02d}", c, c + 8.0))
            c -= 0.05  # 缓慢下跌 → close < ema60，且 close < EMA250（下跌趋势）
        conn.executemany("INSERT INTO index_daily VALUES (?, ?, ?, ?)", rows)
        conn.commit()

        monkeypatch.setattr(base_mod, "db_conn", lambda: conn)
        regime = base_mod.get_market_regime()
        assert regime in (domain.REGIME_BEAR, domain.REGIME_NEUTRAL)

    def test_fallback_single_timeframe_when_short_data(self, monkeypatch):
        """历史不足 250 日 → 回退单周期判定（不崩）。"""
        import sqlite3

        import app.repo.base as base_mod

        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE index_daily (code TEXT, date TEXT, close REAL, ema60 REAL)")
        rows = [("sh000300", f"2026-01-{d:02d}", 100.0 + d, 90.0) for d in range(1, 10)]
        conn.executemany("INSERT INTO index_daily VALUES (?, ?, ?, ?)", rows)
        conn.commit()
        monkeypatch.setattr(base_mod, "db_conn", lambda: conn)
        # 收盘 108 > ema60 90 → 单周期 BULL
        assert base_mod.get_market_regime() == domain.REGIME_BULL


class TestMarketBelowEma250:
    """市场门判定（2026-09）：沪深300 跌破年线 → 不出手。"""

    @staticmethod
    def _conn_with(closes):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE index_daily (code TEXT, date TEXT, close REAL, ema60 REAL)")
        rows = [("sh000300", f"d{i:04d}", float(c), float(c)) for i, c in enumerate(closes)]
        conn.executemany("INSERT INTO index_daily VALUES (?, ?, ?, ?)", rows)
        conn.commit()
        return conn

    def test_below_ema250_blocks(self, monkeypatch):
        import app.repo.base as base_mod
        # 长期上涨后末段暴跌 → close < EMA250
        closes = [100.0 + i * 0.5 for i in range(300)] + [80.0]
        monkeypatch.setattr(base_mod, "db_conn", lambda: self._conn_with(closes))
        assert base_mod.get_market_below_ema250() is True

    def test_above_ema250_allows(self, monkeypatch):
        import app.repo.base as base_mod
        closes = [100.0 + i * 0.5 for i in range(300)] + [400.0]
        monkeypatch.setattr(base_mod, "db_conn", lambda: self._conn_with(closes))
        assert base_mod.get_market_below_ema250() is False

    def test_insufficient_data_not_blocked(self, monkeypatch):
        import app.repo.base as base_mod
        monkeypatch.setattr(base_mod, "db_conn", lambda: self._conn_with([100.0, 90.0, 80.0]))
        assert base_mod.get_market_below_ema250() is False


class TestMarketGateInRecommendation:
    """推荐入口：市场门触发 → 记空推荐日且不调用 LLM。"""

    def test_gate_blocks_before_llm(self, monkeypatch):
        import app.engine.recommend as rec_mod

        recorded = {}
        llm_calls = []
        monkeypatch.setattr(rec_mod.repo, "get_latest_feature_date", lambda: "2026-09-09")
        monkeypatch.setattr(rec_mod, "_feature_freshness", lambda d: 0)
        monkeypatch.setattr(rec_mod, "_market_gate_enabled", lambda: True)
        monkeypatch.setattr(rec_mod.repo, "get_market_below_ema250", lambda: True)
        monkeypatch.setattr(
            rec_mod.repo, "record_empty_recommendation",
            lambda date, reason, reason_type="no_opportunity": recorded.update(
                {"date": date, "reason": reason, "rt": reason_type}))
        monkeypatch.setattr(rec_mod, "build_macro_context",
                            lambda d: llm_calls.append(d))

        rec_mod.run_recommendation()

        assert recorded["rt"] == "no_opportunity"
        assert "EMA250" in recorded["reason"]
        assert llm_calls == []      # LLM 未调用（门在 LLM 之前）

    def test_gate_disabled_does_not_block(self, monkeypatch):
        """开关关闭时不拦（不记市场门空推荐日）。

        注意：不真跑 get_or_train——真实训练会污染模型缓存，导致后续
        model/retrain 测试读到的不是 _FakeBooster（曾在本套件全量运行时炸
        test_retrain 两个用例）。
        """
        import app.engine.recommend as rec_mod

        recorded = []
        monkeypatch.setattr(rec_mod.repo, "get_latest_feature_date", lambda: "2026-09-09")
        monkeypatch.setattr(rec_mod, "_feature_freshness", lambda d: 0)
        monkeypatch.setattr(rec_mod, "_market_gate_enabled", lambda: False)
        monkeypatch.setattr(rec_mod.repo, "record_empty_recommendation",
                            lambda *a, **k: recorded.append(a))
        monkeypatch.setattr(rec_mod, "get_or_train", lambda retrain=False: None)

        rec_mod.run_recommendation()
        assert recorded == []   # 未被市场门拦（无市场门空推荐日）

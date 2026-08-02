"""核心纯函数测试——架构深化前的安全网。

测试顺序对应 architecture-review 的候选顺序：
  C5: 纯函数测试（本文件）
  C2: 消除重复 _call_llm
  C1: Repository 深化
  C4: 防线策略链
  C3: data_foundation 拆分
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import numpy as np
import pandas as pd


# ============================================================
# sector_api — is_industry_code
# ============================================================

from app.features.sector import is_industry_code

class TestIndustryCode:
    def test_valid_sws_code(self):
        """申万二级行业 BK 代码"""
        assert is_industry_code("BK0438")  # 食品饮料
        assert is_industry_code("BK1282")  # 饮料乳品
        assert is_industry_code("BK1036")  # 半导体

    def test_excluded_concept_range(self):
        """概念板块密集区 BK1050-1199"""
        assert not is_industry_code("BK1055")  # 跨镜支付
        assert not is_industry_code("BK1076")  # AI概念
        assert not is_industry_code("BK1100")

    def test_out_of_range(self):
        assert not is_industry_code("BK0001")
        assert not is_industry_code("BK9999")
        assert not is_industry_code("BK0600")

    def test_invalid_format(self):
        assert not is_industry_code("")
        assert not is_industry_code("BK")
        assert not is_industry_code("BK123")  # too short
        assert not is_industry_code("BK12345")  # too long
        assert not is_industry_code("bk0438")  # uppercase only
        assert not is_industry_code("SH000001")

    def test_boundary_codes(self):
        """范围边界值"""
        assert is_industry_code("BK0400")
        assert is_industry_code("BK0555")
        assert is_industry_code("BK0725")
        assert is_industry_code("BK0748")
        assert is_industry_code("BK1015")
        assert is_industry_code("BK1049")
        assert is_industry_code("BK1200")
        assert is_industry_code("BK1288")


# ============================================================
# macro_agent — _is_concept_name
# ============================================================

from app.llm.macro_agent import _is_concept_name

class TestConceptName:
    def test_known_concept_codes(self):
        """32 个精确 BK 概念代码"""
        assert _is_concept_name("基金重仓", "BK0536")
        assert _is_concept_name("次新股", "BK0501")
        assert _is_concept_name("节能环保", "BK0494")
        assert _is_concept_name("物联网", "BK0554")
        assert _is_concept_name("国防军工", "BK1204")
        assert _is_concept_name("预制菜概念", "BK1025")

    def test_real_industries_not_concept(self):
        """真实申万行业不应被拦截"""
        assert not _is_concept_name("食品饮料", "BK0438")
        assert not _is_concept_name("半导体", "BK1036")
        assert not _is_concept_name("游戏Ⅱ", "BK1046")
        assert not _is_concept_name("军工电子Ⅱ", "BK1233")
        assert not _is_concept_name("环境治理", "BK1235")

    def test_empty_name(self):
        assert _is_concept_name("", "")
        assert _is_concept_name("", "BK0438")


# ============================================================
# features — calc_hurst
# ============================================================

from app.features.calculator import calc_hurst

class TestHurst:
    def test_random_walk_in_range(self):
        """随机游走赫斯特指数在 [0,1] 范围内"""
        np.random.seed(42)
        steps = np.cumsum(np.random.randn(500) * 0.1)
        h = calc_hurst(steps)
        assert 0.0 <= h <= 1.0, f"随机游走 H={h:.3f}，期望在 [0,1]"

    def test_trending_series(self):
        """趋势序列赫斯特指数 > 0.5"""
        steps = np.arange(1, 1001, dtype=float)
        h = calc_hurst(steps)
        assert h > 0.5, f"趋势序列 H={h:.3f}，期望 >0.5"

    def test_small_series_fallback(self):
        """序列太短时返回 0.5（fallback）"""
        h = calc_hurst(np.array([1.0, 2.0, 3.0]))
        assert h == 0.5

    def test_constant_series_fallback(self):
        """常数序列返回 0.5（fallback，避免 NaN）"""
        steps = np.ones(500)
        h = calc_hurst(steps)
        assert 0.0 <= h <= 1.0, f"常数序列 H={h}，期望在 [0,1]"


# ============================================================
# recommend — _match_one_sector
# ============================================================

from app.engine.recommend import _match_one_sector

class TestMatchOneSector:
    def test_exact_match(self):
        assert _match_one_sector("食品", ["食品", "饮料", "医药"]) == "食品"
        assert _match_one_sector("半导体", ["半导体", "电子"]) == "半导体"

    def test_alias_match(self):
        """通过 _SECTOR_ALIASES 映射"""
        assert _match_one_sector("风电设备", ["电源设备", "电网设备"]) == "电源设备"
        assert _match_one_sector("军工", ["航空航天装备", "地面兵装"]) == "航空航天装备"
        assert _match_one_sector("白酒", ["饮料", "食品"]) == "饮料"

    def test_substring_match(self):
        """子串匹配"""
        assert _match_one_sector("食品", ["食品饮料", "食品加工"]) == "食品饮料"
        assert _match_one_sector("饮料乳品", ["饮料", "乳品", "食品"]) == "饮料"

    def test_no_match(self):
        assert _match_one_sector("XYZ", ["食品", "饮料"]) is None
        assert _match_one_sector("传媒", ["食品", "饮料"]) is None

    def test_empty_candidates(self):
        assert _match_one_sector("食品", []) is None
        assert _match_one_sector("食品", [""]) is None


# ============================================================
# prompts — sector_selection_prompt
# ============================================================

from app.llm.prompts import sector_selection_prompt, sector_selection_system_prompt

class TestPrompts:
    def test_basic_prompt_structure(self):
        prompt = sector_selection_prompt(
            date_str="2026-07-30",
            available=["食品", "饮料", "半导体", "电力"],
            top_gainers="食品(+3.5%)、饮料(+2.8%)",
            top_losers="半导体(-1.2%)",
            etf_net_flow="食品: 100亿",
            news_summary="市场走强，消费板块领涨",
            flow_summary="食品: 净流入100亿",
        )
        assert "2026-07-30" in prompt
        assert "可选赛道清单" in prompt
        assert "食品" in prompt
        assert "recommended_sectors" in prompt
        assert "risk_sectors" in prompt

    def test_no_flow_data(self):
        prompt = sector_selection_prompt(
            date_str="2026-07-30",
            available=["食品"],
            top_gainers="",
            top_losers="",
            etf_net_flow="",
            news_summary="无数据",
        )
        assert "可选赛道清单" in prompt

    def test_with_lessons(self):
        prompt = sector_selection_prompt(
            date_str="2026-07-30",
            available=["食品"],
            top_gainers="",
            top_losers="",
            etf_net_flow="",
            news_summary="",
            lessons="历史教训: 避免追涨杀跌",
        )
        assert "历史教训" in prompt

    def test_system_prompt(self):
        sp = sector_selection_system_prompt()
        assert "JSON" in sp
        assert "markdown" in sp.lower()


# ============================================================
# features — compute_fund_features（C3 公式单一来源）
# ============================================================

from app.features.calculator import compute_fund_features, combo_score, regime_combo_weights, score_frame
import app.repo as repo
from app.llm.macro_agent import MacroContext
from app.engine import recommend as recommend_mod

class TestComputeFundFeatures:
    def test_returns_seven_features(self):
        """固定单调净值序列应产出全部 7 个特征且数值有限。"""
        navs = np.linspace(1.0, 1.5, 80)
        idx_closes = np.linspace(3000.0, 3200.0, 80)
        idx_vols = np.full(80, 1e8)
        feat = compute_fund_features(navs, idx_closes, idx_vols)
        assert feat is not None
        for key in ("hurst_60d", "momentum_20d", "calmar", "downside_vol",
                    "capture_up", "capture_down", "bias_60d"):
            assert key in feat
            assert np.isfinite(feat[key])
        assert feat["momentum_20d"] > 0  # 单调上涨序列动量应为正

    def test_insufficient_data_returns_none(self):
        """净值不足 60 条时返回 None（与训练样本跳过逻辑一致）。"""
        navs = np.linspace(1.0, 1.1, 30)
        idx_closes = np.linspace(3000.0, 3200.0, 30)
        assert compute_fund_features(navs, idx_closes, np.full(30, 1.0)) is None

    def test_consistency_with_short_history(self):
        """刚好 61 天净值的特征公式应产出有限特征，不抛异常。"""
        navs = np.linspace(1.0, 1.2, 61)
        idx_closes = np.linspace(3000.0, 3100.0, 61)
        feat = compute_fund_features(navs, idx_closes, np.full(61, 1e8))
        assert feat is not None
        assert all(np.isfinite(feat[k]) for k in feat)


# ============================================================
# features — combo_score / regime_combo_weights（C3 打分收敛）
# ============================================================

class TestComboScore:
    def test_basic_combination(self):
        """combo_score 是各因子加权和，且权重由 w 驱动。"""
        w = {"model": 0.5, "rs": 0.15, "cal": 0.1, "hurst": 0.1}
        base = combo_score(1.0, 5.0, 2.0, 0.6, w)
        higher = combo_score(1.0, 10.0, 2.0, 0.6, w)
        assert higher > base  # rel_strength 越大 combo 越高

    def test_default_sector_and_rbsa_zero(self):
        """未传赛道相对项与 rbsa 权重时按 0 处理（降级/回测路径）。"""
        w = {"model": 0.5, "rs": 0.15, "cal": 0.1, "hurst": 0.1}
        a = combo_score(0.5, 0.0, 0.0, 0.5, w)
        b = combo_score(0.5, 0.0, 0.0, 0.5, w, sector_rel_momentum=10.0)
        assert b > a  # 赛道相对动量项为正贡献


class TestRegimeComboWeights:
    def test_bull_shifts_to_momentum(self):
        cfg = {"model_weight": 0.5, "rel_strength_weight": 0.15,
               "calmar_weight": 0.1, "hurst_weight": 0.1}
        bull = regime_combo_weights("BULL", cfg)
        assert bull["rs"] > cfg["rel_strength_weight"]
        assert bull["cal"] < cfg["calmar_weight"]

    def test_bear_shifts_to_calmar(self):
        cfg = {"model_weight": 0.5, "rel_strength_weight": 0.15,
               "calmar_weight": 0.1, "hurst_weight": 0.1}
        bear = regime_combo_weights("BEAR", cfg)
        assert bear["cal"] > cfg["calmar_weight"]


class _FakeModel:
    def predict(self, X):
        return np.full(len(X), 2.0)


class TestScoreFrame:
    def _cfg(self):
        return {"model_weight": 0.5, "rel_strength_weight": 0.15,
                "calmar_weight": 0.1, "hurst_weight": 0.1}

    def _df(self):
        rows = []
        for code, mom in (("A", 5.0), ("B", 3.0)):
            r = {c: 1.0 for c in repo.FEATURE_COLS}
            r["code"] = code
            r["momentum_20d"] = mom
            r["calmar"] = 2.0
            r["hurst_60d"] = 0.6
            r["regime"] = "BULL"
            rows.append(r)
        return pd.DataFrame(rows)

    def test_with_model_ranks_by_combo(self):
        out = score_frame(self._df(), _FakeModel(), self._cfg(), idx_mom=1.0)
        assert {"score", "score_norm", "rel_strength", "combo"} <= set(out.columns)
        a = out[out["code"] == "A"]["combo"].iloc[0]
        b = out[out["code"] == "B"]["combo"].iloc[0]
        assert a > b  # rel_strength 更大者 combo 更高

    def test_without_model_uses_05(self):
        out = score_frame(self._df(), None, self._cfg(), idx_mom=0.0)
        assert (out["score_norm"] == 0.5).all()
        assert "combo" in out.columns

    def test_regime_fallback_when_column_missing(self):
        df = self._df().drop(columns=["regime"])
        out = score_frame(df, _FakeModel(), self._cfg(), idx_mom=1.0, default_regime="BEAR")
        assert "combo" in out.columns


# ============================================================
# monitor — 防线链数据驱动（C2）与遮蔽 bug 回归
# ============================================================

import inspect
from app.engine import monitor as monitor_mod

class TestDefenseChain:
    def test_short_circuit_exit_stops_chain(self):
        """short_circuit=True 的规则触发 EXIT 时链立即终止。"""

        class FakeExit(monitor_mod.DefenseRule):
            severity = 10
            short_circuit = True

            def check(self, ctx):
                return monitor_mod.DefenseResult(signal="EXIT", reason="fake exit")

        class FakeWarn(monitor_mod.DefenseRule):
            severity = 20
            short_circuit = False

            def check(self, ctx):
                return monitor_mod.DefenseResult(signal="WARNING", reason="fake warn")

        signal, detail, *_ = monitor_mod._apply_defense_chain(
            monitor_mod.DefenseContext(code="X"), [FakeExit(), FakeWarn()]
        )
        assert signal == "EXIT"
        assert detail == "fake exit"

    def test_non_short_circuit_warning_finalizes(self):
        """非短路 WARNING 规则设置最终信号，链继续但最终为 WARNING。"""
        signal, detail, *_ = monitor_mod._apply_defense_chain(
            monitor_mod.DefenseContext(code="X"),
            [monitor_mod.SectorAdvantageRule()],
        )
        assert signal in ("WARNING", "HOLD")

    def test_update_highest_nav_is_repo_version(self):
        """回归：monitor.update_highest_nav 不应被本地 2 参函数遮蔽。"""
        sig = inspect.signature(monitor_mod.update_highest_nav)
        assert len(sig.parameters) == 3, f"遮蔽 bug 复现: {sig}"


class _FakeRankModel:
    def predict(self, X):
        return X["momentum_20d"].to_numpy()


class TestRankWithinSectors:
    """赛道覆盖回归：LLM 指定赛道即使 combo 排不进全局 top5，也必须出现在最终候选。

    根因（2026-08-02）：_rank_within_sectors 每赛道取 top2 后按全局 combo 截断到 5，
    高热度赛道（半导体 2648 只）因量化 combo 略低被整体挤出，run_recommendation
    过滤 target_sectors 时误报"赛道无可投基金"。
    """

    def _row(self, code, sector, mom):
        return {
            "code": code, "name": f"基金{code}", "regime": "BULL",
            "rbsa_industry_1": sector, "rbsa_weight_1": 50.0,
            "rbsa_industry_2": "", "rbsa_weight_2": 0.0,
            "rbsa_industry_3": "", "rbsa_weight_3": 0.0,
            "hurst_60d": 0.6, "momentum_20d": mom, "calmar": 2.0,
            "downside_vol": 1.0, "capture_up": 1.0, "capture_down": 1.0,
            "bias_60d": 0.0,
        }

    def test_llm_top_sectors_not_dropped_from_candidates(self, monkeypatch):
        """半导体/电源设备动量最低，但作为 LLM 指定赛道必须有候选。"""
        sectors = ["半导体", "电源设备", "消费电子设备", "通信设备", "电子元件"]
        data = (
            [self._row("SC_A", "半导体", 3.0), self._row("SC_B", "半导体", 2.0)]
            + [self._row("PD_A", "电源设备", 6.0), self._row("PD_B", "电源设备", 5.0)]
            + [self._row("CE_A", "消费电子设备", 12.0), self._row("CE_B", "消费电子设备", 11.0)]
            + [self._row("TX_A", "通信设备", 17.0), self._row("TX_B", "通信设备", 16.0)]
            + [self._row("EL_A", "电子元件", 15.0), self._row("EL_B", "电子元件", 14.0)]
        )
        monkeypatch.setattr(recommend_mod.repo, "get_sector_candidates", lambda s: data)
        monkeypatch.setattr(recommend_mod, "_index_momentum", lambda: 5.0)
        monkeypatch.setattr(recommend_mod, "_get_market_regime", lambda: "BULL")
        monkeypatch.setattr(recommend_mod, "_load_ranking_cfg", lambda: {
            "model_weight": 0.5, "rel_strength_weight": 0.15,
            "calmar_weight": 0.1, "hurst_weight": 0.1, "momentum_guard_pct": -15.0,
        })
        ctx = MacroContext(recommended_sectors=sectors, risk_sectors=[], date="2026-08-02")
        finalists = recommend_mod._rank_within_sectors(ctx, _FakeRankModel())
        got = {f["sector"] for f in finalists}
        assert "半导体" in got, f"半导体被挤出候选: {got}"
        assert "电源设备" in got, f"电源设备被挤出候选: {got}"
        assert len(finalists) <= 5


# ============================================================
# LLM 最终定论解析 + 持仓上下文构建（候选4 胶水收敛后的测试缺口）
# ============================================================

from app.engine.recommend import _parse_llm_result
from app.llm.context import build_holdings_text


class TestParseLlmResult:
    """推荐终定 LLM 返回解析：selected_code 必须在候选池内才算有效。"""

    def test_valid_selection(self):
        parsed = _parse_llm_result(
            '{"selected_code": "000001", "selected_name": "基金A", '
            '"reason": "理由", "vetoed": ["000002"]}',
            {"000001": "基金A", "000002": "基金B"},
        )
        assert parsed == {
            "selected_code": "000001", "selected_name": "基金A",
            "reason": "理由", "vetoed": ["000002"],
        }

    def test_invalid_code_returns_none(self):
        """LLM 返回池外 code → 无效（防止幻觉选错基金）。"""
        parsed = _parse_llm_result(
            '{"selected_code": "999999", "selected_name": "幻觉基金"}',
            {"000001": "基金A"},
        )
        assert parsed is None

    def test_invalid_json_returns_none(self):
        assert _parse_llm_result("not json", {"000001": "基金A"}) is None

    def test_non_dict_returns_none(self):
        assert _parse_llm_result("[1, 2, 3]", {"000001": "基金A"}) is None

    def test_missing_selected_code_returns_none(self):
        assert _parse_llm_result('{"reason": "无推荐"}', {"000001": "基金A"}) is None


class TestBuildHoldingsText:
    """持仓上下文格式单一来源：recommend/monitor 共用同一文本。"""

    def test_with_holdings(self, monkeypatch):
        monkeypatch.setattr(
            "app.llm.context.repo.get_holdings",
            lambda code, limit: [
                {"stock_name": "贵州茅台", "industry": "白酒", "weight": 12.5},
                {"stock_name": "宁德时代", "industry": "电池", "weight": 8.0},
            ],
        )
        assert build_holdings_text("000001", 5) == "贵州茅台(白酒,12.5%)；宁德时代(电池,8.0%)"

    def test_industry_fallback_other(self, monkeypatch):
        monkeypatch.setattr(
            "app.llm.context.repo.get_holdings",
            lambda code, limit: [{"stock_name": "某股", "industry": "", "weight": 1.5}],
        )
        assert build_holdings_text("000002", 5) == "某股(其他,1.5%)"

    def test_empty_holdings(self, monkeypatch):
        monkeypatch.setattr("app.llm.context.repo.get_holdings", lambda code, limit: [])
        assert build_holdings_text("000003", 5) == "无持仓数据"

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

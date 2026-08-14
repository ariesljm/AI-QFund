"""赛道解析与锚定测试（app.domain.SectorPolicy）。

架构深化候选 1 回归：赛道名解析曾散落 recommend._resolve_sectors /
monitor.check_sector_anchor / macro_agent._suggest_quant 三处各自实现，
收敛为 SectorPolicy 后，解析口径（别名+匹配+去重）单一来源，三处消费方
只消费结果。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.domain import SectorPolicy, MIN_SECTOR_EXPOSURE

_AVAILABLE = ["食品", "饮料", "医药", "电源设备", "电网设备",
              "航空航天装备", "半导体", "石油天然气", "基本金属"]


class TestSectorPolicyResolve:
    def test_alias_mapping(self):
        """LLM 自由名经别名映射到 RBSA 行业名（domain.SECTOR_ALIASES）。"""
        p = SectorPolicy(_AVAILABLE)
        assert p.resolve(["风电设备"]) == ["电源设备"]
        assert p.resolve(["光伏"]) == ["电源设备"]
        assert p.resolve(["白酒"]) == ["饮料"]
        assert p.resolve(["军工"]) == ["航空航天装备"]

    def test_exact_and_substring_match(self):
        """精确匹配与子串匹配（含 ideal 包含 candidate 的方向）。"""
        p = SectorPolicy(_AVAILABLE)
        assert p.resolve(["食品"]) == ["食品"]
        assert p.resolve(["石油天然气"]) == ["石油天然气"]

    def test_unmatched_skipped(self):
        """无法匹配的名字跳过，不报错。"""
        p = SectorPolicy(_AVAILABLE)
        assert p.resolve(["不存在的赛道"]) == []

    def test_dedup_preserves_order(self):
        """重复输入去重且保留首次出现顺序。"""
        p = SectorPolicy(_AVAILABLE)
        assert p.resolve(["白酒", "半导体", "白酒"]) == ["饮料", "半导体"]

    def test_resolve_set(self):
        """resolve_set 返回集合形态（锚定/回避成员判断用）。"""
        p = SectorPolicy(_AVAILABLE)
        assert p.resolve_set(["白酒", "军工"]) == {"饮料", "航空航天装备"}
        assert p.resolve_set([]) == set()

    def test_empty_available(self):
        """可用清单为空时任何名字都解析不出结果。"""
        p = SectorPolicy([])
        assert p.resolve(["食品"]) == []


class TestMinSectorExposure:
    def test_constant_single_source(self):
        """C4 纯度门槛常量单一来源（10.0，推荐引擎消费）。"""
        assert MIN_SECTOR_EXPOSURE == 10.0

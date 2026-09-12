"""监控防线测试：风格漂移 + 赛道锚点（真纯函数，ctx 预装配模式）。

架构深化候选 3：check_* 伪纯函数（直读 DB）已内联进 Rule.check，判定只消费
DefenseContext 预装配快照——测试直接构造 ctx 数据，无需 mock DB 函数。

回归场景（旧逻辑只比 rbsa_weight_1 差值，第一行业整体更换时差值≈0 不触发）：
买入时第一行业「半导体 37%」→ 当前第一行业「白酒 37%」→ 必须触发 EXIT。
"""

from app.engine import monitor as monitor_mod
from app.engine.monitor import DefenseContext, SectorAnchorRule, StyleDriftRule


def _ctx(code: str = "001428", cur_feat=None, entry_rbsa=None,
         anchor=None, available=None) -> DefenseContext:
    return DefenseContext(
        code=code, cur_feat=cur_feat or {}, entry_rbsa=entry_rbsa,
        anchor=anchor, available_sectors=available or ["半导体", "白酒", "电源设备"],
    )


def _stub_features(industry: str, weight: float) -> dict:
    return {"rbsa_industry_1": industry, "rbsa_weight_1": weight}


class TestStyleDriftDoubleCheck:
    def test_industry_switch_same_weight_triggers(self):
        """核心回归：第一行业整体更换（权重相同）→ 触发 EXIT（旧逻辑漏检）。"""
        result = StyleDriftRule().check(_ctx(
            cur_feat=_stub_features("白酒", 37.0), entry_rbsa=("半导体", 37.0)))
        assert result is not None
        assert result.signal == "EXIT"
        assert "半导体" in result.reason and "白酒" in result.reason

    def test_weight_drop_triggers(self):
        """原有行为保留：同一行业权重下降超阈值 → 触发。"""
        result = StyleDriftRule().check(_ctx(
            cur_feat=_stub_features("半导体", 30.0), entry_rbsa=("半导体", 50.0)))
        assert result is not None
        assert result.signal == "EXIT"
        assert "权重" in result.reason

    def test_weight_drop_below_threshold_holds(self):
        """权重下降低于阈值且行业未变 → 不触发。"""
        result = StyleDriftRule().check(_ctx(
            cur_feat=_stub_features("半导体", 40.0), entry_rbsa=("半导体", 50.0)))
        assert result is None

    def test_missing_entry_skips(self):
        """买入基准完全缺失 → 跳过，不误报。"""
        result = StyleDriftRule().check(_ctx(
            cur_feat=_stub_features("半导体", 30.0), entry_rbsa=(None, None)))
        assert result is None

    def test_missing_current_features_skips(self):
        """当前特征缺失 → 跳过。"""
        result = StyleDriftRule().check(_ctx(cur_feat={}, entry_rbsa=("半导体", 50.0)))
        assert result is None

    def test_industry_switch_without_weight_still_triggers(self):
        """当前权重缺失（rbsa_weight_1=None）但行业已切换 → 仍触发 EXIT。

        修复：旧逻辑先判 cur_w None 早退，行业切换被漏检。
        """
        result = StyleDriftRule().check(_ctx(
            cur_feat={"rbsa_industry_1": "白酒"},  # 无权重
            entry_rbsa=("半导体", 37.0)))
        assert result is not None
        assert result.signal == "EXIT"
        assert "半导体" in result.reason and "白酒" in result.reason

    def test_weight_missing_and_same_industry_holds(self):
        """权重缺失且行业未变 → 不误报（无依据判定权重下降）。"""
        result = StyleDriftRule().check(_ctx(
            cur_feat={"rbsa_industry_1": "半导体"},
            entry_rbsa=("半导体", 50.0)))
        assert result is None


class TestSectorAnchorRule:
    """R3a 赛道锚点：命中回避赛道 / 离开推荐赛道 → WARNING（ctx 预装配）。"""

    def test_hits_risk_sector_warns(self):
        """当前第一行业命中推荐时规避赛道 → WARNING（锚点名经别名解析，cur_ind 为 RBSA 名）。"""
        ctx = _ctx(cur_feat=_stub_features("饮料", 30.0),
                   anchor=(["食品"], ["白酒"], "reason"),
                   available=["食品", "饮料", "医药"])
        result = SectorAnchorRule().check(ctx)
        assert result is not None
        assert result.signal == "WARNING"
        assert "规避赛道" in result.reason

    def test_left_recommended_sector_warns(self):
        """当前第一行业已离开推荐赛道 → WARNING。"""
        ctx = _ctx(cur_feat=_stub_features("白酒", 30.0),
                   anchor=(["半导体"], [], "reason"))
        result = SectorAnchorRule().check(ctx)
        assert result is not None
        assert result.signal == "WARNING"
        assert "离开推荐赛道" in result.reason

    def test_still_in_recommended_holds(self):
        """当前第一行业仍在推荐赛道 → 不触发。"""
        ctx = _ctx(cur_feat=_stub_features("半导体", 30.0),
                   anchor=(["半导体"], [], "reason"))
        result = SectorAnchorRule().check(ctx)
        assert result is None

    def test_no_anchor_skips(self):
        """无锚点记录 → 不触发。"""
        ctx = _ctx(cur_feat=_stub_features("白酒", 30.0), anchor=None)
        result = SectorAnchorRule().check(ctx)
        assert result is None

    def test_alias_resolution(self):
        """锚点赛道名为 LLM 自由名（如"白酒"）→ 经别名解析后命中（避免误报）。"""
        # 推荐赛道"白酒"→"饮料"，当前第一行业"饮料"仍在推荐内 → 不触发
        ctx = _ctx(cur_feat=_stub_features("饮料", 30.0),
                   anchor=(["白酒"], [], "reason"),
                   available=["食品", "饮料", "医药"])
        result = SectorAnchorRule().check(ctx)
        assert result is None


class TestEntryRbsaFallback:
    """买入基准三级回退（装配层 _entry_rbsa，snap 由装配层预取传入）。"""

    def test_snapshot_priority(self):
        """feature_snapshot 持久化的 RBSA 优先于日快照。"""
        ind, w = monitor_mod._entry_rbsa(
            "x", "2026-06-01", {"rbsa_industry_1": "半导体", "rbsa_weight_1": 45.0})
        assert ind == "半导体" and w == 45.0

    def test_fallback_to_daily_snapshot(self, monkeypatch):
        """快照无 RBSA → 回退买入日快照。"""
        monkeypatch.setattr(monitor_mod, "get_rbsa_at_date", lambda code, d: ("半导体", 40.0))
        ind, w = monitor_mod._entry_rbsa("x", "2026-06-01", None)
        assert ind == "半导体" and w == 40.0

    def test_fallback_to_first_nonempty(self, monkeypatch):
        """买入日空窗（RBSA 为空）→ 用买入后首个非空快照兜底。"""
        monkeypatch.setattr(monitor_mod, "get_rbsa_at_date", lambda code, d: None)
        monkeypatch.setattr(monitor_mod, "get_first_rbsa_after", lambda code, d: ("半导体", 35.0))
        ind, w = monitor_mod._entry_rbsa("x", "2026-06-01", None)
        assert ind == "半导体" and w == 35.0

    def test_all_missing(self, monkeypatch):
        """全部缺失 → (None, None)。"""
        monkeypatch.setattr(monitor_mod, "get_rbsa_at_date", lambda code, d: None)
        monkeypatch.setattr(monitor_mod, "get_first_rbsa_after", lambda code, d: None)
        ind, w = monitor_mod._entry_rbsa("x", None, None)
        assert ind is None and w is None


class TestSameIndustryNormalization:
    """跨体系命名归一（季报 EM2016 名 vs 板块名）："煤炭"与"煤炭开采"是同行业，不算切换。"""

    def test_substring_relation_is_same(self):
        assert monitor_mod._same_industry("煤炭", "煤炭开采") is True
        assert monitor_mod._same_industry("煤炭开采", "煤炭") is True
        assert monitor_mod._same_industry("半导体", "半导体设备") is True

    def test_different_industries_are_not_same(self):
        assert monitor_mod._same_industry("石油天然气", "煤炭开采") is False
        assert monitor_mod._same_industry("半导体", "白酒") is False

    def test_empty_is_not_same(self):
        assert monitor_mod._same_industry("", "煤炭") is False
        assert monitor_mod._same_industry("煤炭", "") is False

    def test_style_drift_uses_normalization(self):
        """同义行业（煤炭→煤炭开采）不触发切换；不同行业才触发。"""
        same = StyleDriftRule().check(_ctx(
            cur_feat=_stub_features("煤炭", 37.0), entry_rbsa=("煤炭开采", 37.0)))
        assert same is None  # 同义，不算切换

        diff = StyleDriftRule().check(_ctx(
            cur_feat=_stub_features("白酒", 37.0), entry_rbsa=("半导体", 37.0)))
        assert diff is not None and diff.signal == "EXIT"

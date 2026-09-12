"""ticket 06/07 测试：日频风格反推接入 R2/R3 + 新增 R1.5 相对超额止损。

06：日频 style track 优先于季度 RBSA（缺失时回退，不破坏旧测试）；
07：R1.5 相对超额止损（未破 EMA60 也可因相对跑输触发）。
"""

from app.engine.monitor import (
    DefenseContext,
    EmaTrendRule,
    RelativeAlphaRule,
    SectorAnchorRule,
    StyleDriftRule,
)


def _ctx(**kw) -> DefenseContext:
    base = dict(code="001428", cur_feat={},
                available_sectors=["半导体", "白酒", "电源设备"])
    base.update(kw)
    return DefenseContext(**base)


class TestDailyStyleTrackPreferred:
    def test_daily_drift_triggers_where_quarterly_would_not(self):
        """日频反推已漂移到白酒，而季度 RBSA 仍写半导体 → 只有日频口径触发。"""
        feat = {"rbsa_industry_1": "半导体", "rbsa_weight_1": 40.0}
        entry = ("半导体", 40.0)
        # 季度口径：行业与权重都没变 → 不触发
        assert StyleDriftRule().check(_ctx(cur_feat=feat, entry_rbsa=entry)) is None
        # 日频口径：主行业已变 → EXIT
        ctx = _ctx(cur_feat=feat, entry_rbsa=entry,
                   style_track={"industry_1": "白酒", "weight_1": 38.0})
        r = StyleDriftRule().check(ctx)
        assert r is not None and r.signal == "EXIT"
        assert "白酒" in r.reason

    def test_daily_weight_drop_triggers(self):
        """日频权重下降超阈值 → 触发（季度权重未动，不触发）。"""
        feat = {"rbsa_industry_1": "半导体", "rbsa_weight_1": 40.0}
        entry = ("半导体", 40.0)
        assert StyleDriftRule().check(_ctx(cur_feat=feat, entry_rbsa=entry)) is None
        ctx = _ctx(cur_feat=feat, entry_rbsa=entry,
                   style_track={"industry_1": "半导体", "weight_1": 20.0})
        r = StyleDriftRule().check(ctx)
        assert r is not None and "权重" in r.reason

    def test_no_style_track_falls_back_to_quarterly(self):
        """无日频数据时回退季度 RBSA（expand-contract：旧行为不变）。"""
        r = StyleDriftRule().check(_ctx(
            cur_feat={"rbsa_industry_1": "白酒", "rbsa_weight_1": 37.0},
            entry_rbsa=("半导体", 37.0)))
        assert r is not None and r.signal == "EXIT"

    def test_sector_anchor_uses_quarterly_rbsa(self):
        """R3a 锚定**季报 RBSA 行业**（与推荐赛道同体系）：日频反推行业不影响 R3。

        反推（板块名）与推荐赛道（行业名）跨体系，日频化会让同体系基金被误判
        “离开赛道”（煤炭开采 vs 石油天然气同属能源却判离开）；R2 才用日频反推。
        """
        feat = {"rbsa_industry_1": "半导体"}
        anchor = (["半导体"], ["白酒"], "")
        assert SectorAnchorRule().check(_ctx(cur_feat=feat, anchor=anchor)) is None
        # 即使日频反推行业不同，R3 只看季报 RBSA（半导体 ∈ 推荐赛道 → 不告警）
        ctx = _ctx(cur_feat=feat, anchor=anchor,
                   style_track={"industry_1": "煤炭开采", "weight_1": 50.0})
        assert SectorAnchorRule().check(ctx) is None
        # 季报 RBSA 命中规避赛道 → 仍告警（与推荐赛道同体系）
        ctx2 = _ctx(cur_feat={"rbsa_industry_1": "白酒"}, anchor=anchor)
        r = SectorAnchorRule().check(ctx2)
        assert r is not None and r.signal == "WARNING"


class TestRelativeAlphaRule:
    def _ctx_with(self, navs, sec, industry="半导体"):
        return _ctx(navs=navs, sector_returns=sec,
                    style_track={"industry_1": industry, "weight_1": 50.0})

    def test_triggers_on_persistent_underperformance(self):
        """连续 3 日累计跑输 >4% → EXIT。"""
        navs = [1.0, 0.98, 0.9604, 0.941192]      # 每日约 -2%
        sec = [0.0, 0.0, 0.0]                     # 板块持平（与 fund_rets 等长）
        r = RelativeAlphaRule().check(self._ctx_with(navs, sec))
        assert r is not None and r.signal == "EXIT"
        assert "半导体" in r.reason
        assert "跑输" in r.reason

    def test_no_trigger_below_threshold(self):
        """累计跑输 1.5% < 4% → 不触发。"""
        navs = [1.0, 0.995, 0.990, 0.985]
        sec = [0.0, 0.0, 0.0]
        assert RelativeAlphaRule().check(self._ctx_with(navs, sec)) is None

    def test_outperformance_holds(self):
        """基金跑赢板块（板块跌更多）→ 不触发。"""
        navs = [1.0, 1.0, 1.0, 1.0]
        sec = [-0.05, -0.05, -0.05]
        assert RelativeAlphaRule().check(self._ctx_with(navs, sec)) is None

    def test_missing_sector_returns_skips(self):
        ctx = _ctx(navs=[1.0, 0.98, 0.96, 0.94],
                   style_track={"industry_1": "半导体"})
        assert RelativeAlphaRule().check(ctx) is None

    def test_missing_industry_skips(self):
        ctx = _ctx(navs=[1.0, 0.98, 0.96, 0.94], sector_returns=[0.0, 0.0, 0.0])
        assert RelativeAlphaRule().check(ctx) is None

    def test_none_days_filtered(self):
        """缺日（None）被过滤，不参与累计。"""
        navs = [1.0, 0.98, 0.9604, 0.941192]
        sec = [None, 0.0, None]
        # 仅 1 个有效样本 < 3 → 跳过
        assert RelativeAlphaRule().check(self._ctx_with(navs, sec)) is None

    def test_priority_between_r1_and_r2(self):
        """信号优先级：R1(15) < R1.5(17) < R2(20)。"""
        assert (EmaTrendRule.severity
                < RelativeAlphaRule.severity
                < StyleDriftRule.severity)

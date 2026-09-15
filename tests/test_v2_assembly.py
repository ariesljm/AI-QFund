"""票 11 前置：2.0 估值特征装配（PIT 持仓 + PE 历史 → 加权分位）。

种子数据验证：PIT 过滤（未来披露的持仓不参与）、PE 历史窗口、加权归一化、
无数据退化。回填完成后同函数直接消费真实数据。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.database as db_mod
from app.data.store import save_holdings_batch, save_stock_valuation
from app.features.v2_assembly import valuation_features


def _seed(monkeypatch, tmp_path):
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "v2.db")
    # 持仓：两期（2023-12-31 公告 2024-01-19 / 2024-03-31 公告 2024-04-19）
    with db_mod.db_conn() as conn:
        save_holdings_batch(conn, [("F001", "2023-12-31", "S1", "老仓", 6.0)])
        save_holdings_batch(conn, [("F001", "2024-03-31", "S2", "未来仓", 9.0)])
    # 估值：S1 有 3 年 PE 历史（当前=最低 → 0 分位）；S2 无估值
    save_stock_valuation("S1", [(f"2021-{i:03d}", 10.0 + i * 0.1, None, None)
                                for i in range(600)] + [("2024-001", 10.0, None, None)])
    # S2 的估值故意缺：未来持仓即使可见也无估值 → 不参与


class TestValuationFeatures:
    def test_pit_filters_future_holdings(self, monkeypatch, tmp_path):
        """决策日 2024-04-10：未来期（04-19 公告）不可见 → 只 S1 → 分位 0（当前=最低）。"""
        _seed(monkeypatch, tmp_path)
        f = valuation_features("F001", "2024-04-10")
        assert f["weighted_pe_pctile"] == 0.0   # S1 当前 10.0 = 历史最低

    def test_after_disclosure_new_period_visible_but_no_pe(self, monkeypatch, tmp_path):
        """公告日后新期可见，但新期持仓（S2）无估值 → None（不产出伪分位）。"""
        _seed(monkeypatch, tmp_path)
        f = valuation_features("F001", "2024-05-01")
        assert f["weighted_pe_pctile"] is None   # 可见期 = 2024-03-31（S2），无估值

    def test_no_holdings_returns_none(self, monkeypatch, tmp_path):
        _seed(monkeypatch, tmp_path)
        f = valuation_features("F999", "2024-04-10")
        assert f["weighted_pe_pctile"] is None

    def test_no_pe_returns_none(self, monkeypatch, tmp_path):
        """有持仓但全部无 PE 数据 → None（不产出伪分位）。"""
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "v2b.db")
        with db_mod.db_conn() as conn:
            save_holdings_batch(conn, [("F002", "2023-12-31", "SX", "无估值", 5.0)])
        f = valuation_features("F002", "2024-04-10")
        assert f["weighted_pe_pctile"] is None

"""票 11：模块一筛选管线接线（种子基座测试——主测试形态）。

种子基座数据夹具 + 注入打分 → 断言：主动权益池筛选、硬过滤生效、
Top30 落库 + 特征快照、空推荐日语义（no_opportunity vs data_failure）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


import app.database as db_mod
from app.engine.screen_pipeline import is_data_failure, is_no_opportunity, screen_top30
from app.repo import decision as decision_repo
from app.repo.base import db_conn


def _seed(monkeypatch, tmp_path):
    """种子基座：主动权益（混合/股票）、指数、暂停申购基金、短历史基金 + 特征。"""
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "screen.db")
    with db_conn() as conn:
        conn.executemany("INSERT INTO fund_basic (code, name, type, is_buyable) VALUES (?,?,?,1)",
                         [("F1", "混合一", "混合型"), ("F2", "股票二", "股票型"),
                          ("F3", "指数三", "指数型"), ("F4", "混合四", "混合型")])
        # F4 暂停申购（应被硬过滤剔除）
        conn.execute("INSERT INTO purchase_restrictions (code, status) VALUES ('F4','suspended')")
        # F5 短历史（nav 不足 62 条，应被剔除）；F5 不在买池也测一下
        conn.execute("INSERT INTO fund_basic (code, name, type, is_buyable) VALUES ('F5','混合五','混合型',1)")
        # 净值：F1/F2/F3 足够多，F5 仅 10 条
        for code in ("F1", "F2", "F3"):
            for i in range(100):
                conn.execute("INSERT INTO fund_nav (code, date, unit_nav, cum_nav) "
                             "VALUES (?,?,?,?)",
                             (code, f"2024-{(i//30)+1:02d}-{(i%30)+1:02d}", 1.0, 1.0))
        for i in range(10):
            conn.execute("INSERT INTO fund_nav (code, date, unit_nav, cum_nav) "
                         "VALUES ('F5',?,?,?)", (f"2024-01-{i+1:02d}", 1.0, 1.0))
        # 特征：F1/F2 有（打分基础），F3 无特征（打分缺失）
        for code in ("F1", "F2"):
            conn.execute(
                "INSERT INTO fund_features (code, date, hurst_60d, momentum_20d, calmar, "
                "downside_vol, capture_up, capture_down, drawdown_60d, reversal_20d, "
                "mom_5d, mom_60d, vol_20d, rbsa_industry_1, rbsa_weight_1) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (code, "2026-09-14", 0.5, 0.1, 1.0, 0.1, 1.0, 1.0, 0.2, 0.0,
                 0.05, 0.2, 0.1, "白酒", 40.0))
        conn.commit()


def _scorer(feat):
    """注入打分：按代码返回可区分分数。"""
    # features 里没有 code；用特征值凑合：momentum_20d 0.1 → 0.8
    return 0.8


class TestScreenPipeline:
    def test_seed_pool_filters_and_saves(self, monkeypatch, tmp_path):
        """种子基座端到端：指数剔除、暂停申购剔除、Top30 落库 + 快照。"""
        _seed(monkeypatch, tmp_path)
        result = screen_top30("2026-09-14", scorer=_scorer)
        # 买池 F1/F2/F4/F5（F3 指数剔除）→ F4 暂停剔除 → F5 短历史剔除 → F1/F2
        # 但 F2 特征日期与 F1 相同 0.1 → 都 0.8 分
        assert result["count"] == 2
        assert set(result["codes"]) == {"F1", "F2"}
        saved = decision_repo.get_screen_candidates("2026-09-14")
        assert [s["code"] for s in saved] == ["F1", "F2"]
        assert saved[0]["features"].get("momentum_20d") == 0.1   # 特征快照保留

    def test_no_scorable_data_failure(self, monkeypatch, tmp_path):
        """有池但打分全无 → data_failure（区别于 no_opportunity）。"""
        _seed(monkeypatch, tmp_path)

        def none_scorer(feat):
            return None
        result = screen_top30("2026-09-14", scorer=none_scorer)
        assert is_data_failure(result) and not is_no_opportunity(result)

    def test_no_equity_pool_no_opportunity(self, monkeypatch, tmp_path):
        """买池空（全是指数）→ no_opportunity（市场判断语义）。"""
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "empty.db")
        with db_conn() as conn:
            conn.execute("INSERT INTO fund_basic (code, name, type, is_buyable) "
                         "VALUES ('I1','指数一','指数型',1)")
            conn.commit()
        result = screen_top30("2026-09-14", scorer=_scorer)
        assert is_no_opportunity(result) and not is_data_failure(result)

    def test_persistence_idempotent(self, monkeypatch, tmp_path):
        """同日重复跑 → REPLACE 幂等，不产生重复行。"""
        _seed(monkeypatch, tmp_path)
        screen_top30("2026-09-14", scorer=_scorer)
        screen_top30("2026-09-14", scorer=_scorer)
        with db_conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM screen_candidates "
                             "WHERE date='2026-09-14'").fetchone()[0]
        assert n == 2   # F1/F2 各一行，非 4

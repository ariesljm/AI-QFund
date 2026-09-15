"""票 10：Purged Time-Series Split（防作弊）与泄漏测试。

purged_folds 纯函数：训练/验证隔离滚动划分，60 交易日隔离带。
泄漏测试：PIT 读端（票 04）拒绝"d 之后披露的持仓"——人为注入泄漏必须被抓。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.database as db_mod
from app.data.store import save_holdings_batch
from app.engine.walk_forward import purged_folds
from app.repo.base import get_holdings_summaries


def _days(n):
    return [f"2024-{(i // 30) + 1:02d}-{(i % 30) + 1:02d}" for i in range(n)]


class TestPurgedFolds:
    def test_fold_count(self):
        """折叠数 = floor((n − train − purge − valid)/valid) + 1（推进步数符合预期）。"""
        dates = _days(100)
        folds = purged_folds(dates, train_days=20, valid_days=5, purge_days=3)
        expected = (100 - 20 - 3 - 5) // 5 + 1
        assert len(folds) == expected == 15

    def test_purge_gap_invariant(self):
        """∀ fold：valid 首日与 train 末日索引差 > purge_days（隔离带成立）。"""
        dates = _days(200)
        folds = purged_folds(dates, train_days=50, valid_days=10, purge_days=7)
        for train, valid in folds:
            ti = dates.index(train[-1])
            vi = dates.index(valid[0])
            assert vi - ti > 7

    def test_train_and_valid_disjoint_per_fold(self):
        """同一 fold 内 train 与 valid 无共享日期。"""
        dates = _days(120)
        for train, valid in purged_folds(dates, train_days=30, valid_days=10, purge_days=5):
            assert set(train).isdisjoint(valid)

    def test_insufficient_data_no_fold(self):
        assert purged_folds(_days(10), train_days=20, valid_days=5, purge_days=3) == []

    def test_defaults_sane(self):
        """默认：训练 2 年 / 验证 1 季 / 隔离 60 交易日（票 10 验收口径）。"""
        dates = _days(1500)
        folds = purged_folds(dates)
        assert folds
        for train, valid in folds:
            assert len(train) == 480 and len(valid) == 60


class TestPitLeakage:
    """人为注入泄漏 → 断言报警：PIT 读端必须拒绝未来披露的持仓。"""

    def test_leaked_newer_period_is_caught(self, monkeypatch, tmp_path):
        """决策日 2024-04-10 时，04-19 才公告的持仓被注入 → 读端必须看不见。"""
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "leak.db")
        # 注入"泄漏"：两期持仓，其中最新期（2024-03-31）公告日 04-19 晚于决策日
        with db_mod.db_conn() as conn:
            save_holdings_batch(conn, [("F001", "2023-12-31", "S1", "老仓", 6.0)])
            save_holdings_batch(conn, [("F001", "2024-03-31", "S2", "未来仓", 9.0)])
        # 泄漏注入后的裸读（无 as_of）会看到最新期——这是"未防护"的基线
        leaked = get_holdings_summaries(["F001"])["F001"]["report_date"]
        assert leaked == "2024-03-31"          # 泄漏确实存在（测试前提成立）
        # PIT 读端（as_of）必须报警：04-10 决策看不到 04-19 公告的持仓
        pit = get_holdings_summaries(["F001"], as_of="2024-04-10")["F001"]["report_date"]
        assert pit == "2023-12-31"             # 断言抓到泄漏

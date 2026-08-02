"""T3 推荐质量度量测试。

核心：IC（预测分 vs 未来20日实际超额收益秩相关）、20日超额胜率、累计超额。
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.engine import quality
import app.database as db_mod
from app import repo


class TestComputeMetricsFromPairs:
    """纯函数：metrics 计算。"""

    def test_perfect_monotonic_ic_one(self):
        pairs = [(1, -0.1), (2, -0.05), (3, 0.0), (4, 0.2)]
        m = quality.compute_metrics_from_pairs(pairs)
        assert m["ic"] == 1.0
        assert m["excess_win_rate"] == 0.25
        assert m["sample_count"] == 4

    def test_ties_average_rank(self):
        pairs = [(1, 0.1), (1, 0.2), (2, 0.3), (3, 0.4)]
        m = quality.compute_metrics_from_pairs(pairs)
        assert m["ic"] == pytest.approx(0.9487, abs=1e-3)

    def test_all_positive_win_rate_one(self):
        pairs = [(0.5, 0.01), (0.6, 0.05), (0.7, 0.08)]
        m = quality.compute_metrics_from_pairs(pairs)
        assert m["excess_win_rate"] == 1.0

    def test_empty(self):
        m = quality.compute_metrics_from_pairs([])
        assert m["ic"] is None
        assert m["sample_count"] == 0

    def test_single_sample_ic_none(self):
        m = quality.compute_metrics_from_pairs([(0.5, 0.02)])
        assert m["ic"] is None
        assert m["excess_win_rate"] == 1.0

    def test_constant_alpha_ic_none(self):
        """常数序列无秩相关 → IC 为 None，而非 NaN 入库。"""
        pairs = [(0.5, 0.01), (0.6, 0.01), (0.7, 0.01)]
        m = quality.compute_metrics_from_pairs(pairs)
        assert m["ic"] is None


class TestComputeQualityMetricsDB:
    """DB 集成：从推荐记录 + 净值 + 指数算 20 日实际超额。"""

    @staticmethod
    def _seed(conn):
        # 000001 涨 10%，000002 平，沪深300 涨 ~3.33%
        for i in range(21):
            d = f"2026-01-{5 + i:02d}"
            conn.execute("INSERT INTO fund_nav (code, date, cum_nav) VALUES ('000001', ?, ?)",
                         (d, 1.0 + i * (0.1 / 20)))
            conn.execute("INSERT INTO fund_nav (code, date, cum_nav) VALUES ('000002', ?, ?)",
                         (d, 1.0))
            conn.execute("INSERT INTO index_daily (code, date, close) VALUES ('sh000300', ?, ?)",
                         (d, 3000.0 + i * (100.0 / 20)))
        conn.execute(
            "INSERT INTO recommend_log (recommend_date, code, name, score, status) "
            "VALUES ('2026-01-05', '000001', 'A', 0.9, 'HOLD')"
        )
        conn.execute(
            "INSERT INTO recommend_log (recommend_date, code, name, score, status) "
            "VALUES ('2026-01-05', '000002', 'B', 0.1, 'HOLD')"
        )

    def test_realized_alpha_and_metrics(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "test.db")
        with db_mod.db_conn() as conn:
            self._seed(conn)
        m = quality.compute_quality_metrics("2026-01-01", "2026-01-31")
        assert m["sample_count"] == 2
        assert m["ic"] == 1.0          # 高预测分基金实现更高超额（预测有效）
        assert m["excess_win_rate"] == 0.5
        # 累计超额曲线点（按 code 排序：000001 先）
        assert len(m["points"]) == 2
        assert m["points"][0]["cum_alpha"] == pytest.approx(0.0667, abs=1e-3)
        assert m["points"][-1]["cum_alpha"] == pytest.approx(0.0333, abs=1e-3)

    def test_save_and_load(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "test.db")
        repo.save_quality_metrics({
            "computed_date": "2026-02-01", "period_start": "2026-01-01",
            "period_end": "2026-01-31", "ic": 0.5, "excess_win_rate": 0.6,
            "mean_excess": 0.01, "cum_excess": 0.03, "sample_count": 10,
            "points": [{"date": "2026-01-05", "code": "000001", "alpha": 0.03,
                        "cum_alpha": 0.03}],
        })
        rows = repo.get_quality_metrics(3)
        assert len(rows) == 1
        assert rows[0]["ic"] == 0.5
        assert rows[0]["sample_count"] == 10
        assert rows[0]["points"][0]["cum_alpha"] == 0.03

    def test_save_idempotent_same_period(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "test.db")
        repo.save_quality_metrics({
            "computed_date": "2026-02-01", "period_start": "2026-01-01",
            "period_end": "2026-01-31", "ic": 0.5, "excess_win_rate": 0.6,
            "mean_excess": 0.01, "cum_excess": 0.03, "sample_count": 10,
        })
        repo.save_quality_metrics({
            "computed_date": "2026-02-02", "period_start": "2026-01-01",
            "period_end": "2026-01-31", "ic": 0.7, "excess_win_rate": 0.8,
            "mean_excess": 0.02, "cum_excess": 0.05, "sample_count": 12,
        })
        rows = repo.get_quality_metrics(3)
        assert len(rows) == 1
        assert rows[0]["ic"] == 0.7

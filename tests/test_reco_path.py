"""#1 降级路径打标 + 分口径度量测试。

覆盖：
- compute_quality_metrics 按 reco_path 分组输出 by_path 分口径赚钱率；
- points 每条带 reco_path 标记（回溯每条推荐走了哪条路径）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.repo as repo
from app.engine import quality


def test_quality_by_path_split(monkeypatch):
    """sector/degrade 两条路径分组度量：赚钱率分别统计。"""
    rows = [
        ("F1", "2026-06-01", 0.5, None, "sector"),
        ("F2", "2026-06-02", 0.4, None, "degrade"),
        ("F3", "2026-06-03", 0.6, None, "sector"),
        ("F4", "2026-06-04", 0.3, None, "degrade"),
    ]
    monkeypatch.setattr(repo, "get_quality_sample_rows", lambda a, b: rows)
    rets = {"F1": 0.03, "F2": -0.02, "F3": 0.05, "F4": -0.01}
    monkeypatch.setattr(repo.nav, "forward_return", lambda code, d: rets[code])

    m = quality.compute_quality_metrics("2026-06-01", "2026-06-30")

    assert m["by_path"]["sector"]["sample_count"] == 2
    assert m["by_path"]["degrade"]["sample_count"] == 2
    # sector 两条都 >1%（0.03/0.05）→ profit_rate 1.0；degrade 两条都 ≤1% → 0.0
    assert m["by_path"]["sector"]["profit_rate"] == 1.0
    assert m["by_path"]["degrade"]["profit_rate"] == 0.0
    # 全量口径不变（4 条中 2 条 >1%）
    assert m["profit_rate"] == 0.5


def test_quality_by_path_points_tagged(monkeypatch):
    """points 每条带 reco_path 标记，可回溯单条推荐的来源路径。"""
    rows = [
        ("F1", "2026-06-01", 0.5, None, "degrade"),
        ("F2", "2026-06-02", 0.4, None, "sector"),
    ]
    monkeypatch.setattr(repo, "get_quality_sample_rows", lambda a, b: rows)
    monkeypatch.setattr(repo.nav, "forward_return", lambda code, d: 0.02)

    m = quality.compute_quality_metrics("2026-06-01", "2026-06-30")

    assert m["points"][0]["reco_path"] == "degrade"
    assert m["points"][1]["reco_path"] == "sector"


def test_quality_by_path_defaults_to_sector(monkeypatch):
    """历史行无 reco_path（NULL）时按 sector 归桶，不丢样本。"""
    rows = [("F1", "2026-06-01", 0.5, None, None)]
    monkeypatch.setattr(repo, "get_quality_sample_rows", lambda a, b: rows)
    monkeypatch.setattr(repo.nav, "forward_return", lambda code, d: 0.02)

    m = quality.compute_quality_metrics("2026-06-01", "2026-06-30")

    assert "sector" in m["by_path"]
    assert m["by_path"]["sector"]["sample_count"] == 1

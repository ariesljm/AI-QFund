"""T10（reco-hardening）：限购/暂停申购检测验收测试。

背景：动量策略最爱的强势基金常限购——系统推荐了用户买不进去。
降级方案：purchase_restrictions 维护清单（东财接口无稳定字段时），
候选装配时标注，终选 prompt 素材可见（限购默认不选）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.llm.prompts import final_pick_prompt
from app.repo import decision as dec


class TestPurchaseRepo:
    def test_roundtrip_status(self, monkeypatch, tmp_path):
        import sqlite3

        import app.repo.base as base_mod

        db = tmp_path / "t10.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE purchase_restrictions (code TEXT PRIMARY KEY, "
                     "status TEXT NOT NULL, note TEXT, updated_at TEXT)")
        conn.commit()
        monkeypatch.setattr(base_mod, "db_conn", lambda: sqlite3.connect(db))

        assert dec.get_purchase_status("F1") is None  # 未知
        dec.save_purchase_restriction("F1", "limited", "暂停大额申购")
        assert dec.get_purchase_status("F1") == "limited"
        # 幂等更新
        dec.save_purchase_restriction("F1", "suspended")
        assert dec.get_purchase_status("F1") == "suspended"

    def test_prompt_marks_limited(self):
        """终选素材：限购候选带提示（默认不选）；normal 不带。"""
        from types import SimpleNamespace
        ctx = SimpleNamespace(sector_reasoning="x", recommended_sectors=["半导体"],
                              risk_sectors=[], regime_label="BEAR")
        base = {"code": "F1", "name": "n1", "sector": "半导体",
                "calmar": 1.0, "hurst_60d": 0.6, "combo": 0.7,
                "momentum_20d": 5.0, "sector_median_mom": 1.0, "mom_gap": 4.0,
                "holdings": [], "report_date": "2026-06-30", "holdings_months": 2}
        limited = dict(base, purchase_status="limited")
        normal = dict(base, purchase_status="normal")
        unknown = dict(base, purchase_status=None)

        p_limited = final_pick_prompt([limited], ctx, [])
        p_normal = final_pick_prompt([normal, unknown], ctx, [])
        assert "申购状态: limited" in p_limited
        assert "除非强理由否则不选" in p_limited
        assert "申购状态" not in p_normal  # normal/未知不标注


class TestPurchaseStatusesBatch:
    """T10 批量申购状态原语（候选 8 素材 N+1 收敛）：批量与单查语义一致。"""

    def test_batch_returns_all(self, monkeypatch, tmp_path):
        import sqlite3

        import app.repo.base as base_mod

        db = tmp_path / "pr.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE purchase_restrictions (code TEXT PRIMARY KEY, status TEXT)")
        conn.executemany("INSERT INTO purchase_restrictions VALUES (?, ?)",
                         [("F1", "normal"), ("F2", "limited"), ("F3", "suspended")])
        conn.commit()
        monkeypatch.setattr(base_mod, "db_conn", lambda: sqlite3.connect(db))
        monkeypatch.setattr(dec, "db_conn", lambda: sqlite3.connect(db))

        assert dec.get_purchase_statuses(["F1", "F2", "F3"]) == {
            "F1": "normal", "F2": "limited", "F3": "suspended"}
        # 未知 code 不在返回 dict 中（调用方按 unknown 处理）
        assert "F9" not in dec.get_purchase_statuses(["F9", "F1"])
        assert dec.get_purchase_statuses([]) == {}

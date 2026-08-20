"""F-4 装配层测试：_entry_rbsa 买入基准三级回退。

回归根因：防线判定纯函数有测，但 _entry_rbsa（snapshot → 当日 → 首个非空兜底）
零测试——三级回退的空窗兑底是 bug 易发区（返回值在装配层被解包，
任一级返回 None 都会传播到 DefenseContext.entry_rbsa）。
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.engine.monitor as mon


def test_level1_snapshot_hit_returns_directly(monkeypatch):
    """一级（feature_snapshot）有值 → 直接返回，不查 fund_features。"""
    called = []
    monkeypatch.setattr(mon, "get_rbsa_at_date", lambda *a: called.append("at_date") or None)
    monkeypatch.setattr(mon, "get_first_rbsa_after", lambda *a: called.append("after") or None)

    snap = {"rbsa_industry_1": "半导体", "rbsa_weight_1": 0.32}
    ind, w = mon._entry_rbsa("A", "2026-08-01", snap)
    assert ind == "半导体"
    assert w == 0.32
    assert called == []  # 一级命中，不进二/三级


def test_level1_blank_falls_to_level2(monkeypatch):
    """一级快照空（无 industry/weight）→ 走二级 get_rbsa_at_date。"""
    monkeypatch.setattr(mon, "get_rbsa_at_date", lambda c, d: ("通信设备", 0.41))
    monkeypatch.setattr(mon, "get_first_rbsa_after", lambda *a: None)

    ind, w = mon._entry_rbsa("A", "2026-08-01", {"rbsa_industry_1": "", "rbsa_weight_1": None})
    assert ind == "通信设备"
    assert w == 0.41


def test_level2_blank_falls_to_level3(monkeypatch):
    """二级（当日无快照）→ 三级 get_first_rbsa_after 兜底（报告期空窗兑底）。"""
    monkeypatch.setattr(mon, "get_rbsa_at_date", lambda c, d: (None, None))
    monkeypatch.setattr(mon, "get_first_rbsa_after", lambda c, d: ("电源设备", 0.18))

    ind, w = mon._entry_rbsa("A", "2026-08-01", {})
    assert ind == "电源设备"
    assert w == 0.18


def test_all_blank_returns_none_pair(monkeypatch):
    """三级全空 → (None, None)，装配层据此标记赛道失效。"""
    monkeypatch.setattr(mon, "get_rbsa_at_date", lambda c, d: (None, None))
    monkeypatch.setattr(mon, "get_first_rbsa_after", lambda c, d: (None, None))

    ind, w = mon._entry_rbsa("A", "2026-08-01", None)
    assert ind is None
    assert w is None


def test_no_reco_date_skips_db_lookup(monkeypatch):
    """reco_date 为 None（无推荐记录）→ 不查 fund_features，直接 (None, None)。"""
    called = []
    monkeypatch.setattr(mon, "get_rbsa_at_date", lambda *a: called.append("at_date") or None)
    monkeypatch.setattr(mon, "get_first_rbsa_after", lambda *a: called.append("after") or None)

    ind, w = mon._entry_rbsa("A", None, None)
    assert ind is None
    assert w is None
    assert called == []


def test_weight_coerced_to_float(monkeypatch):
    """weight 非浮点（DB 可能返回字符串/Decimal）→ 强制 float。"""
    monkeypatch.setattr(mon, "get_rbsa_at_date", lambda c, d: ("半导体", "0.55"))
    monkeypatch.setattr(mon, "get_first_rbsa_after", lambda *a: None)

    _, w = mon._entry_rbsa("A", "2026-08-01", {})
    assert w == 0.55
    assert isinstance(w, float)

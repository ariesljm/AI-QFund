"""票 05：个股估值 fetcher（东财 RPT_VALUEANALYSIS_DET）分页解析 + repo roundtrip。

fetch_stock_valuation 离线测（monkeypatch fetch）：分页遍历、字段映射、
日期截断、升序、空数据。save_stock_valuation + get_pe_histories 用临时库
验证窗口函数限长（各股票独立取最近 N 条，不被跨股票截断）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.data.valuation as val
import app.database as db_mod
from app.data.store import save_stock_valuation
from app.repo.base import get_pe_histories


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _rows(count, start_year=2021):
    return [
        {"SECURITY_CODE": "600519",
         "TRADE_DATE": f"{start_year + i // 365}-{(i % 365) // 30 + 1:02d}-{(i % 30) + 1:02d} 00:00:00",
         "PE_TTM": 20.0 + i * 0.01, "PB_MRQ": 5.0 + i * 0.001,
         "TOTAL_MARKET_CAP": 1_000_000_000 + i}
        for i in range(count)
    ]


class TestFetchStockValuation:
    def test_pagination_and_mapping(self, monkeypatch):
        """两页 600 条：分页遍历到底，字段映射 + 日期截断 + 升序。"""
        calls = []
        total = 600

        def fake_fetch(url, params):
            calls.append(params)
            page = int(params["pageNumber"])
            size = int(params["pageSize"])
            lo = (page - 1) * size
            hi = min(lo + size, total)
            data = _rows(total)[lo:hi] if lo < total else []
            return _FakeResp({"result": {"data": data, "count": total}})

        monkeypatch.setattr(val, "fetch", fake_fetch)
        rows = val.fetch_stock_valuation("600519")

        assert len(rows) == total
        assert len(calls) == 2                      # 500 + 100 → 两页
        assert all(len(r[0]) == 10 for r in rows)   # 日期截断为 YYYY-MM-DD
        assert rows[0][0] == "2021-01-01"
        assert [r[0] for r in rows] == sorted(r[0] for r in rows)  # 升序
        assert rows[0][1] == 20.0 and rows[0][2] == 5.0            # PE/PB 映射

    def test_empty_data(self, monkeypatch):
        monkeypatch.setattr(val, "fetch",
                            lambda url, params: _FakeResp({"result": {"data": [], "count": 0}}))
        assert val.fetch_stock_valuation("000000") == []

    def test_null_result(self, monkeypatch):
        monkeypatch.setattr(val, "fetch",
                            lambda url, params: _FakeResp({"result": None}))
        assert val.fetch_stock_valuation("000000") == []


class TestValuationRepo:
    def _seed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "val.db")
        # 两只股票：A 有 800 条（超 750 默认窗口），B 只有 3 条
        save_stock_valuation("A", [(f"2024-{i:03d}", 10.0 + i * 0.1, None, None) for i in range(800)])
        save_stock_valuation("B", [("2024-001", 1.0, None, None),
                                   ("2024-002", 2.0, None, None),
                                   ("2024-003", 3.0, None, None)])

    def test_window_limit_per_stock(self, monkeypatch, tmp_path):
        """各股票独立限长：A 取最近 750 条，B 只有 3 条不被 A 挤掉。"""
        self._seed(monkeypatch, tmp_path)
        got = get_pe_histories(["A", "B"])
        assert len(got["A"]) == 750
        assert len(got["B"]) == 3
        assert got["A"][-1] > got["A"][0]     # 升序（末位=最新）
        assert got["B"] == [1.0, 2.0, 3.0]

    def test_missing_stock_returns_empty(self, monkeypatch, tmp_path):
        self._seed(monkeypatch, tmp_path)
        got = get_pe_histories(["NOPE"])
        assert got == {"NOPE": []}

"""ticket 02 板块历史回填（成分股市值加权合成）测试。

覆盖：腾讯符号前缀、市值加权合成口径、首日无收益、缓存复用、
批量入库不覆盖已有 net_flow（实时快照与历史合成共存）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.repo as repo
from app.data import sector_history as sh
from app.database import db_conn


class TestTxSymbol:
    def test_sohu_prefix(self):
        # 数据源改为搜狐后，符号统一为 cn_ + 6 位代码（沪/深/创业/科创同构）
        assert sh.tx_symbol("600519") == "cn_600519"
        assert sh.tx_symbol("688981") == "cn_688981"
        assert sh.tx_symbol("000001") == "cn_000001"
        assert sh.tx_symbol("300750") == "cn_300750"
        assert sh.tx_symbol("430047") == "cn_430047"


class TestSynthesizeBoard:
    def test_market_cap_weighted(self, monkeypatch):
        monkeypatch.setattr(sh, "board_top_members",
                            lambda code, k=15: [("600519", 3.0), ("000001", 1.0)])
        daily = {
            "600519": {"2026-01-01": 100.0, "2026-01-02": 110.0},  # +10%
            "000001": {"2026-01-01": 100.0, "2026-01-02": 90.0},   # -10%
        }
        monkeypatch.setattr(sh, "stock_daily", lambda code, days=600: daily[code])

        s = sh.synthesize_board("BK0001")

        # 权重 3/4 × 10% + 1/4 × (-10%) = 7.5% - 2.5% = 5.0%
        assert abs(s["2026-01-02"] - 5.0) < 1e-9
        assert "2026-01-01" not in s  # 首日无前收，不计收益

    def test_empty_members_returns_empty(self, monkeypatch):
        monkeypatch.setattr(sh, "board_top_members", lambda code, k=15: [])
        assert sh.synthesize_board("BK0001") == {}

    def test_stock_daily_cached_across_boards(self, monkeypatch):
        """同一只股票出现在两个板块时只请求一次（跨板块缓存复用）。"""
        calls: list[str] = []

        def fake_daily(code, days=600):
            calls.append(code)
            return {"2026-01-01": 100.0, "2026-01-02": 101.0}

        monkeypatch.setattr(sh, "board_top_members",
                            lambda code, k=15: [("600519", 1.0)])
        monkeypatch.setattr(sh, "stock_daily", fake_daily)

        cache: dict = {}
        sh.synthesize_board("BK0001", cache=cache)
        sh.synthesize_board("BK0002", cache=cache)

        assert calls == ["600519"]  # 第二次命中缓存


class TestSaveSectorHistoryBatch:
    def test_insert_and_preserve_existing_net_flow(self, monkeypatch):
        """历史合成为 pct_chg 补数，不覆盖实时快照已有的 net_flow。"""
        code, name, date = "BK9999", "测试板块", "2026-08-20"
        # 先在（同一 date, sector_code）写入带 net_flow 的实时快照
        repo.save_sector_snapshot(date, [{"c": code, "n": name, "u": 1.0, "zjl": 12345.0}])
        # 再用历史合成批量写同一天
        n = repo.save_sector_history_batch([(date, code, name, 2.5)])

        assert n == 1
        with db_conn() as c:
            row = c.execute(
                "SELECT pct_chg, net_flow FROM sector_daily_snapshot "
                "WHERE date = ? AND sector_code = ?", (date, code)).fetchone()
        assert row[0] == 2.5          # pct_chg 被合成分覆盖
        assert row[1] == 12345.0      # net_flow 保留（历史无源不回写）

    def test_empty_rows_noop(self):
        assert repo.save_sector_history_batch([]) == 0

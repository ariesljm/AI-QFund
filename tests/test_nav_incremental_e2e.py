"""批量快照在“真的返回结果”时的端到端回归（ticket 26）。

**这个用例是在生产上被打出来的**：2026-09-15 把会返回结果的快路径（rankhandler）
装进生产容器后，`async_update_nav_incremental` 立刻炸：

    File "/app/app/data/nav.py", line 548, in async_update_nav_incremental
        for code, navs in batch_results:
    ValueError: too many values to unpack (expected 2)

成因：批量相自己写了一遍**二解包**，而生产者返回**三元组** `(code, navs, failed)`。
它潜伏至今，是因为旧快路径（已死的 fundmobapi）总是返回 0 条，循环体从未执行。

`TestRankhandlerIncrementalContract`（test_nav_rankhandler.py）只守“产出形状”；
本文件守**整条路径真的跑得通**——任何一侧改形状都会在这里红，而不是等生产日更。

**离线保证**：`_probe_lsjz_latest` / `_rankhandler_snapshot` / `_lsjz_fetch_all` /
`_pingzhong_fetch_all_async` 全部打桩，本文件不发任何网络请求。
"""

import asyncio
import sqlite3

import pytest

import app.data.nav as nav_mod
import app.repo as repo


@pytest.fixture
def offline_increment(tmp_path, monkeypatch):
    """把增量净值路径的打桩点全部就位，让批量块真的执行（不联网、不碰真库）。"""
    import app.database as database

    db = tmp_path / "q.db"
    sqlite3.connect(str(db)).close()
    monkeypatch.setattr(database, "DB_PATH", db)

    # 库状态：唯一一只基金停在全局最新日 09-11 → 被拆进 batch（差 1 天）
    monkeypatch.setattr(repo, "get_buyable_codes", lambda: ["000001"])
    monkeypatch.setattr(repo, "get_nav_time_state",
                        lambda: ({"000001": ("2026-09-01", "2026-09-11")}, ["2026-09-11"]))

    async def fake_probe(*_a, **_k):
        return "2026-09-14"

    async def fake_snapshot(*_a, **_k):
        return {"000001": ("2026-09-14", 1.5)}

    async def no_network_fetch(*_a, **_k):
        raise AssertionError("离线用例不得发起逐只回退请求")

    monkeypatch.setattr(nav_mod, "_probe_lsjz_latest", fake_probe)
    monkeypatch.setattr(nav_mod, "_rankhandler_snapshot", fake_snapshot)
    monkeypatch.setattr(nav_mod, "_lsjz_fetch_all", no_network_fetch)
    monkeypatch.setattr(nav_mod, "_pingzhong_fetch_all_async", no_network_fetch)
    # 记账与覆盖度闸门不碰库
    monkeypatch.setattr(nav_mod, "mark_recovered_batch", lambda *_a, **_k: None)
    monkeypatch.setattr(nav_mod, "record_failure", lambda *_a, **_k: None)
    monkeypatch.setattr(nav_mod, "assert_nav_coverage",
                        lambda *_a, **_k: {"total": 1, "stale": 0,
                                           "target": "2026-09-14", "worst_lag": 0})
    return db


def test_batch_path_with_real_results_does_not_crash(offline_increment):
    """批量块拿到非空结果时必须正常写入并返回条数，而不是 ValueError。"""
    assert asyncio.run(nav_mod.async_update_nav_incremental(concurrency=2)) == 1


def test_batch_result_is_persisted(offline_increment):
    """写入要真的落库（不是只把计数加对了）。"""
    asyncio.run(nav_mod.async_update_nav_incremental(concurrency=2))
    conn = sqlite3.connect(str(offline_increment))
    rows = conn.execute("SELECT code, date, cum_nav FROM fund_nav").fetchall()
    conn.close()
    assert rows == [("000001", "2026-09-14", 1.5)]

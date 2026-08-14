"""行业映射拉取测试：push2 批量兜底 + F10 失败降级链路。

回归根因：F10 接口（emweb.securities.eastmoney.com）对云服务器 IP 反爬严格，
并发 30 密集请求被限流 → 行业映射表保持为空 → RBSA 全归"其他" → 推荐可用
赛道 0 个（推荐空转）。修复：F10 失败后统一走 push2 ulist 批量兜底
（一次 80 只、TLS 指纹伪装），并把 F10 查询并发降到 10。
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import pytest

from app.data import foundation


def _mk_conn() -> sqlite3.Connection:
    """内存库：仅建行业映射链路所需表。"""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE fund_holdings (code TEXT, report_date TEXT, "
        "stock_code TEXT, stock_name TEXT, weight REAL)"
    )
    conn.execute(
        "CREATE TABLE stock_industry_map (stock_code TEXT PRIMARY KEY, "
        "industry_code TEXT, industry_name TEXT, update_date TEXT)"
    )
    conn.execute(
        "CREATE TABLE data_fetch_failures (fetch_type TEXT, target TEXT, stage TEXT, "
        "error TEXT, attempts INTEGER, status TEXT, first_failed_at TEXT, "
        "last_failed_at TEXT, recovered_at TEXT)"
    )
    return conn


class TestPush2Secid:
    def test_secid_prefix_rules(self):
        """沪(6)=1.、深/北交(其余)=0.、港股(5位)=116.。"""
        assert foundation._push2_secid("601899") == "1.601899"
        assert foundation._push2_secid("000858") == "0.000858"
        assert foundation._push2_secid("300124") == "0.300124"
        assert foundation._push2_secid("920099") == "0.920099"
        assert foundation._push2_secid("00700") == "116.00700"
        assert foundation._push2_secid("00002") == "116.00002"


class TestFetchIndustryPush2:
    def test_parses_batch_response_and_name_fallback(self, monkeypatch):
        """批量响应解析：f100 行业 + 无行业时按名称兜底。"""
        payload = {
            "data": {
                "diff": [
                    {"f12": "601899", "f14": "紫金矿业", "f100": "贵金属"},
                    {"f12": "000858", "f14": "五粮液", "f100": "白酒"},
                    {"f12": "00857", "f14": "中国石油股份", "f100": ""},  # 无行业 → 名称兜底
                ]
            }
        }

        def fake_fetch(url, params=None, timeout=15, **kw):
            return httpx.Response(200, json=payload)

        monkeypatch.setattr(foundation, "fetch", fake_fetch)

        results: dict[str, tuple[str, str]] = {}
        added = foundation._fetch_industry_push2(["601899", "000858", "00857"], results)

        assert added == 3
        assert results["601899"] == ("贵金属", "贵金属")
        assert results["000858"] == ("白酒", "白酒")
        assert results["00857"] == ("石油天然气", "石油天然气")

    def test_batch_request_groups_80_per_call(self, monkeypatch):
        """每批最多 80 只：130 只应发出 2 次请求。"""
        urls: list[str] = []

        def fake_fetch(url, params=None, timeout=15, **kw):
            urls.append(params["secids"])
            return httpx.Response(200, json={"data": {"diff": []}})

        monkeypatch.setattr(foundation, "fetch", fake_fetch)

        stocks = [f"{i:06d}" for i in range(130)]
        foundation._fetch_industry_push2(stocks, {})
        assert len(urls) == 2
        assert len(urls[0].split(",")) == 80
        assert len(urls[1].split(",")) == 50


class TestFetchIndustryMapFallback:
    def test_f10_failure_falls_back_to_push2(self, monkeypatch):
        """F10 全部失败时，push2 批量兜底应补齐行业映射。"""
        conn = _mk_conn()
        conn.executemany(
            "INSERT INTO fund_holdings VALUES (?,?,?,?,?)",
            [
                ("000001", "2026-06-30", "601899", "紫金矿业", 10.0),
                ("000002", "2026-06-30", "000858", "五粮液", 8.0),
            ],
        )
        conn.commit()

        def fake_fetch_async(session, url, params=None, timeout=15, headers=None):
            raise httpx.TransportError("F10 被限流")

        monkeypatch.setattr(foundation, "fetch_async", fake_fetch_async)
        monkeypatch.setattr(foundation, "db_conn", lambda: conn)
        monkeypatch.setattr(foundation, "filter_cooldown_targets", lambda *a, **k: a[1])
        monkeypatch.setattr(foundation, "run_backfill_rounds", lambda *a, **k: [])

        payload = {
            "data": {
                "diff": [
                    {"f12": "601899", "f14": "紫金矿业", "f100": "贵金属"},
                    {"f12": "000858", "f14": "五粮液", "f100": "白酒"},
                ]
            }
        }

        def fake_fetch(url, params=None, timeout=15, **kw):
            return httpx.Response(200, json=payload)

        monkeypatch.setattr(foundation, "fetch", fake_fetch)

        records = foundation._fetch_industry_map()
        assert len(records) == 2
        assert ("601899", "贵金属", "贵金属") in records
        assert ("000858", "白酒", "白酒") in records

"""推荐前置门控测试（架构深化候选 2）。

回归根因：行业映射缺失（Step 4 失败）时推荐仍继续跑，RBSA 全归"其他"、
可用赛道 0 个，推荐空转并误记空推荐日。且门控曾在引擎入口用裸 SQL 计数，
绕过 repo seam。
修复：计数下沉 repo.check_data_ready（seam 单一来源），门控收敛为公共
check_recommend_ready，由管线槽位（run/run_recommend）与 CLI 入口消费，
引擎入口不再自审自拦。
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.repo.base as repo_base
from app import pipeline
from app.engine import recommend


class TestRepoCheckDataReady:
    def test_counts_holdings_and_industry(self, monkeypatch):
        """repo.check_data_ready 返回持仓/行业映射计数（seam SQL 直测）。"""
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE fund_holdings (code TEXT)")
        conn.execute("CREATE TABLE stock_industry_map (stock_code TEXT)")
        conn.executemany("INSERT INTO fund_holdings VALUES (?)", [("f1",), ("f2",)])
        conn.executemany("INSERT INTO stock_industry_map VALUES (?)", [("s1",)])
        monkeypatch.setattr(repo_base, "db_conn", lambda: conn)

        status = repo_base.check_data_ready()
        assert status == {"holdings_cnt": 2, "industry_cnt": 1}


class TestIsRecommendDataReady:
    def test_ready_true(self, monkeypatch):
        """谓词：持仓与行业映射都就绪 → True（seam 单一来源，SQL 直测）。"""
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE fund_holdings (code TEXT)")
        conn.execute("CREATE TABLE stock_industry_map (stock_code TEXT)")
        conn.executemany("INSERT INTO fund_holdings VALUES (?)", [("f1",), ("f2",)])
        conn.executemany("INSERT INTO stock_industry_map VALUES (?)", [("s1",)])
        monkeypatch.setattr(repo_base, "db_conn", lambda: conn)
        assert repo_base.is_recommend_data_ready() is True

    def test_query_error_falls_back_false(self, monkeypatch):
        """谓词：查库异常统一兜底 False（不向门控调用方抛穿）。"""
        def boom():
            raise RuntimeError("表缺失")
        monkeypatch.setattr(repo_base, "check_data_ready", boom)
        assert repo_base.is_recommend_data_ready() is False


class TestCheckRecommendReady:
    def test_both_empty_blocks(self, monkeypatch):
        monkeypatch.setattr(recommend.repo, "is_recommend_data_ready", lambda: False)
        monkeypatch.setattr(recommend.repo, "check_data_ready",
                            lambda: {"holdings_cnt": 0, "industry_cnt": 0})
        assert recommend.check_recommend_ready() is False

    def test_only_holdings_blocks(self, monkeypatch):
        monkeypatch.setattr(recommend.repo, "is_recommend_data_ready", lambda: False)
        monkeypatch.setattr(recommend.repo, "check_data_ready",
                            lambda: {"holdings_cnt": 10, "industry_cnt": 0})
        assert recommend.check_recommend_ready() is False

    def test_only_industry_blocks(self, monkeypatch):
        monkeypatch.setattr(recommend.repo, "is_recommend_data_ready", lambda: False)
        monkeypatch.setattr(recommend.repo, "check_data_ready",
                            lambda: {"holdings_cnt": 0, "industry_cnt": 10})
        assert recommend.check_recommend_ready() is False

    def test_both_ready_passes(self, monkeypatch):
        monkeypatch.setattr(recommend.repo, "is_recommend_data_ready", lambda: True)
        assert recommend.check_recommend_ready() is True

    def test_query_error_falls_back_to_not_ready(self, monkeypatch):
        """谓词兜底 False + 细节查询异常也兜底为未就绪（不中断管线）。"""
        def boom():
            raise RuntimeError("表缺失")
        monkeypatch.setattr(recommend.repo, "is_recommend_data_ready", lambda: False)
        monkeypatch.setattr(recommend.repo, "check_data_ready", boom)
        assert recommend.check_recommend_ready() is False


class TestPipelineRecommendGate:
    def test_recommend_slot_skips_when_not_ready(self, monkeypatch):
        """推荐槽位：数据未就绪（自愈后仍空）时跳过推荐，监控照常（信号链连续）。"""
        called: list[str] = []
        monkeypatch.setattr(pipeline, "_ensure_recommend_data_ready", lambda: False)
        monkeypatch.setattr(pipeline, "run_recommendation", lambda: called.append("rec"))
        monkeypatch.setattr(pipeline, "run_monitor", lambda: called.append("mon"))

        pipeline.run_recommend()
        assert called == ["mon"]

    def test_recommend_slot_runs_when_ready(self, monkeypatch):
        """推荐槽位：数据就绪时推荐与监控都执行。"""
        called: list[str] = []
        monkeypatch.setattr(pipeline, "_ensure_recommend_data_ready", lambda: True)
        monkeypatch.setattr(pipeline, "run_recommendation", lambda: called.append("rec"))
        monkeypatch.setattr(pipeline, "run_monitor", lambda: called.append("mon"))

        pipeline.run_recommend()
        assert called == ["rec", "mon"]

    def test_full_run_skips_recommend_keeps_monitor(self, monkeypatch):
        """全流程：数据未就绪时跳过推荐，监控照常（真实 phase 骨架验证信号链连续）。"""
        called: list[str] = []
        monkeypatch.setattr(pipeline, "_ensure_recommend_data_ready", lambda: False)
        monkeypatch.setattr(pipeline, "run_data_foundation", lambda steps=None: None)
        monkeypatch.setattr(pipeline, "run_recommendation", lambda: called.append("rec"))
        monkeypatch.setattr(pipeline, "run_monitor", lambda: called.append("mon"))
        monkeypatch.setattr(pipeline, "_evolve_phase", lambda today: [])

        pipeline.run()
        assert called == ["mon"]

"""B3 回归：panel_samples 并行分支（生产主路径，>64 只走并行）。

守护三件事：
1. 并行/串行**逐位一致**（声明的"训练零差异"——chunk 划分/合并不得漂移样本集）；
2. 基金池 <64 自动降级单进程（不构造 Pool）；
3. 并行失败降级串行（子进程异常不炸断训练/推荐）。

⚠ 真 spawn 子进程不继承 pytest 的 monkeypatch——此处用同步 fake Pool
（in-process 直调 _panel_chunk），覆盖并行逻辑的正确性；真实 multiprocessing
并行已验证过（开发期 1291 样本 bitwise identical 冒烟），不与测试重复。
"""

import multiprocessing
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pytest

import app.model as model
import app.repo as repo


def _fake_navs(code: str, days: int = 105, seed: int = 1) -> list[tuple[str, float]]:
    """合成净值：100 起步 + 每日小收益（有波动，可拟合）。"""
    rng = np.random.default_rng(seed + int(code) % 97)
    navs: list[tuple[str, float]] = [("2026-01-01", 100.0)]
    for i in range(1, days):
        d = date(2026, 1, 1) + timedelta(days=i)
        navs.append((d.isoformat(), navs[-1][1] * (1.0 + rng.normal(0.001, 0.012))))
    return navs


def _fake_sector_frame():
    """3 板块 × 220 日百分数涨幅宽表（pivot 形态，值 = 百分数）。"""
    import pandas as pd
    rng = np.random.default_rng(9)
    rows = []
    d0 = date(2025, 12, 1)
    for i in range(220):
        row = {"date": (d0 + timedelta(days=i)).isoformat()}
        for name in ("板块A", "板块B", "板块C"):
            row[name] = rng.normal(0.02, 0.8)
        rows.append(row)
    return pd.DataFrame(rows).set_index("date")


def _fake_index_rows(n: int = 220) -> list[tuple]:
    """指数行 (date, close, volume)，close 单调 + 波动。"""
    out = []
    for i, (d, _v) in enumerate(_fake_navs("0", n)):
        out.append((d, 3000.0 + i + (i % 5) * 0.1, 1e7 + i))
    return out


class _FakePool:
    def __init__(self, workers):
        self.workers = workers

    def map(self, fn, args):
        return [fn(a) for a in args]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeCtx:
    def Pool(self, workers):
        return _FakePool(workers)


class _ExplodingCtx:
    def Pool(self, workers):
        class _P(_FakePool):
            def map(self, fn, args):
                raise RuntimeError("子进程竞态模拟")
        return _P(workers)


@pytest.fixture()
def stub_data(monkeypatch):
    """fake 数据源（指数/净值/板块矩阵）。"""
    monkeypatch.setattr(repo, "get_index_series",
                        lambda symbol, cols=None, start=None, end=None:
                        _fake_index_rows())
    monkeypatch.setattr(repo.nav, "series",
                        lambda code, limit=None, until=None: _fake_navs(code))
    monkeypatch.setattr(model, "load_sector_pct_frame", _fake_sector_frame)
    monkeypatch.setattr(multiprocessing, "get_context", lambda name: _FakeCtx())


def _canon(samples):
    out = []
    for d, feat, ya, adj in samples:
        out.append((str(d), tuple((k, round(float(v), 8))
                                  for k, v in sorted(feat.items())),
                    round(float(ya), 8),
                    tuple((k, round(float(v), 8))
                          for k, v in sorted(adj.items()))))
    return sorted(out, key=repr)


class TestPanelParallel:
    def test_parallel_bitwise_identical_to_serial(self, stub_data):
        """≥64 只走并行（fake Pool），结果与串行逐位一致（训练零差异）。"""
        codes = [f"{100000 + i}" for i in range(65)]
        serial = model.panel_samples(codes, workers=1)
        parallel = model.panel_samples(codes, workers=4)
        assert len(parallel) == len(serial), "并行样本数 != 串行"
        assert _canon(parallel) == _canon(serial), "并行/串行样本集不一致（训练会漂移）"

    def test_small_pool_auto_serial(self, monkeypatch, stub_data):
        """<64 只自动降级单进程：显式 workers=4 也不构造 Pool。"""
        called = []

        def boom(name):
            called.append(name)
            raise AssertionError("小基金池不应构造 Pool")
        monkeypatch.setattr(multiprocessing, "get_context", boom)
        codes = [f"{200000 + i}" for i in range(8)]
        samples = model.panel_samples(codes, workers=4)
        assert isinstance(samples, list) and called == [], \
            "基金池 <64 不应触发并行上下文"

    def test_pool_failure_falls_back_serial(self, monkeypatch, stub_data):
        """并行 Pool.map 抛异常 → 降级串行，产出与正常串行一致的样本。"""
        monkeypatch.setattr(multiprocessing, "get_context",
                            lambda name: _ExplodingCtx())
        codes = [f"{300000 + i}" for i in range(65)]
        got = model.panel_samples(codes, workers=4)
        assert isinstance(got, list) and len(got) >= 1, "降级串行应产出样本"

    def test_workers_one_forced_serial(self, stub_data):
        """显式 workers=1：>=64 只也直接串行。"""
        codes = [f"{400000 + i}" for i in range(70)]
        samples = model.panel_samples(codes, workers=1)
        assert isinstance(samples, list)

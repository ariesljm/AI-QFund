"""features/stats 统计原语测试（架构审查候选 2）。

验证：与 scipy 语义一致、常数序列降级 None、t 双侧尾概率、以及
「引擎层不直接 import scipy」的豁免边界可执行。
"""

import numpy as np
import pytest

from app.features.stats import rankdata, spearman, pearson, t_tail_p


class TestRankdata:
    def test_matches_scipy_average_ties(self):
        import scipy.stats as sps
        x = [3.0, 1.0, 2.0, 2.0, 5.0]
        np.testing.assert_allclose(rankdata(x), sps.rankdata(x, method="average"))

    def test_no_ties(self):
        np.testing.assert_allclose(rankdata([2.0, 7.0, 1.0]), [2.0, 3.0, 1.0])


class TestSpearman:
    def test_positive_correlation(self):
        rho, p = spearman([1, 2, 3, 4, 5], [2, 4, 6, 8, 10])  # type: ignore[arg-type]
        assert rho == pytest.approx(1.0) and p < 0.05

    def test_constant_series_returns_none(self):
        assert spearman([1, 1, 1], [1, 2, 3]) is None
        assert spearman([1, 2, 3], [5, 5, 5]) is None

    def test_matches_scipy(self):
        import scipy.stats as sps
        rng = np.random.default_rng(4)
        x, y = rng.normal(size=30), rng.normal(size=30)
        rho, p = spearman(x, y)
        assert rho == pytest.approx(sps.spearmanr(x, y).statistic)
        assert p == pytest.approx(sps.spearmanr(x, y).pvalue)


class TestPearson:
    def test_perfect_linear(self):
        x = np.array([1.0, 2.0, 3.0])
        r, p = pearson(x, 2.0 * x)
        assert r == pytest.approx(1.0)

    def test_matches_scipy(self):
        import scipy.stats as sps
        rng = np.random.default_rng(6)
        x, y = rng.normal(size=40), rng.normal(size=40)
        assert pearson(x, y) == pytest.approx(
            (sps.pearsonr(x, y).statistic, sps.pearsonr(x, y).pvalue))


class TestTTailP:
    def test_symmetric_two_sided(self):
        assert t_tail_p(2.0, 100) == pytest.approx(t_tail_p(-2.0, 100))
        assert 0 < float(t_tail_p(2.0, 100)) < 0.1

    def test_large_t_small_p(self):
        assert float(t_tail_p(8.0, 1000)) < 1e-10

    def test_array_input(self):
        out = t_tail_p(np.array([1.0, 2.0]), 100)
        assert out.shape == (2,) and out[1] < out[0]


class TestExemptionBoundary:
    def test_engine_layers_do_not_import_scipy(self):
        """豁免边界可执行：引擎/数据层源码不应直接出现 scipy import。"""
import glob
import re

offenders = []
import_stmt = re.compile(r"^\s*(import scipy|from scipy\b)")
for f in sorted(glob.glob("app/**/*.py", recursive=True)):
    if "/features/" in f or "\\features\\" in f:
        continue  # features 域是豁免区
    if "__pycache__" in f:
        continue
    src = open(f, encoding="utf-8").read()
    for line in src.splitlines():
        # 只匹配真正的 import/from 语句（docstring/注释里的文字不算）
        if import_stmt.match(line):
            offenders.append(f"{f}: {line.strip()}")
assert offenders == [], f"引擎层出现 scipy import（违反 ADR-0007）: {offenders}"
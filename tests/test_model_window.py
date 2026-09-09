"""Ticket 02：模型训练标签为"决策日起第 40 交易日绝对收益"验收测试。

prepare_training_data 的 y 语义随 FORWARD_DAYS=40 自动对齐（常量单一来源）；
本测试把该语义固化为验收文档：给定已知净值与指数序列，y 必须等于
nav[id + 40] / nav[id] - 1（而非 20 日）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from app import model as model_mod
from app import repo as repo_mod


def _make_data(n_idx=300, n_nav=200):
    """连续交易日序列：指数 300 天，基金净值从前 200 天起（1.0 + i*0.01）。"""
    base = pd.Timestamp("2026-01-01")
    idx_dates = [(base + pd.Timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n_idx)]
    nav_dates = idx_dates[:n_nav]
    navs = [1.0 + i * 0.01 for i in range(n_nav)]
    idx_rows = [(d, 3000.0 + i, 1e7) for i, d in enumerate(idx_dates)]
    return idx_rows, nav_dates, navs


class TestPrepareTrainingLabel:
    def test_y_is_40d_forward_return(self, monkeypatch):
        """训练/验证样本的 y = nav[id+40]/nav[id] - 1（40 日绝对收益）。"""
        idx_rows, nav_dates, navs = _make_data()
        monkeypatch.setattr(repo_mod, "get_index_series",
                            lambda code, cols: idx_rows)
        monkeypatch.setattr(repo_mod.nav, "series", lambda code: list(zip(nav_dates, navs, strict=True)))
        monkeypatch.setattr(repo_mod, "get_train_fund_codes", lambda *a, **k: ["F1"])

        X_train, y_train, _, X_val, y_val, _ = model_mod.prepare_training_data()

        # 采样位置：range(60, 199-40, 20) = 60, 80, 100, 120, 140（trailing 20% 作验证）
        sample_pos = [60, 80, 100, 120]
        assert len(X_train) == 4 and len(y_train) == 4
        assert len(y_val) == 1
        for pos, y in zip(sample_pos, y_train, strict=True):
            assert y == pytest.approx(navs[pos + 40] / navs[pos] - 1.0, abs=1e-9)
        # 验证集 = 最后一个样本 pos=140
        assert y_val.iloc[0] == pytest.approx(navs[180] / navs[140] - 1.0, abs=1e-9)

    def test_label_series_named_40d(self, monkeypatch):
        """标签序列命名为 abs_ret_40d（与窗口口径一致）。"""
        idx_rows, nav_dates, navs = _make_data()
        monkeypatch.setattr(repo_mod, "get_index_series",
                            lambda code, cols: idx_rows)
        monkeypatch.setattr(repo_mod.nav, "series", lambda code: list(zip(nav_dates, navs, strict=True)))
        monkeypatch.setattr(repo_mod, "get_train_fund_codes", lambda *a, **k: ["F1"])

        _, y_train, _, _, y_val, _ = model_mod.prepare_training_data()
        assert y_train.name == "abs_ret_40d"
        assert y_val.name == "abs_ret_40d"


import pytest  # noqa: E402  # 底部导入：pytest.approx 在断言中使用

_ = np  # noqa: F401  # 保留 numpy 引用（与生产代码风格一致）

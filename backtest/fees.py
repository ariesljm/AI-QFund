"""费用模型已上移至 app/features/fees.py（生产单一来源）。

本文件保留 re-export 以兼容既有引用（backtest/backtest.py 与
tests/test_backtest_fee_model.py 的 `from backtest.fees import ...`），
费率档位/公式只在一处维护（app/features/fees.py），避免双真相。
"""

from app.features.fees import (  # noqa: F401
    STOP_HOLD_DAYS,
    net_return,
    redemption_fee_pct,
)

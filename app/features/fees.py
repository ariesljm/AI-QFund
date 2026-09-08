"""费用与执行摩擦模型（单一来源，已从 backtest/fees.py 上移）。

把"净值收益"换算成"到手收益"：申购费 + 赎回费（按持有天数分段）+ T+1 滑点。
回测默认不启用（费率参数传 0 = 关闭，向后兼容）；启用时输出费后口径，
回答审计问题"费用是否吃掉动量因子的利润空间"。

生产侧（quality 端到端 P&L 度量）与回测共用本模块，避免费率档位双真相。
费率档位为场外基金常见简化口径（平台折扣可另行配置，见 net_return 参数）。
"""

# 赎回费率档（持有自然日，常见场外档位）：[(最低持有天数, 费率%)]
_REDEMPTION_TABLE = [
    (365, 0.0),    # ≥1 年
    (30, 0.5),     # 30-364 日
    (7, 0.75),     # 7-29 日
    (0, 1.5),      # <7 日（惩罚性费率）
]


def redemption_fee_pct(hold_days: int) -> float:
    """赎回费率（%）：按持有自然日查分段表。"""
    for min_days, fee in _REDEMPTION_TABLE:
        if hold_days >= min_days:
            return fee
    return 1.5


def net_return(gross_ret: float, hold_days: int,
               fee_buy_pct: float = 0.15, slippage_pct: float = 0.3,
               fee_sell_pct: float | None = None) -> float:
    """毛收益（小数）→ 到手收益（小数）。

    到手 = (1+gross) × (1−申购费−滑点) × (1−赎回费) − 1
    gross_ret 为小数（如 0.05 = +5%）；费率均为百分数（如 0.15 = 0.15%）。
    fee_sell_pct=None 时按持有天数查分段表；显式传 0 关闭赎回费（纯毛收益）。
    """
    buy_cost = (fee_buy_pct + slippage_pct) / 100.0
    sell_cost = (fee_sell_pct if fee_sell_pct is not None
                 else redemption_fee_pct(hold_days)) / 100.0
    return (1.0 + gross_ret) * (1.0 - buy_cost) * (1.0 - sell_cost) - 1.0


# 止损路径的持有天数近似：硬止损通常入场后 1-3 周触发，
# 取 7-29 日档中值（14 日）作为赎回费率档位（与"持有到期"口径区分）。
STOP_HOLD_DAYS = 14

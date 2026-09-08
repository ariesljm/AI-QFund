# Backtest 研究层归档（2026-09 架构深化，候选 1）

这些脚本是**已完成使命的一次性研究**——结论已沉淀进生产（`app/engine/sector_pool.py`
信号、熊市防守机制、多周期信号），`app/` 与 `tests/` 零引用。归档于此作为历史证据；
如需复现，用 git 历史取回原版本：

```bash
git show <commit>:backtest/<file> > docs/research/backtest-archive/<file>
```

> 注：归档脚本的 import 路径按原位置编写（部分依赖 `backtest/` 内部模块），
> 移动后不可直接运行——这正是"研究证据"与"生产回测"的语义分界。

## 归档清单

| 脚本 | 用途 | 结论去向 | 复现依赖 |
|---|---|---|---|
| `bear_market_research.py` | 熊市盈利机制研究（24089 熊市样本，20 日口径） | 5 日动量高分位胜率 50.2% vs 低分位 18.0% → 入场门槛方向；低波动高分位 29.8% vs 低分位 60.1% → 熊市高波动降权（`sector_pool.BEAR_HIGH_VOL_*`） | `backtest/_regime.py`（生产保留） |
| `sector_strategy_research.py` | 赛道策略截面研究（动量/热度/波动分位） | 定池信号权重与降权逻辑（`sector_pool._signal_of`） | `app.repo` |
| `sector_multi_window.py` | 多周期动量（5/20/60 日）信号研究 | 多周期信号进定池 prompt（`sector_pool` 多周期趋势特征） | `backtest/sector_signals.py`（生产保留，`_load_holdings_labels`/`_load_nav_pivot`） |

## 未归档（研究基础设施，有测试保护）

- `backtest/sector_signals.py` —— `_cross_sectional_ic` 被 `tests/test_backtest_pure_funcs.py` 测试保护
- `backtest/walk_forward_holdout.py` —— `holdout_split`/`compare_segments` 被 `tests/test_walk_forward_holdout.py` 测试保护（样本外纪律工具）

## 关联清理

- `data/` 对应研究产物 JSON（`bear_market_research.json`、`sector_*_research.json` 等）已删除
  （gitignore 已忽略，运行时产物；结论见上表）

"""用 2.0 主标尺重算 1.x 的推荐记录，与 1.x 自己的绝对收益口径并排输出。

两个目的：

1. **验证标尺实现**——若重算结果与直觉矛盾，先怀疑实现，而不是先解释行情。
2. **重新解读 1.x**——若 1.x 一直是正超额，则此前所有"模型区分度失效"的结论都要
   改写成"beta 拖累"（`docs/backtest/model_walk_forward_40d.md` 的验证期是单边
   下跌段，整体 40 日胜率 36%、IC≈0）。

⚠️ **窗口未满**：1.x 推荐日为 2026-08-14 ~ 09-04，而库内净值只到 2026-09-11
（最早一条只走了约 20 个交易日）。因此这里的收益与超额是**部分窗口**口径，不是
40 日标尺值。满 40 日的完整结算要等到约 2026-11。输出中明确标注。

用法：
    python scripts/recheck_1x_excess.py > docs/backtest/1x-excess-recheck.md
"""

from __future__ import annotations

import sqlite3
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import benchmark  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB = Path("data/qfund.db")

# 解读部分随脚本一起生成（而不是事后手写进 markdown）——ticket 目录不纳入版本管理，
# 结论必须留在版本化的产物文件里，否则重跑一次就丢了。
_INTERPRETATION = """
## 解读

**1.x 的选基对同业没有任何可识别的选择价值。** 部分窗口口径下超额收益均值 −0.07pp、
正超额占比 50.0%（n=14）——**恰好是抛硬币**。同时绝对收益均值 −3.02%、赚钱胜率 17.6%
（n=17）：钱是亏的，而拿一只同行业的任意基金也差不多。

对“1.x 一直在正超额”这个假设：**没有证据支持**。所以此前“模型区分度失效”的结论
**不需要**改写成“只是 beta 拖累”——超额接近零恰好说明失效是真失效，不是 beta 拖累。

### 一个值得单独记下的教训：窗口口径不是小事

本报告的第一版用“区间内首个/末个可用净值”算收益，得超额 −0.68pp / 正超额占比 46.4%；
改成“入场日与截至日**两端都必须有净值**”后变成 −0.07pp / 50.0%。
差别来自当时全市场一半基金的净值停更在 2026-09-03（见下），旧口径把停更基金的
14 个交易日收益与正常基金的 20 个交易日收益放进了同一个同类均值。
**这类偏差不会报错，只会让结论失真**——已收敛到 `app/benchmark.window_return` 单一口径。

### 必须一起看的限定

1. **窗口未满**（约 20 / 40 交易日）。部分窗口的排序与满 40 日可能不同；2026-11 后应
   重跑本脚本，以满 40 日结果为准。
2. **两个口径不可直接比较**。`1.x 已实现收益` 是 1.x 自己的**提前退出**结果
   （12 条有值，均值 −5.99%、胜率 8.3%），不是 40 日持有结果——它反映“1.x 的选基 +
   1.x 的退出策略”合起来的效果，而 40 日超额只衡量选基。
3. **样本极小且不完整**：16 个推荐日、单一行情段，且 **11/28 条因净值停更根本算不出**。

### 顺带暴露的数据基座故障

全市场 `fund_nav` 日度条数从 2026-09-04 起由约 12,400 腰斩到约 6,300，截断按代码前缀
（当日只有 0 开头的 6,435 只入库，5/1 开头几乎全缺）；`fund_features` 自 **2026-09-03
起完全停止**。也就是说 1.x 的生产管道大约从 09-04 起已经半死，而推荐记录也正好停在
2026-09-04。这不是本工单该修的，但 **2.0 要保留并扩写的数据基座，当前并不是健康的**。
"""


def _load_nav(conn: sqlite3.Connection, since: str) -> dict[str, dict[str, float]]:
    """{code: {date: cum_nav}}，仅取 since 之后。"""
    out: dict[str, dict[str, float]] = {}
    for code, date, nav in conn.execute(
        "SELECT code, date, cum_nav FROM fund_nav WHERE date >= ? ORDER BY code, date", (since,)
    ):
        out.setdefault(code, {})[date] = nav
    return out


def _window_return(navs_by_date: dict[str, float], start: str, end: str) -> float | None:
    """同窗口收益（要求两端日期都有净值）——口径在 `app.benchmark.window_return`。

    早期版本用“区间内首个/末个可用净值”，会把一只停更在 09-03 的基金的 14 个交易日
    收益，与另一只走到 09-11 的 20 个交易日收益放进同一个同类均值里。
    实测 2026-09-14：全市场一半基金停更在 09-03，所以那个偏差是实在的，不是理论风险。
    """
    return benchmark.window_return(navs_by_date, start, end)


def _features_at(conn: sqlite3.Connection, date: str) -> tuple[str, dict[str, str | None]]:
    """截至 date 的最近一期特征快照 → (特征日, {code: rbsa_industry_1})。"""
    row = conn.execute("SELECT MAX(date) FROM fund_features WHERE date <= ?", (date,)).fetchone()
    feat_date = row[0] if row else None
    if not feat_date:
        return "", {}
    return feat_date, {
        code: benchmark.peer_group({"rbsa_industry_1": ind})
        for code, ind in conn.execute(
            "SELECT code, rbsa_industry_1 FROM fund_features WHERE date = ?", (feat_date,)
        )
    }


def main() -> None:
    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    recs = conn.execute(
        "SELECT recommend_date, code, name, rank, status, return_rate "
        "FROM recommend_log ORDER BY recommend_date, rank"
    ).fetchall()
    if not recs:
        sys.exit("recommend_log 无记录（是否已被 2.0 DROP？改用归档 CSV）")

    end = conn.execute("SELECT MAX(date) FROM fund_nav").fetchone()[0]
    first_date = min(r[0] for r in recs)
    navs = _load_nav(conn, first_date)

    print("# 1.x 推荐记录：2.0 主标尺重算（部分窗口）\n")
    print(f"- 生成于 1.x 冻结库快照，净值截止 **{end}**")
    print(f"- 1.x 推荐日：**{first_date} ~ {max(r[0] for r in recs)}**（{len({r[0] for r in recs})} 个推荐日 / {len(recs)} 条记录）")
    print(f"- 标尺版本：`{benchmark.BENCHMARK_VERSION}`，前向窗口目标 **{benchmark.FORWARD_DAYS} 交易日**，"
          f"同类最小样本 **{benchmark.MIN_PEER_SAMPLES}**（不含自身）")
    print("- 同类宇宙：同一 RBSA 第一行业的全部基金，**不按基金类型过滤**（见 `app/benchmark.py` 头口径声明）")
    print("- 窗口完整性：所有收益均要求**入场日与截至日两端都有净值**（`benchmark.window_return`），"
          "否则会拿不同长度的窗口做截面对比\n")
    print("> ⚠️ **窗口未满**：下列收益与超额是「推荐日 → 净值截止日」的**部分窗口**值，")
    print("> 不是 40 日标尺值。完整结算需等到约 2026-11。\n")

    print("| 推荐日 | 代码 | 名称 | 1.x 已实现收益 | 绝对收益（部分窗口） | 同类 | 同类样本 | 超额（部分窗口） |")
    print("|---|---|---|---:|---:|---|---:|---:|")

    rows_report: list[tuple[float | None, float | None, float | None]] = []
    feat_cache: dict[str, tuple[str, dict[str, str | None]]] = {}
    for rec_date, code, name, _rank, _status, realized in recs:
        if rec_date not in feat_cache:
            feat_cache[rec_date] = _features_at(conn, rec_date)
        _feat_date, feats = feat_cache[rec_date]

        own = _window_return(navs.get(code, {}), rec_date, end)
        industry = feats.get(code)
        peer_returns: list[float | None] = []
        if industry:
            peer_returns = [
                _window_return(navs.get(c, {}), rec_date, end)
                for c, ind in feats.items()
                if ind == industry and c != code
            ]
        excess = benchmark.excess_return(own, peer_returns)
        valid_peers = sum(1 for r in peer_returns if r is not None)

        fmt = lambda v: f"{v * 100:+.2f}%" if v is not None else "—"  # noqa: E731
        print(f"| {rec_date} | {code} | {name} | {fmt(realized)} | {fmt(own)} | "
              f"{industry or '（空→出局）'} | {valid_peers} | {fmt(excess)} |")
        rows_report.append((realized, own, excess))

    print("\n## 汇总\n")
    unavailable = sum(1 for _, own, _ in rows_report if own is None)
    if unavailable:
        print(f"- ⚠️ **{unavailable}/{len(rows_report)} 条自身窗口不可用**（入场日或截至日缺净值）——"
              "全市场一半基金的净值自 2026-09-04 起停更在 2026-09-03，故这些推荐算不出收益。")
    realized_v = [r for r, _, _ in rows_report if r is not None]
    own_v = [o for _, o, _ in rows_report if o is not None]
    excess_v = [e for _, _, e in rows_report if e is not None]
    print(f"- 1.x 已实现收益（截至各自 EXIT）: n={len(realized_v)}，均值 "
          f"{statistics.mean(realized_v) * 100:+.2f}%，赚钱胜率 "
          f"{sum(1 for r in realized_v if r > 0) / len(realized_v) * 100:.1f}%（>0 口径）")
    if own_v:
        print(f"- 绝对收益（部分窗口）: n={len(own_v)}，均值 {statistics.mean(own_v) * 100:+.2f}%，"
              f"赚钱胜率 {sum(1 for r in own_v if benchmark.is_profit(r)) / len(own_v) * 100:.1f}%（>1% 口径）")
    if excess_v:
        print(f"- **超额收益（部分窗口）**: n={len(excess_v)}（{len(rows_report) - len(excess_v)} 条因同类样本不足被剔除），"
              f"均值 {statistics.mean(excess_v) * 100:+.2f}%，"
              f"正超额占比 {sum(1 for e in excess_v if e > 0) / len(excess_v) * 100:.1f}%")
    else:
        print("- **超额收益：全部被剔除**（同类样本不足或 RBSA 为空）")

    print(_INTERPRETATION)


if __name__ == "__main__":
    main()

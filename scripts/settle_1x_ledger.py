"""用 2.0 结算账本口径处理 1.x 的 28 条推荐，并报告三统计量。

这是 ticket 03 的验收证据。它证实了一件必须明说的事：1.x 的推荐日
（2026-08-14 ~ 09-04）到今天（2026-09-14，库内净值到 2026-09-11）**没有任何一条
走满 40 个交易日**，因此按标尺口径**一条都不能结算**。最早一条要到约 2026-10-14。
这不是实现故障，而是标尺的窗口条件尚未满足（类似「财报还没出，不能结算」）。

脚本仍然跑通了完整装配路径（交易日历定位窗口 → 取端点净值 → 算绝对收益与窗口
回撤 → 装配同类收益 → `settlement.build`），所以它同时验证了实现。

窗口口径：以**市场交易日历**定位（入场日 + 40 个交易日），且要求入场日与结算日
两端都有净值——不用“按行数取第 41 条”，因为净值有洞时那会把不同长度的窗口
当成同一个窗口（实测全市场一半基金的净值停更在 2026-09-03）。

`--write` 才真正落库到 `settlement_ledger`（默认 dry-run）。

⚠️ 装配逻辑目前在本脚本里。等 ticket 11（初筛引擎）落地后，装配属于引擎的职责
（它才知道候选池、特征日与自陈置信度从哪来），届时本脚本应改为调用引擎。

用法：
    python scripts/settle_1x_ledger.py            # dry-run，只报告
    python scripts/settle_1x_ledger.py --write     # 真正写账
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import benchmark, domain, settlement  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB = Path("data/qfund.db")
WINDOW = domain.FORWARD_DAYS  # 40 个交易日


def _load(conn: sqlite3.Connection, since: str) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for code, date, nav in conn.execute(
        "SELECT code, date, cum_nav FROM fund_nav WHERE date >= ? ORDER BY code, date", (since,)
    ):
        out.setdefault(code, {})[date] = nav
    return out


def _features_at(conn: sqlite3.Connection, date: str) -> dict[str, str | None]:
    row = conn.execute("SELECT MAX(date) FROM fund_features WHERE date <= ?", (date,)).fetchone()
    if not row or not row[0]:
        return {}
    return {
        code: benchmark.peer_group({"rbsa_industry_1": ind})
        for code, ind in conn.execute(
            "SELECT code, rbsa_industry_1 FROM fund_features WHERE date = ?", (row[0],)
        )
    }


def _drawdown(navs: dict[str, float], start: str, end: str) -> float | None:
    """窗口内最大回撤（路径用窗口内该基金**实际存在**的净值点）。"""
    path = [v for d, v in sorted(navs.items()) if start <= d <= end]
    return domain.window_max_drawdown(path)


def main() -> None:
    ap = argparse.ArgumentParser(description="1.x 推荐记录的 2.0 结算账本验收")
    ap.add_argument("--write", action="store_true", help="真正写入 settlement_ledger（默认 dry-run）")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    recs = conn.execute(
        "SELECT recommend_date, code, name FROM recommend_log ORDER BY recommend_date, code"
    ).fetchall()
    if not recs:
        sys.exit("recommend_log 无记录")

    trading_days = [d for (d,) in conn.execute("SELECT DISTINCT date FROM fund_nav ORDER BY date")]
    idx = {d: i for i, d in enumerate(trading_days)}
    navs = _load(conn, min(r[0] for r in recs))
    feat_cache: dict[str, dict[str, str | None]] = {}

    print("# 1.x 推荐记录：2.0 结算账本验收\n")
    print(f"- 标尺版本 `{benchmark.BENCHMARK_VERSION}`，窗口 **{WINDOW} 交易日**，"
          f"同类最小样本 **{benchmark.MIN_PEER_SAMPLES}**")
    print(f"- 交易日历覆盖到：**{trading_days[-1]}**")
    print(f"- 待处理推荐：{len(recs)} 条 / {len({r[0] for r in recs})} 个推荐日\n")

    print("| 推荐日 | 代码 | 名称 | 窗口状态 | 绝对收益 | 窗口回撤 | 同类样本 | 超额 | 结算日 |")
    print("|---|---|---|---|---:|---:|---:|---:|---|")

    built: list[settlement.Settlement] = []
    pending: list[tuple[str, int]] = []
    missing: list[str] = []
    for reco_date, code, name in recs:
        if reco_date not in feat_cache:
            feat_cache[reco_date] = _features_at(conn, reco_date)
        feats = feat_cache[reco_date]
        own = dd = None
        settle_date = None
        i = idx.get(reco_date)
        if i is None:
            status = "推荐日不在交易日历"
        elif i + WINDOW >= len(trading_days):
            need = i + WINDOW - (len(trading_days) - 1)
            pending.append((reco_date, need))
            status = f"未完成（还差 {need} 个交易日）"
        else:
            settle_date = trading_days[i + WINDOW]
            own = benchmark.window_return(navs.get(code, {}), reco_date, settle_date)
            dd = _drawdown(navs.get(code, {}), reco_date, settle_date)
            status = "净值缺失" if own is None else "已结算"
            if own is None:
                missing.append(code)

        peer_returns: list[float | None] = []
        if own is not None and settle_date:
            industry = feats.get(code)
            if industry:
                peer_returns = [
                    benchmark.window_return(navs.get(c, {}), reco_date, settle_date)
                    for c, ind in feats.items()
                    if ind == industry and c != code
                ]
        row = settlement.build(
            reco_date=reco_date, code=code, settle_date=settle_date or "",
            own_return=own, max_drawdown=dd, peer_returns=peer_returns,
        )
        if row is not None:
            built.append(row)

        fmt = lambda v: f"{v * 100:+.2f}%" if v is not None else "—"  # noqa: E731
        print(f"| {reco_date} | {code} | {name} | {status} | {fmt(own)} | {fmt(dd)} | "
              f"{row.peer_n if row else '—'} | {fmt(row.excess_return if row else None)} | "
              f"{settle_date or '—'} |")

    print("\n## 三统计量\n")
    print(f"- 可结算 **{len(built)}** 条 / 未完成 **{len(pending)}** 条 / 净值缺失 **{len(missing)}** 条\n")
    if built:
        f = lambda v: f"{v * 100:+.2f}%" if v is not None else "—"  # noqa: E731
        print("| 角色 | 统计量 | 值 |")
        print("|---|---|---:|")
        print(f"| 主标尺（守门） | 超额收益期望 | {f(settlement.benchmark_expectation(built))} |")
        print(f"| 过程标尺（诊断） | 校准准确率偏差 | {f(settlement.calibration_gap(built))} |")
        print(f"| 用户口径（展示） | 绝对赚钱胜率 | {f(settlement.absolute_win_rate(built))} |")
    else:
        print("**三条统计量均为空**——没有一条推荐走满 40 个交易日，按标尺口径无从计算。")
        print()
        print("这不是实现故障：标尺的窗口条件尚未满足（类似「财报还没出，不能结算」）。")
        if pending:
            first, need = min(pending)
            print(f"最早的推荐日 {first} 需再等 **{need} 个交易日**（约到 2026-10 中旬）。")
        print("此后本脚本重跑即可自动结算，无需改代码。")
    if missing:
        print()
        print(f"⚠️ **{len(missing)} 条因净值缺失算不出收益**：全市场一半基金的净值自 "
              "2026-09-04 起停更在 2026-09-03（数据基座故障，非标尺问题）。")

    if built and args.write:
        from app import repo

        print(f"\n已写入 settlement_ledger：{repo.ledger.insert(built)} 条")
    elif built:
        print("\n（dry-run：未写库。加 --write 才真正写账）")


if __name__ == "__main__":
    main()

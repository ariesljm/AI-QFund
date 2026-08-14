"""EXIT 信号质量度量（阶段三闭环）：评估每次 EXIT 的事后正确性。

口径：对 recommend_log 中已 EXIT 的持仓，取 exit_date 后 horizon 交易日净值：
  - post_exit_ret < 0 → 退出正确（避免了后续下跌）
  - post_exit_ret > 0 → 误杀（退出后反弹）
输出：EXIT 次数、平均事后收益、胜率（事后仍跌的比例）、分位；按信号类型分组。
运行：uv run python backtest_exit_quality.py [--horizon 20] [--min-exits 3]
"""
import argparse
import sqlite3
from pathlib import Path

import numpy as np

DB = Path("data/qfund.db")
HORIZONS = (20, 60)


def run(horizon: int, min_exits: int) -> None:
    conn = sqlite3.connect(str(DB))
    rows = conn.execute(
        "SELECT code, name, exit_date, sell_reason, return_rate FROM recommend_log "
        "WHERE status='EXIT' AND exit_date IS NOT NULL ORDER BY exit_date"
    ).fetchall()
    conn.close()
    print(f"EXIT 记录共 {len(rows)} 条（要求 ≥{min_exits} 条才输出统计）")
    if len(rows) < min_exits:
        print("样本不足，跳过（历史 EXIT 记录少属正常——系统刚上线/历史清理过）")
        return

    # 逐条取 exit_date 后 horizon 日净值
    conn = sqlite3.connect(str(DB))
    results = []
    for code, name, exit_date, reason, hold_ret in rows:
        navs = conn.execute(
            "SELECT date, cum_nav FROM fund_nav WHERE code=? AND date>=? ORDER BY date LIMIT ?",
            (code, exit_date, horizon + 1)).fetchall()
        if len(navs) < 2:
            continue
        entry = navs[0][1]
        if entry <= 0:
            continue
        post_ret = navs[-1][1] / entry - 1.0
        results.append({
            "code": code, "name": name, "exit_date": exit_date,
            "reason": (reason or "")[:40], "hold_ret": hold_ret,
            "post_exit_ret": post_ret, "horizon": len(navs) - 1,
        })
    conn.close()

    if not results:
        print("无可评估样本")
        return
    rets = np.array([r["post_exit_ret"] for r in results])
    print(f"\n=========== EXIT 事后质量（exit 后 {horizon} 交易日窗口，样本 {len(results)}） ===========")
    print(f"平均事后收益: {rets.mean() * 100:+.2f}%  中位: {np.median(rets) * 100:+.2f}%")
    print(f"退出正确率（事后仍下跌）: {(rets < 0).mean() * 100:.1f}%")
    print(f"误杀率（事后反弹 >2%）: {(rets > 0.02).mean() * 100:.1f}%")
    print(f"分布: P25 {np.percentile(rets, 25) * 100:+.2f}%  P50 {np.percentile(rets, 50) * 100:+.2f}%  "
          f"P75 {np.percentile(rets, 75) * 100:+.2f}%")
    print("\n明细（事后收益 升序，最差在前）:")
    for r in sorted(results, key=lambda x: x["post_exit_ret"]):
        print(f"  {r['exit_date']} {r['code']} {r['name'][:14]:<16} 事后{r['post_exit_ret'] * 100:+.2f}% | {r['reason']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", type=int, default=20)
    ap.add_argument("--min-exits", type=int, default=3)
    args = ap.parse_args()
    run(args.horizon, args.min_exits)

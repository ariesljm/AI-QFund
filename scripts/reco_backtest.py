"""历史推荐收益实证 v2（重构基线）：
- 已 EXIT：用 recommend_log.return_rate 真实结算收益
- 持仓中（HOLD/WARNING）：用"至今收益"（推荐日 → 库内最新净值日 9-03，约15交易日）
"""
import sqlite3
import statistics
from collections import Counter, defaultdict


def _fmt_pct(v, dash="  -"):
    """格式化为带符号百分数，None 显示占位符。"""
    return f"{v * 100:+.2f}%" if v is not None else dash


conn = sqlite3.connect("data/qfund.db")
cur = conn.cursor()

cur.execute("SELECT recommend_date, code, name, rank, score, regime, status, return_rate "
            "FROM recommend_log ORDER BY recommend_date")
recs = cur.fetchall()

# 每只基金最新净值日期（数据截止）
cur.execute("SELECT code, max(date) FROM fund_nav WHERE cum_nav IS NOT NULL GROUP BY code")
latest_nav = dict(cur.fetchall())
GT = "2026-09-03"  # 库内最新净值日


def fund_ret_to(code: str, d: str, target: str):
    """推荐日 d 净值 → target 净值（或库内最新）的收益"""
    cur.execute("SELECT date, cum_nav FROM fund_nav WHERE code=? AND date>=? AND cum_nav IS NOT NULL ORDER BY date",
                (code, d))
    rows = cur.fetchall()
    if not rows or not rows[0][1]:
        return None, 0
    nav0 = rows[0][1]
    # 找到 target 当日或之前最近的净值
    nav1 = None
    for dt, nv in rows:
        if dt <= target:
            nav1 = nv
        else:
            break
    days = sum(1 for dt, _ in rows if dt <= target and dt > d)
    if nav1 is None or nav1 <= 0:
        return None, days
    return nav1 / nav0 - 1.0, days


lines = []
lines.append("=" * 104)
lines.append("AI-QFund 历史推荐收益实证（基线报告 v2）")
lines.append("=" * 104)
lines.append(f"{'日期':<11}{'代码':<8}{'名称':<24}{'状态':<8}{'score':<10}{'距今':<6}{'至今收益':<10}{'结算收益'}")
lines.append("-" * 104)

rows_out = []
for date, code, name, _rank, score, regime, status, rr in recs:
    ret, days = fund_ret_to(code, date, GT)
    rows_out.append(dict(date=date, code=code, name=name, status=status, score=score,
                         ret=ret, days=days, rr=rr, regime=regime))
    frr = f"{rr * 100:+.2f}%" if rr is not None else "-"
    lines.append(f"{date:<11}{code:<8}{name:<24}{status:<8}{score:<10.4f}{days:<6}{_fmt_pct(ret):<10}{frr}")

lines.append("")
lines.append("=" * 104)
lines.append("一、已结算（EXIT）真实收益 —— 最有说服力的证据")
lines.append("=" * 104)
settled = [r for r in rows_out if r["rr"] is not None]
if settled:
    vals = [r["rr"] for r in settled]
    wins = sum(1 for v in vals if v > 0)
    losses = sum(1 for v in vals if v < 0)
    lines.append(f"已结算 {len(vals)} 笔 | 盈利 {wins} 笔 | 亏损 {losses} 笔 | 平 {len(vals) - wins - losses} 笔")
    lines.append(f"胜率: {wins / len(vals):.1%} | 平均收益: {statistics.mean(vals) * 100:+.2f}% | 中位数: {statistics.median(vals) * 100:+.2f}%")
    lines.append(f"平均亏损 -{statistics.mean([v for v in vals if v < 0]) * 100:.2f}% (亏损笔均)")

lines.append("")
lines.append("=" * 104)
lines.append("二、持仓中（HOLD/WARNING）至今浮盈（推荐日→9-03）")
lines.append("=" * 104)
holding = [r for r in rows_out if r["status"] in ("HOLD", "WARNING") and r["ret"] is not None]
if holding:
    vals = [r["ret"] for r in holding]
    wins = sum(1 for v in vals if v > 0)
    lines.append(f"持仓中 {len(vals)} 笔 | 浮盈 {wins} 笔 | 浮亏 {len(vals) - wins} 笔 | 平均 {statistics.mean(vals) * 100:+.2f}%")
    lines.append(f"平均持仓天数: {statistics.mean(r['days'] for r in holding):.0f} 交易日")

lines.append("")
lines.append("=" * 104)
lines.append("三、全部推荐（至今收益口径，不分状态）")
lines.append("=" * 104)
allr = [r for r in rows_out if r["ret"] is not None]
if allr:
    vals = [r["ret"] for r in allr]
    wins = sum(1 for v in vals if v > 0)
    lines.append(f"样本 {len(allr)} | 正收益 {wins} ({wins / len(allr):.0%}) | 平均 {statistics.mean(vals) * 100:+.2f}%")

lines.append("")
lines.append("=" * 104)
lines.append("四、预测分质量")
lines.append("=" * 104)
neg_rec = [r for r in rows_out if r["score"] is not None and r["score"] <= 0]
lines.append(f"预测分<=0 仍被推荐的笔数: {len(neg_rec)}")
for r in neg_rec:
    lines.append(f"  {r['date']} {r['code']} score={r['score']:.4f} 至今收益={_fmt_pct(r['ret'], dash='-')}")
pairs = [(r["score"], r["ret"]) for r in rows_out if r["ret"] is not None and r["score"] is not None]
if len(pairs) >= 5:
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / len(xs)
    sx, sy = statistics.pstdev(xs), statistics.pstdev(ys)
    ic = cov / (sx * sy) if sx and sy else 0
    lines.append(f"score 与至今收益皮尔逊相关(IC): {ic:+.3f}")
    # 分组看：score 高/低分组的实际收益
    ys_sorted = sorted(pairs, key=lambda p: p[0])
    hi = ys_sorted[len(ys_sorted) // 2:]
    lo = ys_sorted[:len(ys_sorted) // 2]
    lines.append(f"高分组(score 前50%)平均至今收益: {statistics.mean(y for _, y in hi) * 100:+.2f}%")
    lines.append(f"低分组(score 后50%)平均至今收益: {statistics.mean(y for _, y in lo) * 100:+.2f}%")

lines.append("")
lines.append("=" * 104)
lines.append("五、重复推荐")
lines.append("=" * 104)
dup = Counter(r["code"] for r in rows_out)
for c, n in dup.items():
    if n >= 2:
        names = {r["name"] for r in rows_out if r["code"] == c}
        rets = [f"{r['ret'] * 100:+.1f}%" for r in rows_out if r["code"] == c and r["ret"] is not None]
        lines.append(f"  {c} {'/'.join(names)} 被推荐 {n} 次 | 至今收益: {', '.join(rets)}")

lines.append("")
lines.append("=" * 104)
lines.append("六、按月+按regime")
lines.append("=" * 104)
for m in sorted(set(r["date"][:7] for r in rows_out)):
    vals = [r["ret"] for r in rows_out if r["date"][:7] == m and r["ret"] is not None]
    if vals:
        lines.append(f"  {m}: n={len(vals)} 至今收益平均 {statistics.mean(vals) * 100:+.2f}%")
reg = defaultdict(list)
for r in rows_out:
    if r["ret"] is not None:
        reg[r["regime"]].append(r["ret"])
for k, v in reg.items():
    lines.append(f"  regime={k}: n={len(v)} 平均 {statistics.mean(v) * 100:+.2f}%")

out = "\n".join(lines)
with open("data/reco_backtest_report.txt", "w", encoding="utf-8") as f:
    f.write(out)
print(out)

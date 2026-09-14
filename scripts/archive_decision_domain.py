"""把 1.x 决策域表导出为可入版本管理的归档文件。

背景：2.0 会 DROP 这些表。它们的业务信息量近乎为零（几十行到一万行），但它们是
1.x 唯一的行为证据——「系统当时推荐了什么、为什么、事后如何」。删除前留下归档，
体积可控，且进 git 后可随代码一起回滚。

策略：小表直接导出 CSV（要能直接读），大表 gzip（只为可恢复）。
同时导出 1.x 全库 schema 快照（`data/schema.sql` 随后会被 2.0 改写）。

用法：
    python scripts/archive_decision_domain.py --out docs/archive/1.x-decision-domain
"""

from __future__ import annotations

import argparse
import csv
import gzip
import shutil
import sqlite3
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_SRC = Path("data/qfund.db")

# Q22 认定的删除对象：1.x 决策域全部表 + 风格跟踪 + 板块快照 + 宏观新闻
TABLES = [
    "empty_recommendations",
    "evolution_insights",
    "fund_style_track",
    "llm_audit",
    "macro_news",
    "monitor_events",
    "monitor_scores",
    "purchase_restrictions",
    "quality_metrics",
    "recommend_log",
    "sector_daily_snapshot",
    "sector_selections",
    "system_logs",
]

# 小于此值保持明文（归档表的价值在于能被直接阅读），大于则 gzip
GZIP_THRESHOLD = 512 * 1024


def _export_table(conn: sqlite3.Connection, table: str, out_dir: Path) -> tuple[int, Path]:
    cur = conn.execute(f'SELECT * FROM "{table}"')
    cols = [d[0] for d in cur.description]
    raw_path = out_dir / f"{table}.csv"
    n = 0
    with raw_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for row in cur:
            w.writerow(row)
            n += 1
    if raw_path.stat().st_size >= GZIP_THRESHOLD:
        gz_path = out_dir / f"{table}.csv.gz"
        with raw_path.open("rb") as src, gzip.open(gz_path, "wb") as dst:
            shutil.copyfileobj(src, dst)
        raw_path.unlink()
        return n, gz_path
    return n, raw_path


def main() -> None:
    ap = argparse.ArgumentParser(description="导出 1.x 决策域归档")
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC, help=f"源库（默认 {DEFAULT_SRC}）")
    ap.add_argument("--out", type=Path, required=True, help="归档输出目录")
    args = ap.parse_args()

    if not args.src.exists():
        sys.exit(f"源库不存在：{args.src}")
    args.out.mkdir(parents=True, exist_ok=True)

    uri = f"file:{args.src.as_posix()}?mode=ro"
    rows: list[tuple[str, int, Path]] = []
    with sqlite3.connect(uri, uri=True) as conn:
        present = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for table in TABLES:
            if table not in present:
                print(f"跳过（表不存在）：{table}")
                continue
            rows.append((table, *_export_table(conn, table, args.out)))

        # schema 快照：2.0 会改写 data/schema.sql，1.x 的结构必须留档
        schema_path = args.out / "schema-1.x.sql"
        with schema_path.open("w", encoding="utf-8") as f:
            for (sql,) in conn.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type DESC, name"
            ):
                f.write(sql.rstrip().rstrip(";") + ";\n\n")

    print(f"{'表':<26}{'行数':>9}  归档文件")
    for table, n, path in rows:
        print(f"{table:<26}{n:>9,}  {path.name}")
    print(f"\n共 {len(rows)} 张表，{sum(n for _, n, _ in rows):,} 行 → {args.out}")
    print(f"schema 快照：{schema_path.name}")


if __name__ == "__main__":
    main()

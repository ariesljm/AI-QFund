"""SQLite 全库备份与校验。

用途：1.x 冻结点的回滚保险。代码可以由 git 回滚，数据不能——`DROP TABLE` 与
`DELETE` 不产生 diff。因此动任何删除之前必须有一份可校验的全库快照。

备份用 `VACUUM INTO` 生成一致性单文件快照（在事务外读源库，顺带整理碎片，不
依赖 WAL 文件是否在场）。校验对比源库与备份的逐表行数，并对备份跑
`integrity_check`；任何不一致都以非零退出码结束。

用法：
    python scripts/db_backup.py --dst /path/to/qfund.db          # 备份 + 校验
    python scripts/db_backup.py --dst /path/to/qfund.db --verify-only
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

DEFAULT_SRC = Path("data/qfund.db")

# Windows 控制台默认 GBK，中文与符号会乱码/报错；强制 UTF-8 并容错
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    names = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    return {name: conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in names}


def _backup(src: Path, dst: Path, force: bool) -> None:
    if not src.exists():
        sys.exit(f"源库不存在：{src}")
    if dst.exists() and not force:
        sys.exit(f"目标已存在（拒绝覆盖，如需覆盖请加 --force）：{dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    # 源库以只读方式打开，避免备份动作本身改动源库
    uri = f"file:{src.as_posix()}?mode=ro"
    started = time.monotonic()
    with sqlite3.connect(uri, uri=True) as conn:
        conn.execute("VACUUM INTO ?", (str(dst),))
    print(f"备份完成：{dst}（{dst.stat().st_size / 1e6:.1f} MB，耗时 {time.monotonic() - started:.1f}s）")


def _verify(src: Path, dst: Path) -> None:
    if not dst.exists():
        sys.exit(f"备份不存在：{dst}")
    with sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True) as s:
        src_counts = _table_counts(s)
    with sqlite3.connect(f"file:{dst.as_posix()}?mode=ro", uri=True) as d:
        integrity = d.execute("PRAGMA integrity_check").fetchone()[0]
        dst_counts = _table_counts(d)

    if integrity != "ok":
        sys.exit(f"integrity_check 失败：{integrity}")

    problems: list[str] = []
    for name in sorted(set(src_counts) | set(dst_counts)):
        a, b = src_counts.get(name), dst_counts.get(name)
        if a != b:
            problems.append(f"  {name}: 源库 {a} vs 备份 {b}")

    total = sum(dst_counts.values())
    print(f"integrity_check: ok；表数 {len(dst_counts)}；总行数 {total:,}")
    if problems:
        sys.exit("行数不一致：\n" + "\n".join(problems))
    print("逐表行数一致 [OK]")


def main() -> None:
    ap = argparse.ArgumentParser(description="SQLite 全库备份与校验")
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC, help=f"源库（默认 {DEFAULT_SRC}）")
    ap.add_argument("--dst", type=Path, required=True, help="备份目标文件")
    ap.add_argument("--force", action="store_true", help="允许覆盖已存在的备份")
    ap.add_argument("--verify-only", action="store_true", help="只校验，不备份")
    args = ap.parse_args()

    if not args.verify_only:
        _backup(args.src, args.dst, args.force)
    _verify(args.src, args.dst)


if __name__ == "__main__":
    main()

"""按 data/schema.sql 创建 SQLite 数据库（WAL 模式）。

运行：uv run python init_db.py
"""

import os
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "qfund.db")
SCHEMA_PATH = os.path.join(BASE_DIR, "data", "schema.sql")


def main():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL;")
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        con.executescript(f.read())
    con.commit()
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )]
    con.close()
    print(f"数据库已创建：{DB_PATH}")
    print(f"表数量：{len(tables)} -> {tables}")


if __name__ == "__main__":
    main()

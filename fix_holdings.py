"""清理 fund_holdings 中用错误编码写入的损坏数据，然后重新下载"""
import sqlite3

conn = sqlite3.connect("data/qfund.db")
cur = conn.cursor()

# 检测损坏数据：stock_name 中包含非法的 UTF-8 字节序列
cur.execute("SELECT rowid, stock_name FROM fund_holdings")
bad_ids = []
total = 0
for rowid, name in cur.fetchall():
    total += 1
    if not name:
        continue
    try:
        name.encode('utf-8')
    except UnicodeEncodeError:
        bad_ids.append(rowid)

print(f'总记录: {total}, 损坏记录: {len(bad_ids)}')

if bad_ids:
    # 分批删除
    batch = 500
    for i in range(0, len(bad_ids), batch):
        chunk = bad_ids[i:i+batch]
        cur.execute(
            "DELETE FROM fund_holdings WHERE rowid IN ({})".format(
                ",".join("?" * len(chunk))
            ),
            chunk,
        )
    conn.commit()
    print(f'已删除 {len(bad_ids)} 条损坏记录')

cur.execute("SELECT COUNT(*), COUNT(DISTINCT code) FROM fund_holdings")
cnt, funds = cur.fetchone()
print(f'清理后: {cnt} 条记录, {funds} 只基金')

# 列出需要重新下载的基金
cur.execute("""
    SELECT DISTINCT r.code 
    FROM recommend_log r 
    WHERE NOT EXISTS (
        SELECT 1 FROM fund_holdings h WHERE h.code = r.code
    )
    ORDER BY r.recommend_date DESC
""")
need_dl = [r[0] for r in cur.fetchall()]
if need_dl:
    print(f'需要重新下载持仓的推荐基金: {need_dl[:5]}')

conn.close()
print('完成')
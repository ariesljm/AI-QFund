"""检测并修复 fund_holdings 中的双编码数据"""
import sqlite3

conn = sqlite3.connect("data/qfund.db")
cur = conn.cursor()

# 查找最新的推荐基金
cur.execute("SELECT code, name FROM recommend_log ORDER BY recommend_date DESC LIMIT 1")
code, fund_name = cur.fetchone()
print(f'最新推荐: {code} {fund_name}')

cur.execute(
    "SELECT stock_code, stock_name FROM fund_holdings WHERE code=? "
    "AND report_date = (SELECT MAX(report_date) FROM fund_holdings WHERE code=?) "
    "ORDER BY weight DESC LIMIT 10",
    (code, code),
)

fixed_count = 0
for stock_code, stock_name in cur.fetchall():
    # 尝试 Latin-1 → GBK 修复
    try:
        raw_bytes = stock_name.encode('latin-1')
        fixed = raw_bytes.decode('gbk')
        # 检查修复后是否是纯中文
        if all('\u4e00' <= c <= '\u9fff' or c in '()（）- ' for c in fixed):
            print(f'{stock_code}: {stock_name} → {fixed} (双编码，需修复)')
        else:
            print(f'{stock_code}: {stock_name} → {fixed} (修复后仍异常)')
    except (UnicodeEncodeError, UnicodeDecodeError):
        try:
            # 直接检查能否正确显示
            if any('\u4e00' <= c <= '\u9fff' for c in stock_name):
                print(f'{stock_code}: {stock_name} (已正常)')
            else:
                print(f'{stock_code}: {stock_name} (异常，非中文)')
        except:
            print(f'{stock_code}: (编码异常)')
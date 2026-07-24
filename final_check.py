"""直接检查 web/app.py 相同的持仓查询逻辑，逐字节分析"""
import sqlite3, json

conn = sqlite3.connect('data/qfund.db')
cur = conn.cursor()

# 获取最新推荐的基金代码
cur.execute("SELECT code FROM recommend_log ORDER BY recommend_date DESC LIMIT 1")
code = cur.fetchone()[0]
print(f'最新推荐: {code}')

# 与 web/app.py 完全相同的查询
cur.execute("""
    SELECT h.stock_code, h.stock_name, h.weight, i.industry_name 
    FROM fund_holdings h 
    LEFT JOIN stock_industry_map i ON h.stock_code = i.stock_code 
    WHERE h.code=? AND h.report_date = (
      SELECT MAX(report_date) FROM fund_holdings WHERE code=?)
    ORDER BY h.weight DESC LIMIT 10
""", (code, code))

rows = cur.fetchall()
print(f'查询到 {len(rows)} 条\n')

for i, (stock_code, stock_name, weight, industry) in enumerate(rows):
    # 逐字节检查
    name_hex = stock_name.encode('utf-8').hex()
    ind_hex = (industry or '').encode('utf-8').hex()
    
    has_cn = any('\u4e00' <= c <= '\u9fff' for c in (stock_name or ''))
    has_fffd = '\ufffd' in (stock_name or '')
    
    print(f'{i+1}. {stock_code} | name={stock_name} | weight={weight}%')
    print(f'   name_hex: {name_hex}')
    print(f'   has_cn={has_cn} has_fffd={has_fffd}')
    print(f'   industry: {industry}')
    print(f'   ind_hex: {ind_hex}')
    print()
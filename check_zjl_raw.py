from fetch import fetch
import json

url = 'https://push2ex.eastmoney.com/getAllBKChanges?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wzchanges&pageindex=0&pagesize=5'
resp = fetch(url, timeout=10)
raw = resp.text
print(f'Status: {resp.status_code}')
print(f'Content-Type: {resp.headers.get("Content-Type")}')
print(f'Length: {len(raw)}')

data = json.loads(raw)
print(f'rc: {data.get("rc")}')
print(f'rt: {data.get("rt")}')
print(f'data type: {type(data.get("data"))}')

d = data.get('data')
if d:
    allbk = d.get('allbk', [])
    print(f'\ntotal: {d.get("total", 0)}, allbk count: {len(allbk)}')
    if allbk:
        # 打印原始字段名
        print(f'\nFields in first item: {list(allbk[0].keys())}')
        print(f'\nTop 5 by zjl:')
        for b in sorted(allbk, key=lambda x: float(x.get("zjl", 0) or 0), reverse=True)[:5]:
            print(f'  {b["n"]}: zjl={b.get("zjl")}, u={b.get("u")}, f={b.get("f12")}')
    else:
        print('allbk is empty')
else:
    print('data is None - 盘前无数据')
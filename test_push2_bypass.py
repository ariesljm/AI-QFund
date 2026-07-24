"""使用 fetch.py 测试 push2（绕过代理）"""
from fetch import fetch, _fetch_regular
import json

# 使用fetch.py (trust_env=False)
r = fetch('https://push2.eastmoney.com/api/qt/clist/get',
    {"pn":"1","pz":"5","po":"1","np":"1","fltt":"2","invt":"2",
     "fid":"f3","fs":"m:90+s:4","fields":"f12,f14,f3,f62"},
    timeout=20)

d = r.json()
total = d.get("data",{}).get("total",0)
diff = d.get("data",{}).get("diff",[])
print(f"push2 total={total} diff_count={len(diff)}")
for item in diff:
    print(f"  {json.dumps(item, ensure_ascii=False)}")

"""探测 push2ex 数据格式"""
import os, json, urllib3
urllib3.disable_warnings()
os.environ["NO_PROXY"] = "*"

import requests
s = requests.Session()
s.trust_env = False
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# push2ex 完整数据
r = s.get("https://push2ex.eastmoney.com/getAllBKChanges",
          params={"ut": "7eea3edcaed734bea9cbfc24409ed989",
                  "dpt": "wzchanges", "pageindex": "0", "pagesize": "10"},
          headers=H, timeout=15, verify=False)
d = r.json()
print("push2ex getAllBKChanges:")
print(f"  rc={d.get('rc')} data_type={type(d.get('data')).__name__}")
data = d.get("data")
if data:
    if isinstance(data, dict):
        print(f"  data_keys={list(data.keys())[:10]}")
        for k in data:
            v = data[k]
            if isinstance(v, list):
                print(f"  {k}: list[{len(v)}]")
                if v and isinstance(v[0], (list, dict)):
                    s2 = json.dumps(v[0], ensure_ascii=False)[:300]
                    print(f"    first: {s2}")
            elif isinstance(v, dict):
                print(f"  {k}: dict keys={list(v.keys())[:10]}")
            else:
                print(f"  {k}: {type(v).__name__}={str(v)[:100]}")

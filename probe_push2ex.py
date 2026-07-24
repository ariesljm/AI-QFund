"""探测 push2ex 端点 — 行业板块数据"""
import requests, json, urllib3
urllib3.disable_warnings()

s = requests.Session()
s.trust_env = False
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
     "Referer": "https://quote.eastmoney.com/changes/"}

# push2ex 已知可用的端点
try:
    r = s.get("https://push2ex.eastmoney.com/getAllBKChanges",
              headers=H, timeout=15, verify=False)
    d = r.json()
    print(f"push2ex/getAllBKChanges: status={r.status_code} total={len(d.get('data', d))}")
    if isinstance(d, dict):
        for k in list(d.keys())[:10]:
            v = d[k]
            if isinstance(v, list):
                print(f"  {k}: list[{len(v)}]")
                if v and isinstance(v[0], dict):
                    print(f"    first_keys: {list(v[0].keys())[:15]}")
                    print(f"    first: {json.dumps(v[0], ensure_ascii=False)[:300]}")
            elif isinstance(v, dict):
                print(f"  {k}: dict keys={list(v.keys())[:10]}")
            else:
                print(f"  {k}: {type(v).__name__} = {str(v)[:100]}")
except Exception as e:
    print(f"FAIL: {e}")

# push2ex clist (类似 push2 但可能不同)
try:
    r = s.get("https://push2ex.eastmoney.com/api/qt/clist/get",
              params={"pn": "1", "pz": "5", "po": "1", "np": "1",
                       "fid": "f3", "fs": "m:90+s:4",
                       "fields": "f12,f14,f3,f62"},
              headers=H, timeout=15, verify=False)
    d = r.json()
    total = d.get("data", {}).get("total", 0)
    diff = d.get("data", {}).get("diff", [])
    print(f"\npush2ex clist m:90+s:4: status={r.status_code} total={total}")
    for row in diff[:5]:
        print(f"  {json.dumps(row, ensure_ascii=False)}")
except Exception as e:
    print(f"FAIL: {e}")

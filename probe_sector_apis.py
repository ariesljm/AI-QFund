"""探测申万行业板块 API 备用方案"""
import json
import sys
import requests
import urllib3
urllib3.disable_warnings()

s = requests.Session()
s.trust_env = False
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
     "Referer": "https://data.eastmoney.com/bkzj/hy.html"}

def test(name, url, params=None, timeout=12):
    try:
        r = s.get(url, params=params, headers=H, timeout=timeout)
        print(f"\n=== {name} ===")
        print(f"  Status: {r.status_code}, Size: {len(r.text)}")
        d = r.json()
        total = d.get("data", {}).get("total", "N/A")
        diff = d.get("data", {}).get("diff", [])
        print(f"  Total: {total}, Items: {len(diff)}")
        for item in diff[:3]:
            print(f"    {json.dumps(item, ensure_ascii=False)}")
        return True
    except Exception as e:
        print(f"  FAILED: {e}")
        return False

# 1) push2 m:90 (no +s:4)
test("push2 fs=m:90 (no +s:4)",
     "https://push2.eastmoney.com/api/qt/clist/get",
     {"pn": "1", "pz": "5", "po": "1", "np": "1", "fltt": "2", "invt": "2",
      "fid": "f3", "fs": "m:90", "fields": "f12,f14,f3,f62"})

# 2) push2 m:90+t:2 (地域板块)
test("push2 fs=m:90+t:2 (地域)",
     "https://push2.eastmoney.com/api/qt/clist/get",
     {"pn": "1", "pz": "5", "po": "1", "np": "1", "fltt": "2", "invt": "2",
      "fid": "f3", "fs": "m:90+t:2", "fields": "f12,f14,f3"})

# 3) push2 m:90+t:3 (概念板块)
test("push2 fs=m:90+t:3 (概念)",
     "https://push2.eastmoney.com/api/qt/clist/get",
     {"pn": "1", "pz": "5", "po": "1", "np": "1", "fltt": "2", "invt": "2",
      "fid": "f3", "fs": "m:90+t:3", "fields": "f12,f14,f3"})

# 4) push2his K线 (secid=90.BKxxxx)
test("push2his K-line secid=90.BK0737",
     "https://push2his.eastmoney.com/api/qt/stock/kline/get",
     {"secid": "90.BK0737", "fields1": "f1,f2,f3,f4,f5,f6",
      "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
      "klt": "101", "fqt": "1", "end": "20500101", "lmt": "3"})

# 5) push2his K线 (secid=1.xxxxxx - 个股格式)
test("push2his K-line secid=1.600519",
     "https://push2his.eastmoney.com/api/qt/stock/kline/get",
     {"secid": "1.600519", "fields1": "f1,f2,f3,f4,f5,f6",
      "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
      "klt": "101", "fqt": "1", "end": "20500101", "lmt": "2"})

# 6) emweb stock list (已知可用)
test("emweb stock list",
     "https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax",
     {"code": "600519"})

# 7) datacenter (try)
test("datacenter-web",
     "https://datacenter-web.eastmoney.com/api/data/v1/get",
     {"reportName": "RPT_BK_BOARDRANK", "sortColumns": "CHANGE_RATE",
      "sortTypes": "-1", "pageSize": "5", "pageNumber": "1"})

# 8) 另一种 quote API
test("quote.eastmoney.com",
     "https://quote.eastmoney.com/api/qt/clist/get",
     {"pn": "1", "pz": "5", "po": "1", "np": "1", "fltt": "2", "invt": "2",
      "fid": "f3", "fs": "m:90+t:2", "fields": "f12,f14,f3"})

# 9) push2 原始 URL（控制字段数量）
test("push2 minimal fields",
     "https://push2.eastmoney.com/api/qt/clist/get",
     {"pn": "1", "pz": "3", "po": "1", "np": "1",
      "fid": "f3", "fs": "m:90+t:2", "fields": "f12,f14,f3"})

print("\n=== SUMMARY ===")

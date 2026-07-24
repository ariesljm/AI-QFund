"""探测 push2 变体 + clist JSONP 模式"""
import os, json, urllib3
urllib3.disable_warnings()
os.environ["NO_PROXY"] = "*"

import requests
s = requests.Session()
s.trust_env = False
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"}

def t(label, url, params=None, timeout=12):
    try:
        r = s.get(url, params=params, headers=H, timeout=timeout)
        if r.status_code == 200:
            print(f"OK {label}: {len(r.text)}b", end=" ")
            try:
                d = r.json()
                if isinstance(d, dict) and "data" in d:
                    dd = d["data"]
                    if isinstance(dd, dict):
                        print(f"total={dd.get('total','?')} diff={len(dd.get('diff',[]))}")
                        for row in dd.get("diff", [])[:2]:
                            print(f"  {json.dumps(row, ensure_ascii=False)[:200]}")
                    elif isinstance(dd, list):
                        print(f"list[{len(dd)}]")
                else:
                    print(f"keys={list(d.keys())[:8]}")
            except ValueError:
                print(f"text: {r.text[:150]}")
        else:
            print(f"FAIL {label}: HTTP{r.status_code}")
        return True
    except Exception as e:
        print(f"FAIL {label}: {type(e).__name__} {str(e)[:80]}")
        return False

# 1) clist JSONP 模式 (cb=jQuery)
t("clist JSONP", "https://push2.eastmoney.com/api/qt/clist/get",
  {"pn":"1","pz":"3","fid":"f3","fs":"m:90+s:4",
   "fields":"f12,f14,f3","ut":"7eea3edcaed734bea9cbfc24409ed989",
   "cb":"jQuery", "po":"1", "np":"1", "fltt":"2", "invt":"2"})

# 2) 使用 push2 但加 ut 参数 (push2ex 的方式)
t("clist +ut", "https://push2.eastmoney.com/api/qt/clist/get",
  {"pn":"1","pz":"3","fid":"f3","fs":"m:90",
   "fields":"f12,f14,f3","ut":"7eea3edcaed734bea9cbfc24409ed989"})

# 3) fs 用简单格式 "m:90"
t("clist m:90 simple", "https://push2.eastmoney.com/api/qt/clist/get",
  {"pn":"1","pz":"3","fid":"f3","fs":"m:90",
   "fields":"f12,f14,f3"})

# 4) 换个 referer
H2 = H.copy()
H2["Referer"] = "https://data.eastmoney.com/bkzj/hy.html"
t("clist +Referer", "https://push2.eastmoney.com/api/qt/clist/get",
  {"pn":"1","pz":"3","fid":"f3","fs":"m:90+s:4","fields":"f12,f14,f3"})

# 5) 试 push2 的其他端口/路径
t("push2 stock get", "https://push2.eastmoney.com/api/qt/stock/get",
  {"secid":"90.BK0596","fields":"f12,f14,f3,f57,f58"})

# 6) push2ex 获取更多 allBKChanges 数据
r2 = s.get("https://push2ex.eastmoney.com/getAllBKChanges",
           params={"ut": "7eea3edcaed734bea9cbfc24409ed989",
                   "dpt": "wzchanges", "pageindex": "0", "pagesize": "500"},
           headers=H, timeout=15)
d2 = r2.json()
data = d2.get("data", {})
allbk = data.get("allbk", [])
print(f"\npush2ex allBKChanges: tc={data.get('tc')} rows={len(allbk)}")
# 按名称筛选 industry related
ind_code_start = ["BK04", "BK07", "BK10", "BK12"]  # 申万行业代码范围
for bk in allbk[:20]:
    print(f"  {bk.get('c')} m={bk.get('m')} {bk.get('n')} 涨跌={bk.get('u')}% 资金={bk.get('zjl')}")

print("\n=== DONE ===")

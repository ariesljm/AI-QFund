"""探测 push2ex 正确参数 + 全局绕过代理"""
import os, json, urllib3
urllib3.disable_warnings()

# 全局绕过代理
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"
os.environ["HTTP_PROXY"] = ""
os.environ["HTTPS_PROXY"] = ""
os.environ["http_proxy"] = ""
os.environ["https_proxy"] = ""

import requests
s = requests.Session()
s.trust_env = False
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
     "Referer": "https://quote.eastmoney.com/changes/"}

def t(label, url, params=None, timeout=12):
    try:
        r = s.get(url, params=params, headers=H, timeout=timeout, verify=False)
        print(f"OK {label}: HTTP{r.status_code} {len(r.text)}b")
        if r.text and len(r.text) < 2000:
            print(f"  {r.text[:500]}")
        elif r.text:
            try:
                d = r.json()
                if isinstance(d, dict):
                    print(f"  keys={list(d.keys())[:10]}")
                    if "data" in d:
                        dd = d["data"]
                        if isinstance(dd, dict):
                            print(f"  data.total={dd.get('total','?')} diff={len(dd.get('diff',[]))}")
                            for row in dd.get("diff", [])[:3]:
                                print(f"  {json.dumps(row, ensure_ascii=False)[:200]}")
                        elif isinstance(dd, list):
                            print(f"  data[{len(dd)}] {json.dumps(dd[0], ensure_ascii=False)[:200] if dd else '[]'}")
                        else:
                            print(f"  data={json.dumps(dd, ensure_ascii=False)[:200]}")
                    elif "result" in d:
                        print(f"  result={json.dumps(d['result'], ensure_ascii=False)[:200]}")
                    else:
                        print(f"  {json.dumps(d, ensure_ascii=False)[:200]}")
                elif isinstance(d, list):
                    print(f"  list[{len(d)}] {json.dumps(d[0], ensure_ascii=False)[:200] if d else '[]'}")
                else:
                    print(f"  type={type(d)}")
            except ValueError:
                print(f"  text: {r.text[:200]}")
        return True
    except Exception as e:
        print(f"FAIL {label}: {type(e).__name__} {str(e)[:100]}")
        return False

# 1) push2ex 带 akshare 参数
t("push2ex allBKChanges", "https://push2ex.eastmoney.com/getAllBKChanges",
  {"ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wzchanges",
   "pageindex": "0", "pagesize": "20", "_": "1753189100000"})

# 2) push2ex 也试试 clist（push2 类似路径）
t("push2ex clist", "https://push2ex.eastmoney.com/api/qt/clist/get",
  {"pn":"1","pz":"3","po":"1","np":"1","fid":"f3","fs":"m:90+s:4",
   "fields":"f12,f14,f3","ut":"7eea3edcaed734bea9cbfc24409ed989"})

# 3) 原 push2 也再试试（绕过代理后也许可以）
t("push2 clist", "https://push2.eastmoney.com/api/qt/clist/get",
  {"pn":"1","pz":"3","po":"1","np":"1","fid":"f3","fs":"m:90+s:4",
   "fields":"f12,f14,f3","ut":"7eea3edcaed734bea9cbfc24409ed989"})

# 4) push2 用 emweb 方式
t("push2 ulist", "https://push2.eastmoney.com/api/qt/ulist.np/get",
  {"fltt":"2","secids":"1.000001,0.399001",
   "fields":"f1,f2,f3,f4,f6,f12,f13","ut":"7eea3edcaed734bea9cbfc24409ed989",
   "cb":"jQuery"})

# 5) 同花顺 10jqka API
t("10jqka board", "https://q.10jqka.com.cn/index/index/board/all/field/zdf/order/desc/page/1/ajax/1/")

print("\n=== DONE ===")

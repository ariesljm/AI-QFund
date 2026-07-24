"""探测API v6 — 每个请求独立，简洁输出"""
import requests, json, urllib3, sys, subprocess
urllib3.disable_warnings()

s = requests.Session()
s.trust_env = False
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

ok_count = 0
fail_count = 0

def t(label, url, params=None, timeout=10):
    global ok_count, fail_count
    try:
        r = s.get(url, params=params, headers=H, timeout=timeout)
        ct = r.headers.get("Content-Type", "")
        print(f"OK {label}: HTTP{r.status_code} {len(r.text)}b {ct[:30]}", flush=True)
        ok_count += 1
        if "json" in ct and len(r.text) > 2:
            try:
                d = r.json()
                s2 = json.dumps(d, ensure_ascii=False)[:250]
                print(f"  {s2}", flush=True)
            except: pass
        elif r.text[:2] in ("[{" ,'{"', '{"'):
            print(f"  {r.text[:250]}", flush=True)
        return True
    except Exception as e:
        print(f"FAIL {label}: {type(e).__name__} {str(e)[:80]}", flush=True)
        fail_count += 1
        return False

# ---- 非 push2 端点 ----

t("datacenter-web BKRANK",
  "https://datacenter-web.eastmoney.com/api/data/v1/get",
  {"reportName": "RPT_BK_BKRANK", "columns": "ALL",
   "sortColumns": "CHANGE_RATE", "sortTypes": "-1",
   "pageSize": "5", "pageNumber": "1", "source": "WEB", "client": "WEB"})

t("emdatah5 block",
  "https://emdatah5.eastmoney.com/dc/zjlx/block",
  {"type": "hy", "order": "desc", "sort": "mainNetInflow", "page": "1", "pageSize": "3"})

t("search-api-web",
  "https://search-api-web.eastmoney.com/search/jsonp",
  {"cb": "jQuery", "param": '{"uid":"","keyword":"行业板块","type":["cmsArticleWebOld"],"client":"web","clientType":"web","clientVersion":"curr","param":{"cmsArticleWebOld":{"searchScope":"default","sort":"default","pageIndex":1,"pageSize":3,"preTag":" ","postTag":" "}}}'})

t("emweb hsf10",
  "https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax",
  {"code": "600519"})

# ---- push2 通过不同方式 ----

# curl.exe with HTTP/2 forced
try:
    cmd = [
        "curl.exe", "-4", "-s", "-m", "10", "--http2-prior-knowledge",
        "-H", "Accept: */*",
        "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=3&po=1&np=1&fid=f3&fs=m:90+s:4&fields=f12,f14,f3"
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15, creationflags=0x08000000)
    print(f"curl-http2: rc={r.returncode} stdout={len(r.stdout)}b stderr={len(r.stderr)}b")
    if r.stdout:
        try:
            d = json.loads(r.stdout)
            print(f"  {json.dumps(d, ensure_ascii=False)[:250]}")
            ok_count += 1
        except:
            print(f"  {r.stdout[:200]}")
            fail_count += 1
except Exception as e:
    print(f"FAIL curl-http2: {e}")
    fail_count += 1

# push2his via curl
try:
    cmd = [
        "curl.exe", "-4", "-s", "-m", "10", "--http2-prior-knowledge",
        "-H", "Accept: */*",
        "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=90.BK0737&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61&klt=101&fqt=1&end=20500101&lmt=2"
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15, creationflags=0x08000000)
    print(f"curl-his: rc={r.returncode} stdout={len(r.stdout)}b")
    if r.stdout:
        try:
            d = json.loads(r.stdout)
            print(f"  {json.dumps(d, ensure_ascii=False)[:250]}")
            ok_count += 1
        except:
            print(f"  {r.stdout[:200]}")
            fail_count += 1
except Exception as e:
    print(f"FAIL curl-his: {e}")
    fail_count += 1

print(f"\n---- Summary: {ok_count} OK, {fail_count} FAIL ----")

"""探测非push2 API端点 — v5: 报告API + 移动端 + datacenter + curl直接"""
import requests, json, urllib3, subprocess
urllib3.disable_warnings()

s = requests.Session()
s.trust_env = False
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"}

def test(label, url, params=None, extra_h=None, timeout=10):
    headers = H.copy()
    if extra_h: headers.update(extra_h)
    try:
        r = s.get(url, params=params, headers=headers, timeout=timeout, verify=False)
        status = r.status_code
        size = len(r.text)
        print(f"\n[OK] {label}  HTTP {status}  {size} bytes")
        ctype = r.headers.get("Content-Type","")
        if "json" in ctype or "javascript" in ctype:
            try:
                d = r.json()
                if isinstance(d, dict):
                    if "result" in d and d["result"]:
                        res = d["result"]
                        if isinstance(res, dict):
                            print(f"  pages={res.get('pages','?')} data_rows={len(res.get('data',[]))}")
                            for row in res.get("data", [])[:2]:
                                print(f"  {json.dumps(row, ensure_ascii=False)[:200]}")
                        else:
                            print(f"  result_type={type(res).__name__}")
                    elif "data" in d:
                        dd = d["data"]
                        if isinstance(dd, dict):
                            print(f"  total={dd.get('total','?')} diff={len(dd.get('diff',[]))}")
                            for row in dd.get("diff", [])[:2]:
                                print(f"  {json.dumps(row, ensure_ascii=False)[:200]}")
                        else:
                            print(f"  data={json.dumps(dd, ensure_ascii=False)[:200]}")
                    else:
                        print(f"  keys={list(d.keys())[:8]} preview={json.dumps(d, ensure_ascii=False)[:200]}")
                else:
                    print(f"  json_type={type(d).__name__}")
            except Exception as je:
                print(f"  parse_err: {je} | text: {r.text[:200]}")
        else:
            print(f"  content-type={ctype} preview={r.text[:200]}")
        return True
    except requests.Timeout:
        print(f"\n[FAIL] {label}: TIMEOUT")
        return False
    except Exception as e:
        print(f"\n[FAIL] {label}: {type(e).__name__}: {str(e)[:120]}")
        return False

# ===== 1) datacenter-web 行业板块数据 =====
test("datacenter BKBJ",
     "https://datacenter-web.eastmoney.com/api/data/v1/get",
     {"reportName": "RPT_BK_BKJ_RANK", "columns": "ALL",
      "sortColumns": "CHANGE_RATE", "sortTypes": "-1",
      "pageSize": "5", "pageNumber": "1", "source": "WEB", "client": "WEB"})

test("datacenter HYRANK",
     "https://datacenter-web.eastmoney.com/api/data/v1/get",
     {"reportName": "RPT_DMSK_FN_HYRANK", "columns": "ALL",
      "sortColumns": "CHANGE_RATE", "sortTypes": "-1",
      "pageSize": "5", "pageNumber": "1", "source": "WEB", "client": "WEB"})

# ===== 2) reportapi.eastmoney.com (数据报告) =====
test("reportapi HYRANK",
     "https://reportapi.eastmoney.com/api/data/v1/get",
     {"reportName": "RPT_DMSK_FN_HYRANK", "columns": "ALL",
      "sortColumns": "CHANGE_RATE", "sortTypes": "-1",
      "pageSize": "5", "pageNumber": "1", "source": "WEB", "client": "WEB"})

# ===== 3) 移动端 H5 数据 API =====
test("emdatah5 zjlx",
     "https://emdatah5.eastmoney.com/dc/zjlx/block",
     {"type": "hy", "order": "desc", "sort": "mainNetInflow", "page": "1", "pageSize": "5"},
     timeout=10)

# ===== 4) search-api-web =====
test("search-api-web fund",
     "https://search-api-web.eastmoney.com/search/jsonp",
     {"cb": "jQuery", "param": '{"uid":"","keyword":"行业板块","type":["cmsArticleWebOld"],"client":"web","clientType":"web","clientVersion":"curr","param":{"cmsArticleWebOld":{"searchScope":"default","sort":"default","pageIndex":1,"pageSize":5,"preTag":" ","postTag":" "}}}'})

# ===== 5) 腾讯财经 行业板块 =====
test("tencent board",
     "https://web.ifzq.gtimg.cn/appstock/app/board/getBoardIndex",
     {"board_code": "sh000001", "type": "fiveDay"},
     timeout=10)

# ===== 6) curl.exe 直连 push2 (HTTP/2 + 特定参数) =====
try:
    r = subprocess.run([
        "curl.exe", "-4", "-s", "-m", "12",
        "--http2",
        "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "-H", "Referer: https://data.eastmoney.com/bkzj/hy.html",
        "-H", "Accept: */*",
        "-H", "Accept-Language: zh-CN,zh;q=0.9",
        "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=3&po=1&np=1&fid=f3&fs=m:90+s:4&fields=f12,f14,f3"
    ], capture_output=True, text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
    print(f"\n[curl push2 http2] rc={r.returncode} stdout_len={len(r.stdout)} stderr_len={len(r.stderr)}")
    if r.stdout:
        try:
            d = json.loads(r.stdout)
            total = d.get("data",{}).get("total","?")
            print(f"  total={total}")
        except:
            print(f"  output: {r.stdout[:200]}")
    if r.stderr:
        print(f"  stderr: {r.stderr[:200]}")
except Exception as e:
    print(f"\n[curl push2 http2] FAIL: {e}")

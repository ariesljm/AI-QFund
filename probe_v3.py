"""测非push2备用API — 逐个测试加超时"""
import requests, json, sys

s = requests.Session()
s.trust_env = False
hdr = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

def test(label, url, params=None, extra_h=None, timeout=8):
    headers = hdr.copy()
    if extra_h:
        headers.update(extra_h)
    try:
        r = s.get(url, params=params, headers=headers, timeout=timeout, verify=False)
        print(f"  {label}: HTTP {r.status_code} bytes={len(r.text)}", flush=True)
        ctype = r.headers.get("Content-Type", "")
        if "json" in ctype or "get" in url:
            try:
                d = r.json()
                if isinstance(d, dict):
                    if "result" in d:
                        res = d["result"]
                        if isinstance(res, dict):
                            total = res.get("pages", "?")
                            data_list = res.get("data", [])
                            print(f"    pages={total} rows={len(data_list)}", flush=True)
                            for row in data_list[:3]:
                                print(f"    {json.dumps(row, ensure_ascii=False)[:150]}", flush=True)
                        else:
                            print(f"    result: {type(res)}", flush=True)
                    elif "data" in d:
                        dd = d["data"]
                        if isinstance(dd, dict):
                            total = dd.get("total", "?")
                            diff = dd.get("diff", [])
                            print(f"    total={total} diff={len(diff)}", flush=True)
                            for row in diff[:3]:
                                print(f"    {json.dumps(row, ensure_ascii=False)[:150]}", flush=True)
                        else:
                            print(f"    data={json.dumps(dd, ensure_ascii=False)[:200]}", flush=True)
                    else:
                        print(f"    keys={list(d.keys())[:8]} {json.dumps(d, ensure_ascii=False)[:200]}", flush=True)
                elif isinstance(d, list):
                    print(f"    list[{len(d)}] {json.dumps(d[0], ensure_ascii=False)[:200] if d else '[]'}", flush=True)
            except Exception:
                print(f"    text: {r.text[:300]}", flush=True)
        else:
            print(f"    text: {r.text[:200]}", flush=True)
        sys.stdout.flush()
        return True
    except requests.Timeout:
        print(f"  {label}: TIMEOUT", flush=True)
        return False
    except Exception as e:
        print(f"  {label}: {type(e).__name__}: {str(e)[:120]}", flush=True)
        return False

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 每个测试独立，有单独超时
test("emweb", "https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax",
     {"code": "600519"})

test("fundapi", "https://fundapi.eastmoney.com/fundtradenew.aspx",
     {"ft": "gp", "pi": "1", "pn": "3", "sc": "1", "st": "desc"})

test("datacenter-HYDAILYRANK",
     "https://datacenter-web.eastmoney.com/api/data/v1/get",
     {"reportName": "RPT_DMSK_FN_HYDAILYRANK", "columns": "ALL",
      "sortColumns": "CHANGE_RATE", "sortTypes": "-1",
      "pageSize": "5", "pageNumber": "1", "source": "WEB", "client": "WEB"},
     timeout=12)

test("10jqka-industry", "https://q.10jqka.com.cn/api/industry/industryList",
     timeout=10)

test("sina-hy7500", "https://hq.sinajs.cn/list=hf_CSIHY7500",
     extra_h={"Referer": "https://finance.sina.com.cn"}, timeout=10)

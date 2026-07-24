"""测非push2备用API"""
import requests, json, time

s = requests.Session()
s.trust_env = False
hdr = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

def test(label, url, params=None, extra_h=None, timeout=12):
    try:
        headers = hdr.copy()
        if extra_h:
            headers.update(extra_h)
        r = s.get(url, params=params, headers=headers, timeout=timeout)
        print(f"\n[OK] {label}  status={r.status_code}  size={len(r.text)}")
        ctype = r.headers.get("Content-Type", "")
        if "json" in ctype or url.endswith("get"):
            d = r.json()
            if isinstance(d, dict):
                print(f"  top_keys: {list(d.keys())[:8]}")
                if "result" in d:
                    print(f"  result_keys: {list(d['result'].keys())[:8] if isinstance(d['result'], dict) else type(d['result'])}")
                if "data" in d:
                    dd = d["data"]
                    if isinstance(dd, dict):
                        print(f"  total={dd.get('total','?')}  diff_count={len(dd.get('diff',[]))}")
                        for row in dd.get("diff", [])[:3]:
                            print(f"    {json.dumps(row, ensure_ascii=False)[:120]}")
            elif isinstance(d, list):
                print(f"  list_len={len(d)}  first={json.dumps(d[0], ensure_ascii=False)[:200] if d else 'empty'}")
        elif "htm" in ctype:
            print(f"  html: {r.text[:200]}")
        else:
            print(f"  content: {r.text[:300]}")
        return True
    except Exception as e:
        print(f"\n[FAIL] {label}: {e}")
        return False

# 1) emweb 公司概况
test("emweb-PageAjax", "https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax",
     {"code": "600519"})

# 2) fundapi
test("fundapi", "https://fundapi.eastmoney.com/fundtradenew.aspx",
     {"ft": "gp", "pi": "1", "pn": "3", "sc": "1", "st": "desc"})

# 3) datacenter 行业资金流  
test("datacenter-HYZJLRANK",
     "https://datacenter-web.eastmoney.com/api/data/v1/get",
     {"reportName": "RPT_DMSK_FN_HYZJLRANK", "columns": "ALL",
      "sortColumns": "MAIN_NET_INFLOW", "sortTypes": "-1",
      "pageSize": "5", "pageNumber": "1", "source": "WEB", "client": "WEB"})

# 4) datacenter 行业板块每日涨跌 (Shenwan)
test("datacenter-HYDAILYRANK",
     "https://datacenter-web.eastmoney.com/api/data/v1/get",
     {"reportName": "RPT_DMSK_FN_HYDAILYRANK", "columns": "ALL",
      "sortColumns": "CHANGE_RATE", "sortTypes": "-1",
      "pageSize": "5", "pageNumber": "1", "source": "WEB", "client": "WEB"})

# 5) 同花顺 10jqka 行业列表
test("10jqka-industry", "https://q.10jqka.com.cn/api/industry/industryList")

# 6) 新浪行情 申万行业指数
test("sina-hy7500", "https://hq.sinajs.cn/list=hf_CSIHY7500",
     extra_h={"Referer": "https://finance.sina.com.cn"})

# 7) 新浪批量行业
codes = ["hf_CSIHY7500", "hf_CSIHY7501", "hf_CSIHY7502", "hf_CSIHY7503", "hf_CSIHY7504"]
test("sina-multi", "https://hq.sinajs.cn/list=" + ",".join(codes),
     extra_h={"Referer": "https://finance.sina.com.cn"})

# 8) 东方财富行业板块 GET HTML
test("eastmoney-hy-html", "https://data.eastmoney.com/bkzj/hy.html")

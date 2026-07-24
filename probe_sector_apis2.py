"""探测申万行业板块 API — 使用 fetch.py TLS 伪装 + 备用端点"""
import json, sys, time
import requests
import urllib3
urllib3.disable_warnings()

from fetch import fetch as _fetch, _fetch_regular, _is_push2

H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
     "Referer": "https://data.eastmoney.com/bkzj/hy.html"}

results = {}

def test(name, url, params=None, timeout=15):
    try:
        if _is_push2(url):
            r = _fetch(url, params, timeout)
        else:
            r = _fetch_regular(url, params, timeout)
        data = r.json()
        total = data.get("data", {}).get("total", -1) if isinstance(data.get("data"), dict) else -1
        diff = data.get("data", {}).get("diff", []) if isinstance(data.get("data"), dict) else []
        result = data.get("data", {}) if isinstance(data.get("data"), dict) else {}
        klines = data.get("data", {}).get("klines", None)
        print(f"\n[OK] {name}")
        if total >= 0:
            print(f"  total={total}, diff_count={len(diff)}")
            for item in diff[:3]:
                print(f"  {json.dumps(item, ensure_ascii=False)[:120]}")
        elif klines is not None:
            print(f"  klines_count={len(klines)}")
            for k in klines[:3]:
                print(f"  {k}")
        else:
            print(f"  data_keys={list(result.keys())[:10]}")
            print(f"  preview: {json.dumps(data.get('data'), ensure_ascii=False)[:300]}")
        results[name] = True
        return True
    except Exception as e:
        print(f"\n[FAIL] {name}: {type(e).__name__}: {str(e)[:150]}")
        results[name] = str(e)[:150]
        return False

# === 使用已配置的 fetch.py（TLS 伪装）测试 push2 ===

# 1) 原始申万板块 URL（fs=m:90+s:4）
test("push2 原始 sectors", "https://push2.eastmoney.com/api/qt/clist/get",
     {"pn": "1", "pz": "5", "po": "1", "np": "1", "fltt": "2", "invt": "2",
      "fid": "f3", "fs": "m:90+s:4", "fields": "f12,f14,f3"})

# 2) 无 s:4 分类
test("push2 m:90 (no sub-type)", "https://push2.eastmoney.com/api/qt/clist/get",
     {"pn": "1", "pz": "5", "po": "1", "np": "1",
      "fid": "f3", "fs": "m:90", "fields": "f12,f14,f3"})

# 3) push2his K-line
test("push2his K-line BK0737", "https://push2his.eastmoney.com/api/qt/stock/kline/get",
     {"secid": "90.BK0737", "fields1": "f1,f2,f3,f4,f5,f6",
      "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
      "klt": "101", "fqt": "1", "end": "20500101", "lmt": "3"})

# === 非 push2 备用端点 ===

# 4) emweb 公司概况（已有成功先例）
test("emweb 600519", "https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax",
     {"code": "600519"})

# 5) fundapi（已有成功先例）
test("fundapi fundtradenew", "https://fundapi.eastmoney.com/fundtradenew.aspx",
     {"ft": "gp", "pi": "1", "pn": "3", "sc": "1", "st": "desc"})

# 6) datacenter-web 行业资金流
test("datacenter 行业资金流", "https://datacenter-web.eastmoney.com/api/data/v1/get",
     {"reportName": "RPT_DMSK_FN_HYZJLRANK", "columns": "ALL",
      "sortColumns": "MAIN_NET_INFLOW", "sortTypes": "-1",
      "pageSize": "5", "pageNumber": "1", "source": "WEB", "client": "WEB"})

# 7) datacenter 行业板块涨跌幅
test("datacenter 行业涨跌", "https://datacenter-web.eastmoney.com/api/data/v1/get",
     {"reportName": "RPT_DMSK_FN_HYDAILYRANK", "columns": "ALL",
      "sortColumns": "CHANGE_RATE", "sortTypes": "-1",
      "pageSize": "5", "pageNumber": "1", "source": "WEB", "client": "WEB"})

# 8) 东方财富行情首页JSONP
test("emweb quotes", "https://push2.eastmoney.com/api/qt/clist/get",
     {"pn": "1", "pz": "5", "po": "1", "np": "1",
      "fid": "f3", "fs": "m:90+t:2", "fields": "f12,f14,f3"})

# 9) AKShare 备用（如果可用）
try:
    import akshare as ak
    df = ak.stock_board_industry_name_em()
    print(f"\n[OK] AKShare stock_board_industry_name_em: {len(df)} rows")
    print(df.head(3).to_string())
    results["AKShare"] = True
except ImportError:
    print("\n[SKIP] AKShare not installed")
except Exception as e:
    print(f"\n[FAIL] AKShare: {e}")

# 10) 10jqka (同花顺) 行业板块
try:
    r = requests.get("https://q.10jqka.com.cn/api/industry/industryList", headers=H, timeout=10)
    data = r.json()
    print(f"\n[OK] 10jqka industryList: status={r.status_code}, keys={list(data.keys())[:5]}")
    results["10jqka"] = True
except Exception as e:
    print(f"\n[FAIL] 10jqka: {e}")

print("\n" + "=" * 50)
print("SUMMARY:")
for k, v in results.items():
    status = "OK" if v is True else f"FAIL: {v[:80]}"
    print(f"  {k}: {status}")

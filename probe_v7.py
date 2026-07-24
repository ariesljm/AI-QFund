"""探测 push2ex + 备用 — v7"""
import requests, json, urllib3
urllib3.disable_warnings()

s = requests.Session()
s.trust_env = False
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
     "Referer": "https://quote.eastmoney.com/changes/"}

def t(label, url, params=None, timeout=12):
    try:
        r = s.get(url, params=params, headers=H, timeout=timeout, verify=False)
        print(f"OK {label}: HTTP{r.status_code} {len(r.text)}b")
        if r.text:
            try:
                d = r.json()
                if isinstance(d, dict):
                    if "data" in d and isinstance(d["data"], dict):
                        print(f"  data_total={d['data'].get('total','?')} diff={len(d['data'].get('diff',[]))}")
                        for row in d["data"].get("diff", [])[:2]:
                            print(f"  {json.dumps(row, ensure_ascii=False)[:200]}")
                    elif "result" in d:
                        print(f"  result={json.dumps(d['result'], ensure_ascii=False)[:200]}")
                    else:
                        keys = list(d.keys())
                        if len(d) == 2 and "rc" in d:
                            print(f"  err: {json.dumps(d, ensure_ascii=False)[:200]}")
                        else:
                            print(f"  keys={keys} {json.dumps(d, ensure_ascii=False)[:200]}")
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

# push2ex — 不同路径
t("push2ex getAllBKChanges", "https://push2ex.eastmoney.com/getAllBKChanges")
t("push2ex clist m:90", "https://push2ex.eastmoney.com/api/qt/clist/get",
  {"pn":"1","pz":"3","po":"1","np":"1","fid":"f3","fs":"m:90","fields":"f12,f14,f3"})

# 用 web 页面策略 — 直接抓 HTML
t("quote.eastmoney.com changes", "https://quote.eastmoney.com/changes/")
t("quote.eastmoney.com center", "https://quote.eastmoney.com/center/")

# 10jqka (同花顺)
t("10jqka industry list", "https://q.10jqka.com.cn/thshy/hyts/")
t("10jqka api", "http://q.10jqka.com.cn/index/index/board/all/field/zdf/order/desc/page/1/ajax/1/",
  timeout=10)

# 新浪财经 行业
t("sina finance hy", "https://vip.stock.finance.sina.com.cn/q/go.php/vIndustryRank/kind/hyzs/p/1/num/10/sort/changepercent/asc/0/")

print("\n=== DONE ===")

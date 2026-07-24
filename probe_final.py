"""最终方案：push2ex 获取申万行业板块涨跌幅"""
import os, json, urllib3, re, logging
urllib3.disable_warnings()
os.environ["NO_PROXY"] = "*"

import requests
s = requests.Session()
s.trust_env = False
s.proxies = {"http": None, "https": None}
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

def fetch_all_boards(pagesize=2000):
    """从 push2ex 获取当日所有板块变动数据"""
    r = s.get("https://push2ex.eastmoney.com/getAllBKChanges",
              params={"ut": "7eea3edcaed734bea9cbfc24409ed989",
                      "dpt": "wzchanges", "pageindex": "0",
                      "pagesize": str(pagesize)},
              headers=H, timeout=15)
    d = r.json()
    if d.get("rc") != 0:
        raise RuntimeError(f"push2ex error: rc={d.get('rc')}")
    return d.get("data", {}).get("allbk", [])

def get_industry_names():
    """通过 HTML 页面获取申万行业名称列表（绕过 push2）"""
    # 抓取行业板块页面，从HTML中提取板块代码列表
    r = s.get("https://data.eastmoney.com/bkzj/hy.html", headers=H, timeout=15)
    # 页面中 <a href="/bkzj/BKxxxx.html">行业名</a>
    pattern = re.compile(r'/bkzj/(BK\d{4})\.html">([^<]+)</a>')
    matches = pattern.findall(r.text)
    return {code: name for code, name in matches}

print("===== 方案1：push2ex 全部板块（前30条按涨跌幅排序）=====")
boards = fetch_all_boards()
# 按涨跌幅绝对值排序
boards.sort(key=lambda b: abs(float(b.get("u", 0))), reverse=True)
print(f"总共 {len(boards)} 条板块变动")
for b in boards[:30]:
    print(f"  {b.get('c')} | {b.get('n')} | 涨跌={b.get('u')}% | 资金={b.get('zjl',''):.0f}")

print("\n===== 方案2：从HTML页面提取申万行业列表 =====")
try:
    ind_map = get_industry_names()
    print(f"从HTML提取到 {len(ind_map)} 个行业板块代码")
    # 匹配push2ex数据
    ind_boards = [(b, ind_map[b['c']]) for b in boards if b['c'] in ind_map]
    print(f"在 push2ex 数据中匹配到 {len(ind_boards)} 个行业板块")
    # 按涨跌幅排序
    ind_boards.sort(key=lambda x: abs(float(x[0].get("u", 0))), reverse=True)
    for b, name in ind_boards[:30]:
        print(f"  {b.get('c')} | {name} | 涨跌={b.get('u')}% | 资金={b.get('zjl',''):.0f}")
except Exception as e:
    print(f"FAIL: {e}")

print("\n===== 方案3：按板块代码前缀过滤行业 =====")
# 申万二级行业代码范围：BK04xx ~ BK12xx（不含概念和地域）
def is_industry(board_code):
    """根据板块代码判断是否为申万行业板块"""
    num = int(board_code[2:])  # BK后的数字
    # 申万行业: 400-799, 1000-1299
    return (400 <= num <= 799) or (1000 <= num <= 1299)

ind_boards2 = [b for b in boards if is_industry(b.get("c", ""))]
print(f"代码过滤后 {len(ind_boards2)} 个行业板块")
ind_boards2.sort(key=lambda b: abs(float(b.get("u", 0))), reverse=True)
for b in ind_boards2[:30]:
    print(f"  {b.get('c')} | {b.get('n')} | 涨跌={b.get('u')}% | 资金={b.get('zjl',''):.0f}")

print("\n=== DONE ===")

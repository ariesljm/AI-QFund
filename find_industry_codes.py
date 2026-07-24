"""提取准确的申万二级行业代码列表，并验证 sector_api"""
import os, json, urllib3, re
urllib3.disable_warnings()
os.environ["NO_PROXY"] = "*"

import requests
s = requests.Session()
s.trust_env = False
s.proxies = {"http": None, "https": None}
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# 1) 从 bkzj/hy.html 页面提取行业板块代码
#    页面由 JS 通过 clist fs=m:90+s:4 加载，所以页面上显示的链接就是行业板块
r = s.get("https://data.eastmoney.com/bkzj/hy.html", headers=H, timeout=15)
text = r.text

# 提取页面中行业板块列表: <a href="/bkzj/BKxxxx.html">行业名</a>
# 注意: 页面同时显示行业/概念/地域三个 tab，但通过 JS filter 切换
# 默认显示的是行业 (hy) tab

# 方法: 提取所有 /bkzj/BKxxxx.html 链接
all_links = re.findall(r'/bkzj/(BK\d{4})\.html">([^<]+)</a>', text)
print(f"HTML页面中所有板块链接: {len(all_links)} 个")
for code, name in all_links[:10]:
    print(f"  {code} {name}")

# 2) 区分: 行业代码特征
#    从 list.js 可知: hy="m:90 s:4", gn="m:90 t:3", dy="m:90 t:1"
#    行业板块代码的特征（根据已知数据推断）：
#    申万二级行业 (hy) ~152 个，代码范围松散
#
#    更可靠的方法: 使用 push2ex 获取所有板块，然后通过板块代码特征过滤
#    申万行业代码大概有: BK04xx-BK07xx (经典), BK10xx-BK12xx (新)
#    但要排除: 带"_"后缀的、BK08xx-BK09xx (概念)、BK14xx+（特殊）

# 3) 更好的方法: 从页面 HTML 中提取行业板块 tab 下的数据
#    行业、概念、地域三个 tab 的数据结构不同
#    页面通过 JS filter 切换: <li data-value="hy"> <li data-value="gn"> <li data-value="dy">
#    初始加载的是行业 (hy)

# 找 "hy" filter 相关的 HTML
filter_match = re.search(r'<ul[^>]*id="[^"]*filter_bk[^"]*"[^>]*>(.*?)</ul>', text, re.DOTALL)
if filter_match:
    print(f"\n行业/概念/地域 filter HTML 已找到")

# 4) 尝试通过 board code number 精准区分
#    已知申万二级行业代码列表 (从公开数据整理)
#    以下是根据 AKShare 和 ebdata 整理的申万二级行业代码示例
test_codes = {
    "BK0428": "银行", "BK0451": "房地产开发", "BK0454": "水泥", "BK0465": "化学制药",
    "BK0470": "造纸印刷", "BK0471": "化学纤维", "BK0473": "证券", "BK0474": "保险",
    "BK0475": "银行", "BK0480": "航空航天", "BK0484": "贸易", "BK0538": "化学制品",
    "BK0546": "电力行业", "BK0732": "黄金", "BK0738": "多元金融",
    "BK1015": "能源金属", "BK1016": "汽车整车", "BK1017": "电源集成",  
    "BK1019": "化学原料", "BK1020": "非金属材料", "BK1033": "煤炭行业",
    "BK1036": "半导体", "BK1037": "消费电子", "BK1038": "化学制药",
    "BK1218": "中药", "BK1221": "数字媒体", "BK1253": "医疗美容",
}

# 验证: 检查这些代码是否在 HTML 页面中
found = 0
for code, name in test_codes.items():
    if f"/bkzj/{code}.html" in text:
        found += 1
print(f"\n验证: {found}/{len(test_codes)} 个已知行业代码在 HTML 页面中")

# 5) 最终方案: 从 HTML 中提取的代码作为行业代码集合
#    但要排除概念和地域(概念代码一般在 BK05xx/BK08xx/BK09xx/BK13xx+)
#    地域代码在 BK01xx-BK02xx
    
industry_codes_from_html = {code for code, name in all_links}
print(f"\n从 HTML 提取的代码可用于筛选 push2ex 数据")
print(f"行业代码数量: {len(industry_codes_from_html)}")

# 但全是"行业"吗? 验证: 看看是否有典型概念代码在里面
concept_indicators = ["概念", "AI", "5G", "锂电", "元宇宙", "东数西算", "CPO", "ChatGPT"]
concept_in_list = []
for code, name in all_links:
    if any(c in name for c in concept_indicators):
        concept_in_list.append(f"{code} {name}")
print(f"\n疑似概念板块: {len(concept_in_list)}")
for item in concept_in_list[:10]:
    print(f"  {item}")

# 6) 找一个真正只含申万二级行业的来源
#    尝试从 quote.eastmoney.com/center/gridlist.html#hs_a_board 提取
#    或者使用已知的行业分类数据
print("\n\n===== 总结 =====")
print("push2ex.getAllBKChanges 可获取当日997条板块变动（含涨跌幅、资金流）")
print("需通过 代码前缀过滤 或 HTML页面代码匹配 筛选申万行业")
print("建议: 维护一个申万二级行业代码列表 (约152个)，定期更新")

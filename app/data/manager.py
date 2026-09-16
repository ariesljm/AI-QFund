"""基金管理团队切片（票 12 切片三 / spec US17）：经理负荷与稳定性。

数据源：天天基金 F10 `fundf10.eastmoney.com/jjjl_{code}.html`（UTF-8，实测
200/50KB），表格含最近若干任期的基金经理记录：
    任职起始 | 任职结束（'至今' = 现任） | 经理姓名（空格分隔=共管） | 任职天数 | 任职回报

切片信号（从单页可得，无递归经理详情页）：
- 现任经理姓名与人数（共管 → 负荷分散信号）
- 现任本任期时长（<12 个月 = 刚上任，稳定性偏弱）
- 近 3 年经理更换次数与离职名单（快速轮换 → 卸任前兆）
管理规模/单经理在管基金数需经理详情页（递归抓取，成本高）——本轮不做，
审计仍可基于共管/时长/更换频率给出 manager_load 判断。

缺失显式可见（不静默填空）：页面获取失败/解析为空 → 切片文本注明缺失。
"""

import re
from datetime import date, datetime

import requests

_F10_URL = "http://fundf10.eastmoney.com/jjjl_{code}.html"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": "http://fund.eastmoney.com/"}
_FETCH_TIMEOUT = 15
# 任职时长观察阈值（月）：短于即"刚上任"（稳定性弱）
_MIN_TENURE_MONTHS = 12
# 近 N 年经理更换扫描窗口
_CHANGE_YEARS = 3


def parse_managers(html: str) -> dict:
    """解析 F10 基金经理表格 → {current: [names], tenure_months, change_count, changes}。

    current: 现任经理姓名列表（'至今' 行解析，空格分隔=共管）。
    tenure_months: 现任期间月数（最早现任记录起算；无现任 → None）。
    change_count: 近 _CHANGE_YEARS 年记录到的经理轮换次数（非现任行计数）。
    changes: 近 _CHANGE_YEARS 年离任经理姓名列表（新名字进入/旧名字消失）。
    表格缺失 → {"current": [], "tenure_months": None, "change_count": 0, "changes": []}。
    """
    out: dict = {"current": [], "tenure_months": None,
                 "change_count": 0, "changes": []}
    m = re.search(r"<th>基金经理</th>.*?<tbody>(.*?)</tbody>", html, re.S)
    if not m:
        return out
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(1), re.S)
    if not rows:
        return out
    current: list[str] = []
    cur_start: str | None = None
    for row in rows:
        cells = [re.sub(r"<[^>]+>", "", c).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(cells) < 3:
            continue
        start, end, names = cells[0], cells[1], cells[2]
        names = [n for n in re.split(r"\s+", names) if n]
        if not names:
            continue
        if end == "至今":
            current.extend(names)
            if cur_start is None:
                cur_start = start
        else:
            out["change_count"] += 1
            # 近 _CHANGE_YEARS 年内离任记录
            try:
                end_d = datetime.strptime(end, "%Y-%m-%d")
                if (date.today() - end_d.date()).days <= _CHANGE_YEARS * 365:
                    out["changes"].append(names[0])
            except ValueError:
                pass
    out["current"] = current
    out["changes"] = list(dict.fromkeys(out["changes"]))   # 去重（同一经理多段任期）
    if cur_start:
        try:
            s = datetime.strptime(cur_start, "%Y-%m-%d")
            out["tenure_months"] = max(0, (date.today() - s.date()).days // 30)
        except ValueError:
            out["tenure_months"] = None
    return out


def fetch_managers(code: str) -> dict | None:
    """抓取 + 解析；网络失败/解析异常 → None（调用方标注缺失）。"""
    try:
        r = requests.get(_F10_URL.format(code=code), headers=_HEADERS,
                         timeout=_FETCH_TIMEOUT)
        r.raise_for_status()
        html = r.content.decode("utf-8", errors="replace")
    except Exception:
        return None
    return parse_managers(html)


def manager_text(code: str) -> str:
    """管理团队切片文本（US17 素材装配）。缺失显式标注，不静默填空。"""
    info = fetch_managers(code)
    if info is None:
        return "管理团队：数据获取失败——无法评估该维度"
    if not info["current"]:
        return "管理团队：无现任经理数据（页面无'至今'记录）——无法评估"
    parts = [f"现任经理: {'、'.join(info['current'])}"]
    if len(info["current"]) > 1:
        parts.append(f"共管 {len(info['current'])} 人（精力分散信号）")
    tm = info["tenure_months"]
    if tm is not None:
        flag = "（刚上任，稳定性偏弱）" if tm < _MIN_TENURE_MONTHS else ""
        parts.append(f"本任期 {tm} 个月{flag}")
    if info["change_count"]:
        parts.append(f"近{_CHANGE_YEARS}年经理更换 {info['change_count']} 次")
        if info["changes"]:
            parts.append(f"离任: {'、'.join(info['changes'][:5])}")
    return "管理团队：" + "；".join(parts)

"""开发期接口验证脚本：确认 3 条核心数据链路可用。

运行方式：
    uv run python probe_apis.py

验证目标（见 DEVELOPMENT_PLAN Phase 0 验证项）：
    - 基金列表 ≥100 条
    - 历史净值 ≥500 天
    - 当日沪深300收盘价可获取
"""

import json
import re
import sys
import tomllib

import requests

SETTINGS_PATH = "config/settings.toml"


def load_settings():
    with open(SETTINGS_PATH, "rb") as f:
        return tomllib.load(f)


def fetch(url, params=None, timeout=15):
    """发起 GET 请求，绕过系统代理（解决 push2 封控问题）。"""
    s = requests.Session()
    s.trust_env = False  # 忽略 Windows 系统代理，直连目标服务器
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    resp = s.get(url, params=params, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp


def probe_fund_list(settings):
    """验证基金全量列表接口（天天基金 fundtradenew.aspx）。"""
    from ast import literal_eval

    api = settings["api"]
    url = api["fund_list_url"]
    params = {
        "ft": "gp",  # 股票型
        "pi": 1,
        "pn": 200,
        "sc": "1",
        "st": "desc",
    }
    resp = fetch(url, params)
    text = resp.text
    # 接口返回 JS 赋值：var rankData = {datas:["...","..."],...};
    m = re.search(r"var rankData\s*=\s*(\{.*\});", text, re.DOTALL)
    if not m:
        raise RuntimeError("基金列表响应无法解析为 rankData")
    obj_str = re.sub(r"([A-Za-z_]\w*)\s*:", r'"\1":', m.group(1))
    obj = literal_eval(obj_str)
    rows = obj.get("datas") or []
    n = len(rows)
    print(f"[基金列表] 拉取 {n} 条（目标 ≥100）")
    if n < 100:
        raise AssertionError(f"基金列表数量不足：{n} < 100")
    return n


def probe_history_nav(settings, code="000001"):
    """验证单基历史净值接口（pingzhongdata/{code}.js）。"""
    api = settings["api"]
    url = api["pingzhongdata_url"].format(code=code)
    resp = fetch(url)
    text = resp.text
    # 累计净值序列存放于 ACWorthTrend 变量，格式 [[时间戳, 累计净值], ...]
    # 文件为单行，取首个 '];' 结束的数组，避免贪婪匹配到文件末尾多余字符
    m = re.search(r"ACWorthTrend\s*=\s*(\[.*?\]);", text, re.DOTALL)
    if not m:
        raise RuntimeError("未在 pingzhongdata 中找到 ACWorthTrend（累计净值）")
    series = json.loads(m.group(1))
    days = len(series)
    print(f"[历史净值] 基金 {code} 累计净值 {days} 天（目标 ≥500）")
    if days < 500:
        raise AssertionError(f"历史净值天数不足：{days} < 500")
    return days


def probe_hs300(settings):
    """验证沪深300日线接口（新浪财经 K 线）。"""
    api = settings["api"]
    url = api["index_url"]
    params = {
        "symbol": api["hs300_symbol"],
        "scale": 240,  # 日线
        "ma": 60,
        "datalen": 5,
    }
    resp = fetch(url, params)
    klines = resp.json()
    if not klines:
        raise RuntimeError("沪深300 日线无返回")
    close = klines[-1]["close"]
    print(f"[沪深300] 最新收盘={close}，最近 {len(klines)} 条日线")
    return close


def main():
    settings = load_settings()
    results = {}
    for name, fn in [
        ("fund_list", probe_fund_list),
        ("history_nav", probe_history_nav),
        ("hs300", probe_hs300),
    ]:
        try:
            results[name] = fn(settings)
            print(f"  ✅ {name} 通过")
        except Exception as e:  # noqa: BLE001
            print(f"  ❌ {name} 失败：{e}")
            results[name] = None

    failed = [k for k, v in results.items() if v is None]
    if failed:
        print(f"\n验证未通过：{failed}")
        sys.exit(1)
    print("\n全部接口验证通过。")


if __name__ == "__main__":
    main()

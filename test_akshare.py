"""测试AKShare行业板块函数 — 带trust_env绕过"""
import requests

# Monkey-patch: 确保所有请求绕过系统代理
_original_init = requests.Session.__init__
def _patched_init(self, *args, **kwargs):
    _original_init(self, *args, **kwargs)
    self.trust_env = False
requests.Session.__init__ = _patched_init

import akshare as ak
import json

funcs = [
    ("stock_board_industry_name_em", lambda: ak.stock_board_industry_name_em()),
    ("stock_board_industry_spot_em", lambda: ak.stock_board_industry_spot_em()),
    ("stock_sector_fund_flow_rank", lambda: ak.stock_sector_fund_flow_rank(indicator="今日")),
    ("stock_board_change_em", lambda: ak.stock_board_change_em()),
    ("stock_hsgt_board_rank_em", lambda: ak.stock_hsgt_board_rank_em()),
]

for name, fn in funcs:
    try:
        print(f"\n=== {name} ===")
        df = fn()
        print(f"rows={len(df)} cols={list(df.columns)[:15]}")
        print(df.head(2).to_string())
    except Exception as e:
        print(f"FAIL: {type(e).__name__}: {str(e)[:150]}")

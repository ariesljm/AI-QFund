"""实时指数行情 module：15 秒实时缓存，抓取失败降级为数据库最近收盘（60 秒缓存）。"""

import asyncio
import logging
import re
import time
from datetime import datetime

from app.data.fetchers import fetch
from app.web.runner import is_trading_time
import app.repo as repo

logger = logging.getLogger("web.quotes")


class IndexQuoteCache:
    """实时指数行情缓存：交易时段 15 秒实时抓取，非交易时段/失败降级为最近收盘。"""

    def __init__(self):
        self.data: dict | None = None
        self.expires: float = 0.0
        self.ttl_live = 15.0
        self.ttl_fallback = 60.0

    async def get(self) -> dict:
        now = time.time()
        if self.data is not None and now < self.expires:
            return self.data
        # 非交易日/非交易时段（节假日、调休、周末均覆盖）不请求行情接口，直接返回收盘缓存
        if not is_trading_time():
            self.data = {"items": self._fallback_closed(), "updated_at": datetime.now().strftime("%H:%M:%S"), "source": "closed"}
            self.expires = now + self.ttl_fallback
            return self.data
        try:
            items = await asyncio.to_thread(self._fetch_live)
            self.data = {"items": items, "updated_at": datetime.now().strftime("%H:%M:%S"), "source": "live"}
            self.expires = now + self.ttl_live
        except Exception as e:
            logger.warning("实时行情抓取失败，降级为收盘价: %s", str(e)[:120])
            self.data = {"items": self._fallback_closed(), "updated_at": datetime.now().strftime("%H:%M:%S"), "source": "closed"}
            self.expires = now + self.ttl_fallback
        return self.data

    def _fetch_live(self) -> list[dict]:
        """腾讯 qt.gtimg.cn 简化接口：v_s_sh000001="1~上证指数~000001~价格~涨跌额~涨跌幅%..."。"""
        resp = fetch("https://qt.gtimg.cn/q=s_sh000001,s_sh000300", timeout=8)
        text = resp.content.decode("gbk", errors="replace")
        items = []
        for m in re.finditer(r'v_s_sh(\d+)="([^"]*)"', text):
            code, payload = m.group(1), m.group(2)
            fields = payload.split("~")
            if len(fields) < 6:
                continue
            try:
                price = float(fields[3])
                pct = float(fields[5])
            except ValueError:
                continue
            items.append({"code": f"sh{code}", "name": fields[1], "price": price,
                          "change_percent": pct, "source": "live"})
        if not items:
            raise RuntimeError("行情响应为空")
        return items

    def _fallback_closed(self) -> list[dict]:
        """降级：沪深300/上证指数取数据库最近收盘（含最近两日涨跌幅）；无历史数据标记不可用。"""
        items = []
        for code, name in (("sh000300", "沪深300"), ("sh000001", "上证指数")):
            rows = sorted(repo.get_index_series(code, ("date", "close")), key=lambda r: r[0])
            if rows:
                price = rows[-1][1]
                pct = None
                if len(rows) >= 2 and rows[-2][1]:
                    pct = round((rows[-1][1] / rows[-2][1] - 1) * 100, 2)
                items.append({"code": code, "name": name, "price": price,
                              "change_percent": pct, "date": rows[-1][0], "source": "closed"})
            else:
                items.append({"code": code, "name": name, "price": None,
                              "change_percent": None, "source": "unavailable"})
        return items


# 模块级单例：路由层只读消费，缓存状态封装在实例内
index_quote = IndexQuoteCache()

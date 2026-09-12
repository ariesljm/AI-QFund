"""板块历史日线回填：成分股市值加权合成（ticket 02）。

**背景**：东财官方板块历史接口（`push2his.eastmoney.com`）在本部署环境
**域名级不可达**——TLS 连接被服务端主动断开（RemoteProtocolError），
已验证 curl_cffi 指纹伪装、IPv4 强制、多编号子域、HTTP/HTTPS、长样本重试
（0/15 成功率）均无效；`push2delay` 仅返回最新 1 条；新浪/同花顺行业指数
与本项目东财板块体系名称匹配率仅 36%（粒度不一致）。

**方案**：改用「板块市值 Top-K 成分股日线 → 市值加权合成板块日涨跌幅」。
- 成分股列表：`push2.eastmoney.com/api/qt/clist/get`（本环境可达、稳定）
- 个股日线：搜狐 `q.stock.sohu.com/hisHq`（GBK、654 日、前复权）
  （原腾讯 `web.ifzq.gtimg.cn` 被 WAF 概率拦截——curl 与 httpx 均偶发 501，改用搜狐）
- 合成：以当前总市值（push2 clist 的 f20）加权各成分股日收益

**局限**：主力净流入（`net_flow`）无历史源，历史行留空；该维度仅由
每日实时快照（`macro.fetch_flow` → `save_sector_snapshot`）逐日累积。

**口径**：Top-K=15 的成分股市值加权通常覆盖板块 50~70% 市值，与官方板块
指数存在跟踪误差；用于 RBSA 风格回归（找共变关系）足够，但不宣称等同于
官方指数点位。
"""

import app.repo as repo
from app.data.fetchers import fetch
from app.utils.log import get_logger

logger = get_logger("data.sector_history")

_TOP_K = 15  # 每板块取市值 Top-K 成分股
_SOHU_KLINE = "https://q.stock.sohu.com/hisHq"
_EM_CLIST = "https://push2.eastmoney.com/api/qt/clist/get"
_BACKFILL_DONE = "sector_history_backfilled"  # meta 标记（避免重复全量回填）


def tx_symbol(code: str) -> str:
    """6 位 A 股代码 → 搜狐符号（cn_ 前缀，沪/深/创业/科创通用）。"""
    return f"cn_{code}"


def _sohu_start_days(days: int) -> int:
    """600 个交易日约需 900 自然日（搜狐按自然日区间拉取）。"""
    return int(days * 1.5)


def board_top_members(board_code: str, k: int = _TOP_K) -> list[tuple[str, float]]:
    """板块市值 Top-K 成分股，返回 [(代码, 总市值)]（按市值降序）。"""
    r = fetch(_EM_CLIST, params={
        "pn": "1", "pz": str(k), "po": "1", "fid": "f20",
        "fs": f"b:{board_code}", "fields": "f12,f20",
    })
    diff = (r.json().get("data") or {}).get("diff") or {}
    vals = diff.values() if isinstance(diff, dict) else diff
    out: list[tuple[str, float]] = []
    for v in vals:
        code = str(v.get("f12") or "")
        mv = v.get("f20")
        if code and isinstance(mv, (int, float)) and mv > 0:
            out.append((code, float(mv)))
    return out


def stock_daily(code: str, days: int = 600) -> dict[str, float]:
    """搜狐历史日线，返回 {date: close}；无数据返回空 dict。

    搜狐行格式：[日期, 开盘, 收盘, 涨跌额, 涨跌幅, 最低, 最高, 量, 额, 换手, 量比]，
    收盘取 **index 2**（index 1 是开盘价，若误取会含隔夜跳空噪声、与基金净值收盘价
    口径不匹配）；响应为 GBK 编码，需手动 decode。
    """
    from datetime import datetime, timedelta
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=_sohu_start_days(days))).strftime("%Y%m%d")
    r = fetch(_SOHU_KLINE, params={
        "code": tx_symbol(code), "start": start, "end": end,
        "stat": "1", "order": "D", "period": "d",
    })
    try:
        import json
        data = json.loads(r.content.decode("gbk", errors="replace"))
    except Exception:
        return {}
    hq = ((data or [{}])[0] or {}).get("hq") or []
    out: dict[str, float] = {}
    for row in hq:
        if len(row) >= 3 and row[0] and row[2] not in (None, ""):
            try:
                out[str(row[0])] = float(row[2])
            except (TypeError, ValueError):
                continue
    return out


def synthesize_board(board_code: str, k: int = _TOP_K,
                     cache: dict[str, dict[str, float]] | None = None) -> dict[str, float]:
    """市值加权合成板块日涨跌幅，返回 {date: pct_chg%}。

    cache：跨板块复用个股日线（同一只股票出现在多个板块时避免重复请求）。
    """
    members = board_top_members(board_code, k)
    if not members:
        return {}
    total_mv = sum(mv for _, mv in members)
    if total_mv <= 0:
        return {}
    if cache is None:
        cache = {}
    agg: dict[str, float] = {}
    for code, mv in members:
        daily = cache.get(code)
        if daily is None:
            try:
                daily = stock_daily(code)
            except Exception as e:
                logger.debug("个股 %s 日线获取失败: %s", code, str(e)[:60])
                daily = {}
            cache[code] = daily
        if not daily:
            continue
        w = mv / total_mv
        dates = sorted(daily)
        for j in range(1, len(dates)):
            prev, cur = daily[dates[j - 1]], daily[dates[j]]
            if prev > 0:
                agg[dates[j]] = agg.get(dates[j], 0.0) + w * (cur / prev - 1.0) * 100.0
    return agg


def backfill_sector_history(days: int = 600, top_k: int = _TOP_K,
                            force: bool = False) -> int:
    """回填全部行业板块历史日线（成分股合成），返回写入行数。

    meta 标记避免重复全量回填（`force=True` 强制重跑）。
    """
    if not force and repo.get_meta(_BACKFILL_DONE):
        logger.info("板块历史已回填（%s），跳过", repo.get_meta(_BACKFILL_DONE))
        return 0

    # 本环境 curl_cffi/tls-client 对 push2 稳定失败（连接被断），curl.exe -4 才是
    # 生效策略；把降级链起点直接指向它，避免每板块白等两次失败（约 8s → 1s）。
    # `_PUSH2_STRATEGIES` 即为此设计的可注入策略序列（见 fetchers 注释）。
    from app.data import fetchers as _f
    _f._PUSH2_STRATEGIES = (_f._fetch_push2_curl_exe,)

    from app.data.macro import load_board_sectors
    boards = load_board_sectors()
    if not boards:
        logger.warning("板块列表为空，无法回填")
        return 0

    cache: dict[str, dict[str, float]] = {}
    total = 0
    for i, b in enumerate(boards, 1):
        code = str(b.get("c") or "")
        name = str(b.get("n") or "").strip()
        if not code or not name:
            continue
        try:
            series = synthesize_board(code, top_k, cache)
        except Exception as e:
            logger.warning("板块 %s(%s) 合成失败: %s", name, code, str(e)[:80])
            continue
        if not series:
            continue
        rows = [(d, code, name, round(v, 4)) for d, v in series.items()]
        total += repo.save_sector_history_batch(rows)
        if i % 20 == 0:
            logger.info("回填进度 %d/%d 板块，累计 %d 行", i, len(boards), total)

    from datetime import datetime
    repo.save_meta(_BACKFILL_DONE, datetime.now().strftime("%Y-%m-%d"))
    logger.info("板块历史回填完成: %d 板块, %d 行", len(boards), total)
    return total


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    force = "--force" in sys.argv
    n = backfill_sector_history(force=force)
    print(f"回填完成: {n} 行")

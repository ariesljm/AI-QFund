"""净值数据抓取：全量下载 & 增量更新。"""

import asyncio
import json
import re
import time
from datetime import datetime, timedelta, timezone

import httpx

from app.data.fetchers import fetch, fetch_async
from app.data.ingest import (
    FetchOutcome,
    filter_cooldown_targets,
    is_systematic_no_update,
    run_batched_fetch,
)
from app.data.store import (
    NAV_RETENTION_DAYS,
    STAGE_NO_UPDATE,
    mark_recovered_batch,
    record_failure,
    save_nav_batch,
)
from app.database import db_conn
from app.utils.log import get_logger
from app.utils.trading_calendar import trading_day_lag

logger = get_logger("nav")

_CHINA_TZ = timezone(timedelta(hours=8))
"""东八区时区：pingzhongdata 时间戳为中国时间 0 点，避免容器 UTC 下日期偏移一天。"""


_LSJZ_PAGE_SIZE = 100
"""lsjz 接口单页最大返回条数（实测固定 20 条，pageSize>=500 会直接返回空）。"""
_LSJZ_MAX_PAGES = 400


def _parse_lsjz_page(text: str) -> tuple[list[dict], int]:
    """解析单页 lsjz 响应 → (navs, total_count)，兼容 jQuery 包裹与裸 JSON。

    响应不含合法 JSON（空 body/反爬拦截页）时抛 ValueError——这类"假空"
    必须按拉取失败处理（短冷却+补查），不能与"接口确认无数据"混为一谈，
    否则健康基金会被误判停更、误入 7 天长冷却造成静默断档。
    """
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"lsjz 响应非 JSON（疑似反爬/限流）: {text[:80]!r}")
    try:
        data = json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError) as e:
        raise ValueError(f"lsjz 响应 JSON 解析失败（疑似反爬/限流）: {e}") from e
    records = (data.get("Data") or {}).get("LSJZList") or []
    total = int(data.get("TotalCount") or 0)
    navs = []
    for r2 in records:
        cum_nav_str = r2.get("LJJZ", "")
        if not cum_nav_str:
            continue
        navs.append({
            "date": r2["FSRQ"],
            "cum_nav": float(cum_nav_str),
        })
    return navs, total


async def _lsjz_fetch_all(session, code: str, start_date: str,
                          headers: dict, timeout: float = 15) -> list[dict]:
    """分页拉取 lsjz 历史净值（pageSize 太大接口会返回空，必须逐页翻取）。

    全量时返回完整历史序列；带 start_date 时仅返回其后的净值。
    垃圾响应（接口抖动/反爬返回空 body 但 HTTP 200）重试一次后仍失败则向上抛，
    由调用方按拉取失败记录（primary），不误判为"确认无新数据"。
    """
    all_navs = []
    page_index = 1

    def _page_url() -> str:
        url = (
            "https://api.fund.eastmoney.com/f10/lsjz?"
            f"callback=jQuery&fundCode={code}&pageIndex={page_index}"
            f"&pageSize={_LSJZ_PAGE_SIZE}"
        )
        if start_date:
            url += f"&startDate={start_date}"
        return url

    async def _fetch_page() -> tuple[list[dict], int]:
        resp = await fetch_async(session, _page_url(), timeout=timeout, headers=headers)
        text = resp.text
        return _parse_lsjz_page(text)

    while page_index <= _LSJZ_MAX_PAGES:
        try:
            navs, total = await _fetch_page()
        except ValueError:
            # 垃圾响应重试一次；仍失败则让异常传播（按拉取失败处理）
            navs, total = await _fetch_page()
        all_navs.extend(navs)
        if not navs or len(all_navs) >= total:
            break
        page_index += 1
    return all_navs


def _parse_pingzhong_acworth(text: str) -> list[dict]:
    """解析 pingzhongdata 的 ACWorthTrend（累计净值序列）→ [{"date", "cum_nav"}, ...]。"""
    m = re.search(r"ACWorthTrend\s*=\s*(\[.*?\]);", text, re.DOTALL)
    if not m:
        return []
    try:
        series = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return []
    navs = []
    for item in series:
        if len(item) < 2:
            continue
        ts_ms, cum_nav = item[0], item[1]
        if cum_nav is None:
            continue
        date_str = datetime.fromtimestamp(ts_ms / 1000, tz=_CHINA_TZ).strftime("%Y-%m-%d")
        navs.append({"date": date_str, "cum_nav": float(cum_nav)})
    return navs


async def _pingzhong_fetch_all_async(session, code: str,
                                     headers: dict, timeout: float = 20) -> list[dict]:
    """异步抓取 pingzhongdata 完整历史净值（累计净值），单请求返回全量序列。

    用于全量首次下载，以及增量更新中无本地数据（全量兜底）的基金——
    lsjz 单页最多 20 条，全量逐页翻取需要数百次请求，pingzhongdata 一次即可。
    """
    url = f"https://fund.eastmoney.com/pingzhongdata/{code}.js"
    resp = await fetch_async(session, url, timeout=timeout, headers=headers)
    text = resp.text
    return _parse_pingzhong_acworth(text)


_PROBE_FUND_CODES = ["000001", "110011", "161725", "005827", "519674", "163406"]
"""活跃基金代理：探测接口最新净值日期时逐只取首页，避免单只基金停更/失败导致误判。"""


async def _probe_lsjz_latest(session, headers: dict) -> str | None:
    """探测 lsjz 接口当前可返回的最新净值日期。

    用多只活跃基金取最大值，降低单只基金停更/请求失败带来的偏差；
    全部失败返回 None（上层降级为本地全局最新日期）。
    """
    latest: str | None = None
    for code in _PROBE_FUND_CODES:
        try:
            url = (
                "https://api.fund.eastmoney.com/f10/lsjz?"
                f"callback=jQuery&fundCode={code}&pageIndex=1&pageSize={_LSJZ_PAGE_SIZE}"
            )
            resp = await fetch_async(session, url, timeout=15, headers=headers)
            text = resp.text
            navs, _ = _parse_lsjz_page(text)
            if navs and (latest is None or navs[0]["date"] > latest):
                latest = navs[0]["date"]
        except Exception:
            logger.debug("探测基金 %s 最新净值失败", code, exc_info=True)
    return latest


def _plan_nav_tasks(
    all_codes: list[str],
    local_max: dict[str, str],
    global_latest: str | None,
    api_latest: str | None,
) -> tuple[list[tuple[str, str]], int, int]:
    """规划净值增量/全量任务，对齐到接口最新日期（纯函数，便于测试）。

    对齐基准取接口最新日期（api_latest），探测失败时降级为本地全局最新日期：
    - api_latest > global_latest（接口有新交易日数据）→ 全库对齐：所有
      lm < api_latest 的基金都规划增量，从本地最后日期补到接口最新；
    - api_latest == global_latest（无新数据）→ 已对齐基金跳过，仅滞后基金增量；
    - api_latest 为 None（探测失败）→ 降级：跳过本地==global_latest 的基金。

    返回 (tasks_meta, incr_cnt, full_cnt)。
    """
    target = api_latest or global_latest
    tasks_meta: list[tuple[str, str]] = []
    incr_cnt = 0
    full_cnt = 0
    for code in all_codes:
        lm = local_max.get(code)
        if target is not None and lm == target:
            continue
        if lm:
            tasks_meta.append((code, lm))
            incr_cnt += 1
        else:
            tasks_meta.append((code, ""))
            full_cnt += 1
    return tasks_meta, incr_cnt, full_cnt


def _split_tasks(tasks_meta: list[tuple[str, str]], global_latest: str | None
                 ) -> tuple[list[str], list[tuple[str, str]], list[tuple[str, str]]]:
    """三路拆分增量任务（纯函数便于测试）：

    - batch：本地最新 == 全局最新（只差最新 1 天）→ rankhandler 榜单快照（200 只/请求）
    - lag：本地最新 < 全局最新（差 2+ 天，QDII/停更）→ lsjz 逐只补全
    - full：本地无数据 → pingzhongdata 全量兜底
    """
    batch_codes = [code for code, lm in tasks_meta if lm == global_latest]
    lag_tasks = [(code, lm) for code, lm in tasks_meta if lm and lm < global_latest]
    full_tasks = [(code, lm) for code, lm in tasks_meta if not lm]
    return batch_codes, lag_tasks, full_tasks


def _count_stale_lagging(tasks_meta: list[tuple[str, str]], target: str | None) -> int:
    """统计增量任务中长期停更的基金数（滞后 >= 2 天，纯函数便于测试）。

    滞后 1 个交易日（如 QDII 净值晚一天发布）属正常发布节奏，不算停更；
    仅用于日志措辞（"规划增量 X 只（含长期停更 Y 只）"），避免误读为拉到了数据。
    """
    if not target:
        return 0
    t = datetime.strptime(target, "%Y-%m-%d").date()
    n = 0
    for _code, lm in tasks_meta:
        if not lm:
            continue
        try:
            if (t - datetime.strptime(lm, "%Y-%m-%d").date()).days >= 2:
                n += 1
        except ValueError:
            continue
    return n


# ── 覆盖度断言（ticket 25）─────────────────────────────
# 容忍 5 个交易日：实测正常发布延迟最多到 4~5 天（QDII/慢披露基金），
# 容忍 1~3 天时健康态基线高达 2.05%（260/12,675，全是正常换手）。
#
# **为何是 5 而不是 10**：真实故障恰好是 **6 个交易日**（2026-09-03 → 09-11），
# 而 `mark_stale_funds` 的逐只打标阀值是 **>10 交易日** —— 6 < 10，所以那些基金
# 根本没被打标，这才是故障能藏住 6 天的直接原因。本门必须卡在 5~10 之间：
# 上界看住“不是正常延迟”，下界看住“比逐只打标更早发现”。
NAV_COVERAGE_TOLERANCE_DAYS = 5
# 超过该比例的基金滞后才算数据基座故障。阈值由**实测**校准，不是拍脑袋：
#   - 健康态（2026-09-03 回放重建）：71/12,675 = **0.56%**
#   - 故障态（2026-09-11，真实发生）：6,232/12,675 = **49.17%**
# 取 5%：对健康基线留 9 倍余量，距故障态还差 10 倍——两者差了 88 倍，这个
# 阈值不需要更精细。
# 注意：**单基金停更不由本门拦**（那由 mark_stale_funds 逐只打标）；本门只负责
# “数据基座部分失效”这种静默灾难。
MAX_NAV_STALE_RATIO = 0.05


class NavCoverageError(RuntimeError):
    """净值覆盖度缺口超阀——数据基座部分失效，不可继续算特征与推荐。"""


def nav_coverage_gap(ranges: dict[str, tuple[str, str]], dates: list[str],
                     target: str | None = None,
                     tolerance: int = NAV_COVERAGE_TOLERANCE_DAYS) -> dict:
    """净值覆盖度缺口（纯函数，便于测试）。

    `ranges` 来自 `repo.get_nav_time_state()`：{code: (首日, 末日)}；`dates` 为
    全部净值日期。滞后计数用 `trading_day_lag`（与停更打标/特征新鲜度同一来源）。

    **为什么需要这个函数**：既有护栏是 `total_new == 0`——只要还有一只基金在新，
    它就永不触发。那护栏拦不住真实发生过的故障（2026-09-04 起一半市场停更、每日
    仍写入约 6,400 行、日志无异常），因为“写入了若干条”与“全市场都更新了”是两件事。

    返回 dict：`target` / `total` / `stale` / `ratio` / `worst_lag` / `samples`。
    `stale` 含**从无净值**的基金（拉取从未成功也是缺口），而 `worst_lag` 只统计
    有净值基金中的最深滞后。空库/无目标日一律返回零缺口（空库自举不得自己拦住自己）。
    """
    latest = {code: end for code, (_start, end) in ranges.items()}
    total = len(latest)
    if target is None:
        target = max((v for v in latest.values() if v), default=None)
    if not target or not dates or not total:
        return {"target": target, "total": total, "stale": 0, "ratio": 0.0,
                "worst_lag": 0, "samples": []}

    days = set(dates)
    stale: list[tuple[str, int]] = []
    worst_lag = 0
    for code, end in latest.items():
        if not end:
            stale.append((code, 0))  # 从无净值
            continue
        lag = trading_day_lag(end, target, days=days)
        if lag > tolerance:
            stale.append((code, lag))
            worst_lag = max(worst_lag, lag)
    stale.sort(key=lambda x: -x[1])
    return {
        "target": target,
        "total": total,
        "stale": len(stale),
        "ratio": len(stale) / total,
        "worst_lag": worst_lag,
        "samples": [c for c, _ in stale[:10]],
    }


def assert_nav_coverage(ranges: dict[str, tuple[str, str]], dates: list[str],
                        target: str | None = None,
                        tolerance: int = NAV_COVERAGE_TOLERANCE_DAYS,
                        threshold: float = MAX_NAV_STALE_RATIO) -> dict:
    """缺口超阀 → 抛 `NavCoverageError`（让整个数据基座步骤失败），否则返回缺口。

    **为何是抛错而不是告警**：`run_pipeline` 不捕获步骤异常，所以抛错会中止后续
    步骤（特征、推荐），下游就拿不到陈旧特征；而告警只是一种建议——1.x 在
    `recommend` 里已经有“特征滞后 ≥ 2 天”的 ERROR 日志，它是**故意只记不拦**的
    （按早年决策“失败后用旧特征”，注释原文：“强告警但仍放行”），结果就是 6 天没
    出特征也无人知晓。静默失效只能靠结构性阻断治疗，不能靠“多看一眼日志”。
    """
    gap = nav_coverage_gap(ranges, dates, target, tolerance)
    if gap["ratio"] > threshold:
        raise NavCoverageError(
            f"净值覆盖度缺口过大：{gap['stale']}/{gap['total']} 只基金的净值日落后于 "
            f"{gap['target']} 超过 {tolerance} 个交易日（{gap['ratio']:.1%} > 阈值 "
            f"{threshold:.1%}），最深滞后 {gap['worst_lag']} 个交易日，"
            f"样例 {gap['samples']}。这不是「少拉了几只」，而是数据基座部分失效："
            f"流程已中止，避免用陈旧净值算特征与推荐。"
        )
    return gap


def _summarize_nav_results(conn_, results) -> FetchOutcome:
    """批处理净值结果汇总为 FetchOutcome 契约（增量与全量共用 handle_batch adapter）。

    原名 _save_nav_batch 与 store.save_nav_batch（写入器）同名异义，改名消歧义：
    本函数不写库语义主导，而是"逐只调写入器 + 汇总为结果契约"。
    """
    success: set[str] = set()
    no_update: list[str] = []
    failed_codes: list[str] = []
    new_count = 0
    for code, navs, failed in results:
        if failed:
            failed_codes.append(code)
            continue
        n = save_nav_batch(conn_, code, navs)
        if n == 0:
            # 接口确认无新数据（停更/滞后发布/无净值页）：计入失败累计，
            # 连续 3 个周期后进入冷却，避免每次运行对注定拉不到的基金反复重试
            no_update.append(code)
        else:
            success.add(code)
            new_count += n
    return {"new_count": new_count, "success": success,
            "no_update": no_update, "failed": failed_codes}


def _backfill_one(code: str) -> None:
    """单只基金全量回填（走 pingzhongdata 一次拉全历史）。"""
    navs = fetch_fund_nav(code)
    if navs:
        with db_conn() as conn_:
            save_nav_batch(conn_, code, navs)


# ── 批量净值快照（rankhandler 榜单，200 只/请求；2026-09-15 替换死掉的 fundmobapi） ──
# 为什么换：fundmobapi 移动端接口已**确定性失效**——`ErrCode=61136403`，换
# deviceid / 单只 / 多只全都同一错误码，生产日志连续多日"净值批量增量完成: 成功 0 只"。
# 于是每天上万只整体退入逐只 lsjz，把 3 分钟的快相变成 69 分钟的慢相（ticket 26）。
#
# 为什么不是 pingzhongdata：实测两者都被限到同一个速率——
#   lsjz           3.0 req/s（每请求 1 只）
#   pingzhongdata  3.2 req/s（每请求 1 只，且要传/解析 2,371 行全量历史）
# 瓶颈是"每请求只取一只"，换同维度的端点没有收益，pingzhongdata 反而多传约
# 400 倍数据。要提速只能提高"每次请求覆盖的基金数"。
#
# rankhandler 榜单一次返回 200 只的代码/净值日期/累计净值：实测 102 页 36.8 秒
# 覆盖 20,356 只（本股票池 12,900 只覆盖 91.3%），比逐只的 69 分钟快约 130 倍。
# 榜单按区间收益排序、且要求有 1 年以上历史，故新基金/部分份额不在榜上——它们
# 仍走原逐只路径（约 1,100 只 ≈ 6 分钟）。这是刻意的回退，不是遗漏。
_RANKHANDLER_PAGE = 200
_RANKHANDLER_CONCURRENCY = 5
# 必须用 https：用 http 会得到 301（重定向到 https）。而 `fetch_async` 不跟随重定向，
# 且 301 不会触发 `raise_for_status` —— 结果是拿到空 body、快照为空、**静默退入
# 慢路径 69 分钟**。这正是本模块要消灭的那类隐蔽失效，所以在这里把它钉住。
_RANKHANDLER_URL = (
    "https://fund.eastmoney.com/data/rankhandler.aspx"
    "?op=ph&dt=kf&ft=all&rs=&gs=0&sc=zzf&st=desc&sd=&ed=&qdii=&tabSubtype=,,,,,"
)


def parse_rankhandler_page(text: str) -> list[tuple[str, str, float]]:
    """解析一页榜单 → [(code, date, cum_nav)]（纯函数，便于测试）。

    响应是 JS 字面量 `datas:['code,name,...,date,unit,cum,...', ...]`，每行是
    逗号分隔字符串。实测字段位：0=代码 3=净值日期 5=累计净值（与 lsjz 的 LJJZ 同口径）。
    """
    m = re.search(r"datas:(\[.*?\])", text, re.S)
    if not m:
        return []
    try:
        rows = json.loads(m.group(1).replace("'", '"'))
    except ValueError:
        return []
    out: list[tuple[str, str, float]] = []
    for row in rows:
        f = str(row).split(",")
        if len(f) <= 5 or not f[0] or not f[3] or not f[5]:
            continue
        try:
            out.append((f[0], f[3], float(f[5])))
        except ValueError:
            continue
    return out


async def _rankhandler_snapshot(session, headers: dict) -> dict[str, tuple[str, float]]:
    """全市场最新净值快照 {code: (date, cum_nav)}（一页 200 只，约 102 页）。

    先取首页读 `allRecords` 得总页数，再并发取余页（限流 `_RANKHANDLER_CONCURRENCY`）。
    """
    sem = asyncio.Semaphore(_RANKHANDLER_CONCURRENCY)

    async def _page_text(pi: int) -> str:
        async with sem:
            url = f"{_RANKHANDLER_URL}&pi={pi}&pn={_RANKHANDLER_PAGE}&dx=1&v=0.1"
            resp = await fetch_async(session, url, timeout=30, headers=headers)
            return resp.text

    first = await _page_text(1)
    m = re.search(r"allRecords:(\d+)", first)
    pages = 1 if not m else max(1, -(-int(m.group(1)) // _RANKHANDLER_PAGE))
    texts = [first]
    if pages > 1:
        texts += list(await asyncio.gather(*(_page_text(p) for p in range(2, pages + 1))))
    snap: dict[str, tuple[str, float]] = {}
    for t in texts:
        for code, date, cum in parse_rankhandler_page(t):
            snap[code] = (date, cum)
    return snap


async def _rankhandler_incremental(session, headers: dict, codes: list[str],
                                   local_max: dict[str, str]) -> tuple[list, list[str], bool]:
    """用一份全市场快照覆盖批量相，返回 (results, missing, snapshot_failed)。

    - results: [(code, [{"date", "cum_nav"}], False)]——**仅当快照日期严格晚于本地
      最新日**才取用。否则会把同一行旧值当新值反复写回（快照接口对停更基金长期
      返回同一个日期，这是它最常见的形态）。
    - missing: 榜上无此基金，或快照日期不比本地新（回退 lsjz 逐只）
    - snapshot_failed: 快照一页都没拉到（调用方应大声告知，不要静静掉进慢路径）
    """
    t0 = time.monotonic()
    try:
        snap = await _rankhandler_snapshot(session, headers)
    except Exception as e:
        logger.error("净值批量快照（rankhandler 榜单）拉取失败: %s——本次整体退入逐只回退",
                     str(e)[:120])
        return [], list(codes), True
    if not snap:
        logger.error("净值批量快照（rankhandler 榜单）返回空——本次整体退入逐只回退")
        return [], list(codes), True

    results: list[tuple[str, list[dict], bool]] = []
    missing: list[str] = []
    for code in codes:
        got = snap.get(code)
        if not got or got[0] <= (local_max.get(code) or ""):
            missing.append(code)
            continue
        results.append((code, [{"date": got[0], "cum_nav": got[1]}], False))
    logger.info("净值批量快照: 榜单 %d 只, 命中并更新 %d/%d 只, 耗时 %.1f 秒",
                len(snap), len(results), len(codes), time.monotonic() - t0)
    return results, missing, False


async def async_update_nav_incremental(concurrency: int = 5) -> int:
    import app.repo as repo
    # 净值时间状态单一归属（架构审查候选 4）：消费 repo 语义服务，
    # 不再内联 GROUP BY（与打标/陈旧侧同口径，改一处即可）。
    all_codes = repo.get_buyable_codes()
    ranges, _ = repo.get_nav_time_state()
    local_max = {code: end for code, (_start, end) in ranges.items()}
    global_latest = max((end for _, end in ranges.values()), default=None)

    all_codes = filter_cooldown_targets(
        "nav_incr", all_codes, "净值增量",
        # 确认无新数据（停更/无净值页）的基金接口端就是没有数据，用 7 天长冷却
        # 减少反复请求；临时拉取失败（primary）仍走默认 1 天冷却，尽快重试恢复
        stage_cooldown_days={STAGE_NO_UPDATE: 7},
    )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://fund.eastmoney.com/",
    }

    async def _fetch_lsjz(session_, item) -> tuple[str, list[dict], bool]:
        code, start_date = item
        try:
            if start_date:
                navs = await _lsjz_fetch_all(session_, code, start_date, headers)
            else:
                # 无本地数据（新基金/H类份额等）：lsjz 全量需逐页数百次请求，改用 pingzhongdata 一次拿全
                navs = (await _pingzhong_fetch_all_async(session_, code, headers))[-NAV_RETENTION_DAYS:]
            logger.debug("增量 %s: %d 条净值", code, len(navs))
            return code, navs, False
        except Exception as e:
            logger.debug("基金 %s 增量净值拉取失败: %s", code, str(e)[:120], exc_info=True)
            return code, [], True

    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)

    async with httpx.AsyncClient(limits=limits) as session:
        api_latest = await _probe_lsjz_latest(session, headers)
        tasks_meta, incr_cnt, full_cnt = _plan_nav_tasks(
            all_codes, local_max, global_latest, api_latest,
        )
        logger.info(
            "净值增量：接口最新 %s, 本地全局最新 %s, 跳过已对齐 %d 只，"
            "规划增量 %d 只（本地滞后，含长期停更 %d 只），全量兜底 %d 只（本地无数据）",
            api_latest, global_latest,
            len(all_codes) - len(tasks_meta), incr_cnt,
            _count_stale_lagging(tasks_meta, api_latest or global_latest), full_cnt,
        )
        if not tasks_meta:
            return 0

        # ── 三路拆分：差 1 天走榜单快照 / 差多天走 lsjz / 无本地走 pingzhongdata ──
        # 占绝大多数的“差 1 天”基金由 rankhandler 榜单快照一次覆盖（200 只/请求，
        # 实测全市场 102 页 36.8 秒），把 12,690 次 lsjz 请求（~69 分钟）降到
        # ~1,100 次（约 6 分钟）。榜单未覆盖的（新基金/部分份额）与滞后基金
        # （QDII/停更，差 2+ 天）、无本地基金仍走原逐只路径。
        batch_codes, lag_tasks, full_tasks = _split_tasks(tasks_meta, global_latest)

        total_new = 0
        success: set[str] = set()
        no_update: list[str] = []
        failed: list[str] = []

        if batch_codes:
            t0 = time.monotonic()
            logger.info("净值批量快照（rankhandler 榜单 200只/请求）: %d 只（差 1 天）", len(batch_codes))
            batch_results, batch_missing, batch_api_down = await _rankhandler_incremental(
                session, headers, batch_codes, local_max)
            with db_conn() as conn_:
                for code, navs in batch_results:
                    n = save_nav_batch(conn_, code, navs)
                    if n:
                        total_new += n
                        success.add(code)
                    else:
                        no_update.append(code)
            if success:
                mark_recovered_batch("nav_incr", sorted(success))
            # no_update 记账与 lsjz 路径同语义（接口确认无新数据 → 累计进冷却，
            # 防每日反复重试）；系统性假空（占比超熔断阈值，如探测与批量接口
            # 日期竞态、PDATE 集体滞后）不记账，并入 missing 回退 lsjz 逐只
            # 确认真伪——与 run_batched_fetch 的 NO_UPDATE_GUARD 护栏同构。
            if no_update:
                if is_systematic_no_update(len(no_update), len(batch_codes)):
                    logger.error(
                        "净值批量增量：%d/%d 只确认无新数据，占比异常——疑似批量接口返回滞后，"
                        "并入 lsjz 回退逐只确认",
                        len(no_update), len(batch_codes))
                    batch_missing.extend(no_update)
                    no_update = []
                else:
                    for code in no_update:
                        record_failure("nav_incr", code, "接口确认无新数据", stage=STAGE_NO_UPDATE)
                    logger.info("净值批量增量：%d 只确认无新数据，已累计失败次数（满 3 次进入冷却）",
                                len(no_update))
            # 批量未返回/失败的基金回退 lsjz 逐只补全（不在此处判失败/冷却）
            if batch_missing:
                lag_tasks.extend((c, local_max.get(c, "")) for c in batch_missing)
                logger.info("净值批量缺失 %d 只，回退 lsjz 逐只补全", len(batch_missing))
            logger.info("净值批量增量完成: 成功 %d 只, 无新数据 %d 只, 缺失 %d 只, 耗时 %.1f 秒%s",
                        len(success), len(no_update), len(batch_missing), time.monotonic() - t0,
                        "（批量接口不可用，已整体退入慢路径）" if batch_api_down else "")

        if lag_tasks or full_tasks:
            outcome = await run_batched_fetch(
                session, fetch_type="nav_incr", label="增量净值",
                targets=lag_tasks + full_tasks, batch_size=100,
                fetch_one=_fetch_lsjz, handle_batch=_summarize_nav_results, backfill_one=_backfill_one,
                no_update_note="接口确认无新数据", primary_note="增量净值拉取失败",
            )
            total_new += outcome["new_count"]
            success |= outcome["success"]
            no_update.extend(outcome["no_update"])
            failed.extend(outcome["failed"])

        ok_cnt = len(tasks_meta) - len(failed)
        logger.info("净值增量更新完成: 新增 %d 条, 成功 %d 只, 无新数据 %d 只, 失败 %d 只",
                    total_new, len(success), len(no_update), len(failed))
        if total_new == 0 and ok_cnt > 100:
            # 探测接口最新净值日期：若比本地最新还新却没写入，才是真异常；
            # 周末/停更基金导致的 0 条属正常，不应告警。
            if api_latest and global_latest and api_latest > global_latest:
                logger.error(
                    "净值增量更新写入 0 条但接口已可返回 %s（本地最新 %s）——"
                    "疑似净值接口失效或请求被拒，请检查后重跑，否则特征/推荐将基于陈旧净值",
                    api_latest, global_latest,
                )
            else:
                logger.info("净值增量更新 0 条: 接口最新 %s, 本地最新 %s，无新增数据（正常）",
                            api_latest, global_latest)

        # 覆盖度断言（ticket 25）：写入若干条 ≠ 全市场都更新了。放在阶段末尾，
        # 因为要判定的是“跑完之后还剩多少基金没跟上”，而不是“这次请求成不成”——
        # 2026-09-04 那次故障每日都成功写入约 6,400 条，日志无异常。
        gap = assert_nav_coverage(*repo.get_nav_time_state())
        logger.info("净值覆盖度：%d/%d 只已对齐 %s，滞后超 %d 交易日 %d 只（最深 %d）",
                    gap["total"] - gap["stale"], gap["total"], gap["target"],
                    NAV_COVERAGE_TOLERANCE_DAYS, gap["stale"], gap["worst_lag"])

    return total_new


async def async_download_all_nav(concurrency: int = 15) -> int:
    import app.repo as repo
    all_codes = repo.get_buyable_codes()
    all_codes = filter_cooldown_targets(
        "nav_full", all_codes, "全量净值",
        stage_cooldown_days={STAGE_NO_UPDATE: 7},
    )
    if not all_codes:
        return 0

    logger.info("全量净值下载: %d 只基金, 并发 %d", len(all_codes), concurrency)

    # 注意：不能用 fundgz.1234567.com.cn/js/{code}.js（实时估值接口，无历史序列）。
    # lsjz 历史净值接口单页硬上限 20 条且大 pageSize 返回空，全量逐页翻取太慢，
    # 全量首次下载改用 pingzhongdata（单请求返回完整 ACWorthTrend 历史序列）；
    # 增量日常更新仍走 lsjz（startDate 后通常只有 1-2 页，见 _lsjz_fetch_all）。
    batch_size = 200
    total_new = 0
    start_time = time.monotonic()

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://fund.eastmoney.com/",
    }

    async def _fetch_one(session_, code: str) -> tuple[str, list[dict], bool]:
        try:
            navs = await _pingzhong_fetch_all_async(session_, code, headers)
            return code, navs[-NAV_RETENTION_DAYS:], False
        except Exception as e:
            logger.warning("基金 %s 全量净值拉取失败: %s", code, str(e)[:120], exc_info=True)
            return code, [], True

    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)

    async with httpx.AsyncClient(limits=limits, trust_env=False) as session:
        outcome = await run_batched_fetch(
            session, fetch_type="nav_full", label="全量净值",
            targets=all_codes, batch_size=batch_size,
            fetch_one=_fetch_one, handle_batch=_summarize_nav_results, backfill_one=_backfill_one,
            no_update_note="接口无净值数据", primary_note="全量净值拉取失败",
        )

        total_new = outcome["new_count"]
        logger.info(
            "全量净值更新完成: 新增 %d 条, 无数据 %d 只, 失败 %d/%d 只, 耗时 %.1f 秒",
            total_new, len(outcome["no_update"]), len(outcome["failed"]),
            len(all_codes), time.monotonic() - start_time,
        )
        if total_new == 0 and len(all_codes) > 100:
            logger.error(
                "全量净值下载写入 0 条但任务数 %d——疑似净值接口失效，请检查",
                len(all_codes),
            )

        # 覆盖度断言（ticket 25）：与增量路径同一闸门（首库自举也不能绕过）
        assert_nav_coverage(*repo.get_nav_time_state())

    return total_new


def prune_nav_history(retention: int = NAV_RETENTION_DAYS) -> int:
    """删除每只基金超出保留窗口的旧净值（存量一次性清理），返回删除行数。

    新写入路径由 save_nav_batch 自动修剪，本函数用于清理历史遗留的存量数据。
    """
    with db_conn() as conn:
        cur = conn.execute(
            "DELETE FROM fund_nav WHERE rowid IN ("
            "  SELECT rowid FROM ("
            "    SELECT rowid, ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) rk"
            "    FROM fund_nav) WHERE rk > ?)",
            (retention,),
        )
        return cur.rowcount


def fetch_fund_nav(code: str) -> list[dict]:
    """从 pingzhongdata 拉取单只基金历史净值（累计净值）。

    返回格式：[{"date": "2024-01-02", "cum_nav": 1.2345}, ...]
    """
    url = f"https://fund.eastmoney.com/pingzhongdata/{code}.js"
    resp = fetch(url)
    nav_list = _parse_pingzhong_acworth(resp.text)
    if not nav_list:
        # 已终止的后端份额等基金在天天基金已无净值页（404），属常态，降为 debug 避免刷屏
        logger.debug("基金 %s 未找到 ACWorthTrend", code)
    return nav_list


def fetch_fund_nav_incremental(code: str) -> int:
    """增量拉取单只基金净值（走 save_nav_batch 统一过滤+修剪），返回新增条数。"""
    navs = fetch_fund_nav(code)
    if not navs:
        return 0
    with db_conn() as conn:
        n = save_nav_batch(conn, code, navs)
        if n:
            conn.commit()
        return n

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

    - batch：本地最新 == 全局最新（只差最新 1 天）→ fundmobapi 批量
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


# ── 批量净值增量（fundmobapi 移动端接口，30 只/请求，2026-09 提速） ──
# lsjz 逐只拉取受单 IP ~3 QPS 限速，全市场 1.2 万只每日增量需 70+ 分钟；
# fundmobapi 一次返回最多 30 只的累计净值（口径与 lsjz LJJZ 一致），同样
# ~3 QPS 但吞吐 30 倍，把“差 1 天”的主流增量从逐只降到批量。
# 滞后基金（QDII/停更，差 2+ 天）仍走 lsjz 补全，本接口只覆盖最新单日。
_FUNDMOBAPI_BATCH = 30
_FUNDMOBAPI_URL = "https://fundmobapi.eastmoney.com/FundMNewApi/FundMNFInfo"


def _fundmobapi_url(codes: list[str]) -> str:
    return (
        f"{_FUNDMOBAPI_URL}?plat=Android&appType=ttjj&product=EFund"
        f"&Version=1&deviceid=1&Fcodes={','.join(codes)}"
    )


async def _fundmobapi_fetch_group(session, codes: list[str], headers: dict,
                                  timeout: float = 15) -> list[dict]:
    """批量拉取一组基金的最新累计净值（fundmobapi，<=30 只/请求）。

    返回 [{"code", "date", "cum_nav"}]；接口未返回的基金（停更/无净值/漏返）
    由调用方回退到 lsjz 逐只补全，不在此处判失败。
    """
    resp = await fetch_async(session, _fundmobapi_url(codes), timeout=timeout, headers=headers)
    data = json.loads(resp.text)
    out: list[dict] = []
    for d in data.get("Datas") or []:
        try:
            out.append({"code": d["FCODE"], "date": d["PDATE"], "cum_nav": float(d["ACCNAV"])})
        except (KeyError, TypeError, ValueError):
            continue
    return out


async def _fundmobapi_incremental(session, codes: list[str], headers: dict) -> tuple[list, list[str]]:
    """fundmobapi 批量增量：30 只/请求，semaphore 限流，失败重试。

    返回 (results, missing)：
    - results: [(code, [navs], False)] 成功项（navs 仅最新单日）
    - missing: 批量拉取失败或接口未返回的基金代码（回退 lsjz 逐只补全）
    """
    results: list[tuple[str, list[dict], bool]] = []
    missing: list[str] = []
    sem = asyncio.Semaphore(3)  # 服务端 ~3 QPS，并发 3 已到上限
    groups = [codes[i:i + _FUNDMOBAPI_BATCH] for i in range(0, len(codes), _FUNDMOBAPI_BATCH)]

    async def _one(group: list[str]) -> None:
        async with sem:
            for attempt in range(3):
                try:
                    navs = await _fundmobapi_fetch_group(session, group, headers)
                    returned = {n["code"] for n in navs}
                    for n in navs:
                        results.append((n["code"],
                                        [{"date": n["date"], "cum_nav": n["cum_nav"]}]))
                    for c in group:
                        if c not in returned:
                            missing.append(c)
                    return
                except Exception:
                    if attempt == 2:
                        missing.extend(group)
                        return
                    await asyncio.sleep(0.6)

    await asyncio.gather(*(_one(g) for g in groups))
    return results, missing


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

        # ── 三路拆分（2026-09 提速）：差 1 天批量 / 差多天 lsjz / 无本地 pingzhongdata ──
        # 占绝大多数的“差 1 天”基金走 fundmobapi 批量（30 只/请求），把全市场
        # 增量从 ~12690 次 lsjz 请求（~71 分钟）降到 ~423 次批量（~3 分钟）。
        # 滞后基金（QDII/停更，差 2+ 天）与无本地基金仍走原路径。
        batch_codes, lag_tasks, full_tasks = _split_tasks(tasks_meta, global_latest)

        total_new = 0
        success: set[str] = set()
        no_update: list[str] = []
        failed: list[str] = []

        if batch_codes:
            t0 = time.monotonic()
            logger.info("净值批量增量（fundmobapi 30只/请求）: %d 只（差 1 天）", len(batch_codes))
            batch_results, batch_missing = await _fundmobapi_incremental(session, batch_codes, headers)
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
            logger.info("净值批量增量完成: 成功 %d 只, 无新数据 %d 只, 缺失 %d 只, 耗时 %.1f 秒",
                        len(success), len(no_update), len(batch_missing), time.monotonic() - t0)

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

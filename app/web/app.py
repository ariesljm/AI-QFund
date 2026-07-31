"""FastAPI：将 docs/stitch_daily_fund_alpha (1)/code.html 对接真实数据。"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import collections
import logging
import threading
import time
from datetime import datetime
from contextlib import asynccontextmanager

import tomllib
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

import json

from app.database import get_db as _get_db, db_conn
from app.config import load_settings as _load_settings, save_settings as _save_settings, SETTINGS_PATH
from app.pipeline import run as run_full_pipeline
import app.repo as repo

logger = logging.getLogger("web")

TEMPLATES_DIR = Path(__file__).parent / "templates"

class _PipelineState:
    """管线运行时状态封装，避免模块级可变全局变量。"""

    def __init__(self):
        self.status: dict = {"state": "idle", "message": ""}
        self.logs: collections.deque = collections.deque(maxlen=200)
        self.last_run_date: str | None = None

    def add_log(self, msg: str) -> None:
        now = datetime.now().strftime("%H:%M:%S")
        line = f"{now} [pipeline] {msg}"
        self.logs.append(line)
        print(line, file=__import__("sys").stderr, flush=True)


_pipeline = _PipelineState()


class _PipelineLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            _pipeline.logs.append(self.format(record))
        except Exception:
            pass


def _run_pipeline_wrapper(force: bool = False):
    _pipeline.logs.clear()
    _pipeline.status = {"state": "running", "message": "管线启动..."}

    handler = _PipelineLogHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)-7s] %(name)s: %(message)s", datefmt="%H:%M:%S"))
    handler.setLevel(logging.INFO)
    root = logging.getLogger()
    root.addHandler(handler)

    try:
        _pipeline.add_log("[启动] 全量管线开始执行")
        run_full_pipeline(force=force)
        _pipeline.status = {"state": "done", "message": "管线执行完成"}
    except Exception as e:
        _pipeline.add_log(f"[错误] 管线执行失败: {e}")
        _pipeline.status = {"state": "error", "message": f"管线执行失败: {e}"}
    finally:
        root.removeHandler(handler)


def _scheduler_loop():
    _sched_logger = logging.getLogger("scheduler")

    import os as _os
    enabled = _os.environ.get("ENABLE_SCHEDULER", "true").lower() in ("1", "true", "yes")
    if not enabled:
        _sched_logger.info("调度器已通过 ENABLE_SCHEDULER 环境变量禁用")
        return

    _sched_logger.info("调度器启动，模式: daemon线程")
    while True:
        try:
            s = _load_settings()
            sched = s.get("scheduler", {})
            h = sched.get("hour", "")
            m = sched.get("minute", "")
            if h != "" and m != "":
                today = datetime.now().strftime("%Y-%m-%d")
                now = datetime.now()
                if now.hour == int(h) and now.minute == int(m) and _pipeline.last_run_date != today:
                    _sched_logger.info("定时触发: %s %02d:%02d", today, int(h), int(m))
                    _pipeline.last_run_date = today
                    _run_pipeline_wrapper()
            else:
                _sched_logger.debug("调度器已禁用（hour 为空）")
        except Exception as e:
            _sched_logger.error("调度器异常: %s", e, exc_info=True)
        time.sleep(60)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    conn = _get_db()
    conn.close()
    t = threading.Thread(target=_scheduler_loop, daemon=True)
    t.start()
    yield


app = FastAPI(title="AI Quant Terminal", lifespan=lifespan)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _q(sql: str, params: tuple = ()):
    with db_conn() as conn:
        r = conn.execute(sql, params).fetchall()
    return r


def _q1(sql: str, params: tuple = ()):
    with db_conn() as conn:
        r = conn.execute(sql, params).fetchone()
    return r


def _display_score(combo, raw_score):
    if combo is not None:
        return min(max(int(combo * 10 + 50), 0), 100)
    return min(max(int((raw_score or 0) * 500 + 50), 0), 100) if raw_score else 0


def _period_returns(code):
    """计算基金多周期收益率及同期沪深300收益。"""
    rows = _q(
        "SELECT date, cum_nav FROM fund_nav WHERE code=? ORDER BY date DESC LIMIT 250",
        (code,),
    )
    if not rows or not rows[0][1]:
        return {}
    rows.reverse()
    dates = [r[0] for r in rows]
    navs = [r[1] or 0 for r in rows]
    latest_nav = navs[-1]
    # 自然月 ≈ 22交易日，季≈66，半年≈126
    periods = {"月": 22, "季": 66, "半年": 126}
    hs_rows = _q(
        "SELECT date, close FROM index_daily WHERE code='sh000300' "
        "AND date >= ? ORDER BY date ASC",
        (dates[0],),
    )
    hs_map = {r[0]: r[1] for r in hs_rows}
    result = {}
    for label, lookback in periods.items():
        idx = max(0, len(navs) - 1 - lookback)
        old_nav = navs[idx]
        result[label] = round((latest_nav / old_nav - 1) * 100, 2) if old_nav else None
        old_date = dates[idx]
        old_hs = hs_map.get(old_date)
        latest_hs = hs_map.get(dates[-1])
        if old_hs and latest_hs:
            result[label + "_hs"] = round((latest_hs / old_hs - 1) * 100, 2)
        else:
            result[label + "_hs"] = None
    return result


def _nav_chart(code):
    """返回近3个月基金净值+沪深300数据，用于双线走势图。"""
    rows = _q(
        "SELECT date, cum_nav FROM fund_nav WHERE code=? ORDER BY date DESC LIMIT 65",
        (code,),
    )
    rows = list(reversed(rows))
    if not rows:
        return [], [], [], []
    # 基金净值归一化为收益率
    base_nav = rows[0][1] or 1
    nav_pcts = [round(((r[1] or 0) / base_nav - 1) * 100, 2) for r in rows]
    dates = [r[0] for r in rows]
    # 沪深300同日期
    hs_rows = _q(
        "SELECT date, close FROM index_daily WHERE code='sh000300' "
        "AND date >= ? ORDER BY date ASC",
        (dates[0],),
    )
    hs_map = {r[0]: r[1] for r in hs_rows}
    hs_pcts = []
    hs_dates = []
    base_hs = None
    for d in dates:
        v = hs_map.get(d)
        if v and base_hs is None:
            base_hs = v
        if v and base_hs:
            hs_pcts.append(round(((v / base_hs) - 1) * 100, 2))
            hs_dates.append(d)
    return nav_pcts, dates, hs_pcts, hs_dates


def _make_dual_svg(pcts, hs_pcts):
    """生成两条平滑SVG路径：基金(主色)和沪深300(灰色)。
    viewBox 200x100，根据数据范围自动缩放 Y 轴。
    返回 (fund_svg, hs_svg, baseline_y) 其中 baseline_y 是 0% 线在 SVG 中的 y 坐标。"""
    all_vals = [v for v in pcts if v is not None] + [v for v in hs_pcts if v is not None]
    if not all_vals:
        return "", "", 50
    y_min, y_max = min(all_vals), max(all_vals)
    y_range = y_max - y_min or 1
    pad = y_range * 0.1
    y_min -= pad
    y_max += pad
    y_range = y_max - y_min or 1

    def _y(v):
        return 90 - (v - y_min) / y_range * 80

    baseline_y = _y(0)

    def _smooth_path(data):
        n = len(data)
        if n < 2:
            return ""
        pts = [(i / (n - 1) * 200, _y(v)) for i, v in enumerate(data)]
        d = f"M {pts[0][0]:.1f},{pts[0][1]:.1f}"
        for i in range(n - 1):
            x0, y0 = pts[i]
            x1, y1 = pts[i + 1]
            mx = (x0 + x1) / 2
            d += f" C {mx:.1f},{y0:.1f} {mx:.1f},{y1:.1f} {x1:.1f},{y1:.1f}"
        return d
    return _smooth_path(pcts), _smooth_path(hs_pcts), baseline_y


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    today = datetime.now().strftime("%Y-%m-%d")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 今日推荐（最新 2 条 recommend_log）
    recs = repo.get_latest_recommendations(2)

    latest = None
    latest_rec_id = None
    latest_list: list[dict] = []
    for rec in recs:
        if latest_rec_id is None:
            latest_rec_id = rec["id"]
        entry = {
            "code": rec["code"], "name": rec["name"], "score": _display_score(rec["combo"], rec["score"]),
            "regime": rec["regime"] or "NEUTRAL", "reason": rec["reason"],
            "status": rec["status"], "date": rec["date"] or today, "return": rec["return"],
            "type": rec["type"] or "",
        }
        latest_list.append(entry)
        if latest is None:
            latest = entry

    # 宏观摘要（从 macro_news 取管线已入库的快讯，与 LLM 分析数据源一致）
    import re as _re
    news_items = ["暂无快讯"]
    sector_gainers = sector_losers = []
    flow_inflows = []
    flow_outflows = []
    sector_reasoning = ""
    regime_label = "NEUTRAL"
    mn = repo.get_latest_macro_news()
    if mn:
        text = mn.get("news_summary") or ""
        lines = text.replace("；", "\n").split("\n")
        seen = set()
        items = []
        for seg in lines:
            seg = seg.strip()
            if not seg or len(seg) < 6 or seg.startswith(("http", "www")):
                continue
            dedup = seg[:100] if len(seg) > 100 else seg
            if dedup in seen:
                continue
            seen.add(dedup)
            items.append(seg)
        if items:
            news_items = items
        top_gainers = mn.get("top_gainers") or ""
        top_losers = mn.get("top_losers") or ""
        etf_net_flow = mn.get("etf_net_flow") or ""
        gainer_seen = set()
        if top_gainers:
            for g in top_gainers.replace("、", "\n").split("\n"):
                g = g.strip()
                if g and g not in gainer_seen:
                    gainer_seen.add(g)
                    news_items.append("\u2191 " + g)
        if top_losers:
            for l in top_losers.replace("、", "\n").split("\n"):
                l = l.strip()
                if l:
                    news_items.append("\u2193 " + l)
        if etf_net_flow:
            news_items.insert(0, "\u8d44\u91d1\u6d41\u5411: " + etf_net_flow)
        # 领涨/领跌行业（各取前3，带幅度强度）
        if top_gainers:
            raw_g = _re.findall(r"([^(]+)\(([^)]+)\)", top_gainers)[:9]
            if raw_g:
                g = [(n.strip("、 "), float(p.replace("%", ""))) for n, p in raw_g]
                m = len(g)
                sector_gainers = [
                    {"name": n, "pct": f"{v:+.2f}%", "s": 1 - i / (m - 1) if m > 1 else 0.5}
                    for i, (n, v) in enumerate(g)
                ]
        if top_losers:
            raw_l = _re.findall(r"([^(]+)\(([^)]+)\)", top_losers)[:3]
            if raw_l:
                l = [(n.strip("、 "), float(p.replace("%", ""))) for n, p in raw_l]
                l.sort(key=lambda x: x[1])
                if l:
                    m = len(l)
                    sector_losers = [
                        {"name": n, "pct": f"{v:+.2f}%", "s": 1 - i / (m - 1) if m > 1 else 0.5}
                        for i, (n, v) in enumerate(l)
                    ]
                    sector_losers.reverse()  # 左浅右深：跌幅从小到大排列
        # 资金流向（flow_json 合并行）
        flow_inflows = mn.get("flow_inflows") or []
        flow_outflows = [
            {**s, "abs": abs(s.get("flow", 0) or 0)}
            for s in (mn.get("flow_outflows") or [])
        ]
        # 赛道分析（context_json 合并行）
        sector_reasoning = mn.get("sector_reasoning") or ""
        raw_regime = (mn.get("regime_label") or "neutral").upper()
        regime_label = raw_regime if raw_regime in ("BULL", "BEAR") else "NEUTRAL"
    macro_data = {
        "news": "；".join(news_items),
        "news_items": news_items,
        "top_gainers": [],
        "top_losers": [],
        "etf_net_flow": "",
    }

    # 行业热力图
    sectors = _q(
        "SELECT rbsa_industry_1, AVG(rbsa_weight_1), AVG(momentum_20d) "
        "FROM fund_features "
        "WHERE rbsa_industry_1 IS NOT NULL AND rbsa_industry_1 != '' "
        "GROUP BY rbsa_industry_1 ORDER BY AVG(rbsa_weight_1) DESC LIMIT 6"
    )
    sector_list = [
        {"name": s[0], "weight": round(s[1] or 0, 1), "momentum": round(s[2] or 0, 1)}
        for s in sectors
    ]

    # 基金池总数 + 按类型分组
    fund_pool, pool_by_type = repo.get_fund_pool_stats()
    pool_types = [{"type": t["type"], "count": t["count"]} for t in pool_by_type]

    # 追踪监控列表
    candidates = repo.get_tracking_list()
    candidate_list = []
    today_str = datetime.now().strftime("%Y-%m-%d")
    for c in candidates:
        code, first_date = c["code"], c["first_date"]
        name = c["name"] or ""
        rec_count = c["rec_count"]
        status = c["status"] or "HOLD"
        exit_date = c["exit_date"] or ""
        # 首次推荐净值（优先读 recommend_log.entry_nav，缺失时查 fund_nav 当日净值，无则 --）
        first_nav = repo.get_entry_nav(code, first_date)
        if first_nav is None:
            first_nav = repo.get_nav_at_date(code, first_date)
        # 当前净值（取最新盘后净值，今日无则自动回退到前一日）
        cur_nav = repo.get_latest_nav(code) if first_nav is not None else None
        # 累计收益
        ret = None
        if first_nav and cur_nav and first_nav > 0:
            ret = round((cur_nav / first_nav - 1) * 100, 2)
        candidate_list.append({
            "code": code, "name": name,
            "first_date": first_date or "",
            "first_nav": round(first_nav, 4) if first_nav else None,
            "cur_nav": round(cur_nav, 4) if cur_nav else None,
            "return": ret,
            "rec_count": rec_count,
            "status": status,
            "exit_date": exit_date,
            "type": "",
        })
    # 累计收益总和
    total_return = round(sum(c["return"] for c in candidate_list if c["return"] is not None), 2) if candidate_list else 0
    rec_count = len(candidate_list)
    hit_count = sum(1 for c in candidate_list if c["return"] is not None and c["return"] > 0)
    hit_rate = round(hit_count / rec_count * 100, 1) if rec_count > 0 else 0

    # 净值图表（近3个月双线走势）
    nav_pcts, nav_dates, hs_pcts, hs_dates = _nav_chart(latest["code"]) if latest else ([], [], [], [])
    fund_svg, hs_svg, baseline_y = _make_dual_svg(nav_pcts, hs_pcts)
    period_ret = _period_returns(latest["code"]) if latest else {}

    # 基金特征画像
    fund_features = None
    if latest:
        feat = repo.get_latest_features(latest["code"])
        if feat:
            fund_features = {
                "hurst": feat["hurst_60d"],
                "momentum": round(feat["momentum_20d"] or 0, 2) if feat["momentum_20d"] is not None else None,
                "calmar": round(feat["calmar"] or 0, 2) if feat["calmar"] is not None else None,
                "downside_vol": round(feat["downside_vol"] or 0, 2) if feat["downside_vol"] is not None else None,
                "capture_up": round(feat["capture_up"] or 0, 1) if feat["capture_up"] is not None else None,
                "capture_down": round(feat["capture_down"] or 0, 1) if feat["capture_down"] is not None else None,
                "bias": round(feat["bias_60d"] or 0, 2) if feat["bias_60d"] is not None else None,
                "top_industry": feat["rbsa_industry_1"] or "",
                "top_industry_weight": round(feat["rbsa_weight_1"] or 0, 1),
            }

    # 持仓透视
    top_holdings = []
    if latest:
        top_holdings = [
            {"code": h["stock_code"], "name": h["stock_name"], "weight": h["weight"],
             "industry": h["industry"] or ""}
            for h in repo.get_holdings(latest["code"], 10)
        ]

    top_holdings2 = []
    if len(latest_list) > 1:
        top_holdings2 = [
            {"code": h["stock_code"], "name": h["stock_name"], "weight": h["weight"],
             "industry": h["industry"] or ""}
            for h in repo.get_holdings(latest_list[1]["code"], 10)
        ]

    # 运行天数
    uptime_days = repo.get_uptime_days()

    # 超额阿尔法（系统运行以来累计超额收益 = total_return - 同期沪深300涨幅）
    alpha = None
    alpha_pcts = []
    start_date = repo.get_first_reco_date()
    if start_date and total_return is not None:
        hs300_start = repo.get_index_close("sh000300", start_date)
        hs300_now = repo.get_index_close("sh000300")
        if hs300_start and hs300_now:
            hs300_pct = round((hs300_now / hs300_start - 1) * 100, 2)
            alpha = round(total_return - hs300_pct, 2)
    # 逐基金alpha贡献（按推荐日期排序，用于alpha曲线）
    # 对已平仓基金用 exit_date 截断持有期，避免基准延伸至今日
    sorted_candidates = sorted(candidate_list, key=lambda x: x["first_date"] or "")
    cum_alpha = 0.0
    for c in sorted_candidates:
        if c["return"] is not None and c["first_date"]:
            hs_start = repo.get_index_close("sh000300", c["first_date"])
            end_str = c["exit_date"] or today_str
            hs_end = repo.get_index_close("sh000300", end_str)
            if hs_start and hs_end:
                hs_ret = (hs_end / hs_start - 1) * 100
                fund_ret = c["return"]
                if c.get("exit_date") and c["status"] == "EXIT":
                    end_nav = repo.get_nav_at_or_before(c["code"], end_str)
                    if end_nav and c["first_nav"] and c["first_nav"] > 0:
                        fund_ret = round((end_nav / c["first_nav"] - 1) * 100, 2)
                cum_alpha += fund_ret - hs_ret
                alpha_pcts.append(round(cum_alpha, 2))
    # alpha曲线SVG（自动缩放）
    alpha_svg = ""
    alpha_baseline_y = 50
    if len(alpha_pcts) >= 1:
        a_min, a_max = min(alpha_pcts), max(alpha_pcts)
        a_range = a_max - a_min or 1
        a_pad = a_range * 0.1
        a_min -= a_pad
        a_max += a_pad
        a_range = a_max - a_min or 1
        def _ay(v): return 90 - (v - a_min) / a_range * 80
        alpha_baseline_y = _ay(0)
        if len(alpha_pcts) == 1:
            y = _ay(alpha_pcts[0])
            alpha_svg = f"M 0,{y:.1f} L 200,{y:.1f}"
        else:
            n = len(alpha_pcts)
            pts = [(i / (n - 1) * 200, _ay(v)) for i, v in enumerate(alpha_pcts)]
            d = f"M {pts[0][0]:.1f},{pts[0][1]:.1f}"
            for i in range(len(pts) - 1):
                x0, y0 = pts[i]
                x1, y1 = pts[i + 1]
                mx = (x0 + x1) / 2
                d += f" C {mx:.1f},{y0:.1f} {mx:.1f},{y1:.1f} {x1:.1f},{y1:.1f}"
            alpha_svg = d

    max_inflow = max((s.get("flow", 0) or 0 for s in flow_inflows), default=0)
    max_outflow = max((abs(s.get("flow", 0) or 0) for s in flow_outflows), default=0)
    return templates.TemplateResponse(request, "index.html", {
        "latest": latest,
        "latest_list": latest_list,
        "latest_rec_id": latest_rec_id or 0,
        "macro": macro_data,
        "candidates": candidate_list,
        "fund_pool": fund_pool,
        "pool_types": pool_types,
        "now": now_str,
        "today": today,
        "sector_list": sector_list,
        "sector_reasoning": sector_reasoning,
        "regime_label": regime_label,
        "nav_pcts": nav_pcts,
        "nav_dates": nav_dates,
        "hs_pcts": hs_pcts,
        "fund_svg": fund_svg,
        "hs_svg": hs_svg,
        "baseline_y": baseline_y,
        "period_ret": period_ret,
        "fund_features": fund_features,
        "top_holdings": top_holdings,
        "top_holdings2": top_holdings2,
        "sector_gainers": sector_gainers,
        "sector_losers": sector_losers,
        "uptime_days": uptime_days,
        "alpha": alpha,
        "alpha_svg": alpha_svg,
        "alpha_baseline_y": alpha_baseline_y,
        "total_return": total_return,
        "rec_count": rec_count,
        "hit_rate": hit_rate,
        "flow_inflows": flow_inflows,
        "flow_outflows": flow_outflows,
        "max_inflow": max_inflow,
        "max_outflow": max_outflow,
    })


@app.get("/api/logs")
async def get_logs(lines: int = 200, after: int = 0):
    """从 SQLite 返回日志；after 为上次读取的最大 id（增量游标，轮转/清理后仍可靠）。"""
    from app.utils.log import SYSTEM_LOG_TABLE_SQL
    with db_conn() as conn:
        conn.execute(SYSTEM_LOG_TABLE_SQL)
        total = conn.execute("SELECT COUNT(*) FROM system_logs").fetchone()[0]
        if after <= 0:
            rows = conn.execute(
                "SELECT id, ts, level, logger, event, message, correlation_id "
                "FROM system_logs ORDER BY id DESC LIMIT ?",
                (lines,),
            ).fetchall()
            rows.reverse()
        else:
            rows = conn.execute(
                "SELECT id, ts, level, logger, event, message, correlation_id "
                "FROM system_logs WHERE id > ? ORDER BY id LIMIT ?",
                (after, lines),
            ).fetchall()
    out = []
    last_id = after
    for r in rows:
        last_id = r[0]
        out.append(json.dumps({
            "timestamp": r[1],
            "level": r[2],
            "logger": r[3],
            "event": r[4],
            "message": r[5],
            "correlation_id": r[6],
        }, ensure_ascii=False))
    return {"lines": out, "total": total, "last_id": last_id}


@app.get("/api/settings")
async def get_settings():
    s = _load_settings()
    s.get("web", {}).pop("settings_password", None)
    return s


@app.post("/api/settings")
async def save_settings(body: dict):
    try:
        _save_settings(body)
        return {"status": "ok"}
    except Exception as e:
        logger.error("保存设置失败: %s", e)
        return {"status": "error", "message": str(e)}


@app.post("/api/check-password")
async def check_password(body: dict):
    s = _load_settings()
    pwd = (s.get("web", {}) or {}).get("settings_password", "") or ""
    if not pwd:
        return {"ok": True}
    return {"ok": body.get("password", "") == pwd}


@app.post("/api/run-pipeline")
async def run_pipeline():
    logger = logging.getLogger("web")
    try:
        t = threading.Thread(target=_run_pipeline_wrapper, args=(True,), daemon=True)
        t.start()
        logger.info("管线手动触发成功")
        return {"status": "started"}
    except Exception as e:
        logger.error("管线手动触发失败: %s", e)
        return {"status": "error", "detail": str(e)}


@app.get("/api/pipeline-status")
async def get_pipeline_status():
    return _pipeline.status


@app.post("/api/clear-recommendations")
async def clear_recommendations(body: dict | None = None):
    """清除推荐决策域数据（推荐记录、赛道选择、监控事件、进化洞察）。

    dry_run=true 时仅返回各表行数不删除（前端确认弹窗用）。
    管线运行中拒绝执行；保留底层数据与 meta 配置。
    """
    if _pipeline.status.get("state") == "running":
        return {"status": "error", "message": "管线运行中，请稍后再试"}
    from app.repo import clear_recommendations as _clear
    from app.database import db_conn as _db_conn
    body = body or {}
    dry_run = bool(body.get("dry_run"))
    if dry_run:
        with _db_conn() as conn:
            counts = {
                "recommend_log": conn.execute("SELECT COUNT(*) FROM recommend_log").fetchone()[0],
                "sector_selections": conn.execute("SELECT COUNT(*) FROM sector_selections").fetchone()[0],
                "monitor_events": conn.execute("SELECT COUNT(*) FROM monitor_events").fetchone()[0],
                "evolution_insights": conn.execute("SELECT COUNT(*) FROM evolution_insights").fetchone()[0],
            }
        return {"status": "ok", "dry_run": True, "deleted": counts}
    try:
        counts = _clear()
        return {"status": "ok", "deleted": counts}
    except Exception as e:
        logger.error("清除推荐数据失败: %s", e)
        return {"status": "error", "message": str(e)}


@app.get("/api/recommendation-status")
async def recommendation_status():
    """返回最新推荐ID和时间，前端用于检测推荐是否更新。"""
    rid, created_at = repo.get_latest_reco_id()
    return {"id": rid, "updated_at": created_at}


@app.get("/api/pipeline-log")
async def get_pipeline_log(since: int = 0):
    """返回管线日志，since 为上次读取的行数。"""
    items = list(_pipeline.logs)
    return {"lines": items[since:], "total": len(items)}


@app.get("/api/fund-detail/{code}")
async def get_fund_detail(code: str):
    """返回指定基金的首次推荐分析、理由、十大持仓、净值走势数据。"""
    rec = repo.get_fund_detail(code)
    if not rec:
        return {"error": "未找到该基金的推荐记录"}

    fund_info = {
        "code": code,
        "name": rec["name"] or code,
        "type": rec["type"] or "",
        "first_date": rec["first_date"] or "",
        "entry_nav": rec["entry_nav"],
        "buy_reason": rec["buy_reason"],
        "score": rec["score"],
        "combo": rec["combo"],
        "regime": rec["regime"] or "NEUTRAL",
        "status": rec["status"] or "HOLD",
        "display_score": _display_score(rec["combo"], rec["score"]),
    }

    top_holdings = [
        {"stock_code": h["stock_code"], "stock_name": h["stock_name"], "weight": h["weight"],
         "industry": h["industry"] or ""}
        for h in repo.get_holdings(code, 10)
    ]

    nav_data = [
        {"date": r[0], "nav": round(r[1], 4) if r[1] else None}
        for r in repo.get_nav_history(code, 90)
    ]

    signal = _q1(
        "SELECT signal, logic_verdict, sector_risk, holding_risk, detail, date "
        "FROM monitor_events WHERE code=? ORDER BY date DESC LIMIT 1",
        (code,),
    )
    current_signal = None
    if signal:
        detail = signal[4] or ""
        try:
            import json
            detail_obj = json.loads(detail)
            reason = detail_obj.get("reason", detail)
        except Exception:
            reason = detail
        current_signal = {
            "signal": signal[0],
            "logic_verdict": signal[1] or "",
            "sector_risk": bool(signal[2]),
            "holding_risk": bool(signal[3]),
            "reason": reason,
            "date": signal[5] or "",
        }

    return {
        "fund": fund_info,
        "top_holdings": top_holdings,
        "nav_data": nav_data,
        "current_signal": current_signal,
    }


if __name__ == "__main__":
    import uvicorn
    import os as _os
    try:
        settings = _load_settings()
        port = int(settings.get("web", {}).get("port", 9123))
    except Exception as e:
        logger.warning("设置文件加载失败(使用默认端口): %s", e)
        port = 9123
    reload = _os.environ.get("UVICORN_RELOAD", "").lower() in ("1", "true", "yes")
    uvicorn.run("app.web.app:app", host="0.0.0.0", port=port, reload=reload)

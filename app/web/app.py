"""FastAPI：将 docs/stitch_daily_fund_alpha (1)/code.html 对接真实数据。"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import collections
import json
import logging
import re
import threading
import time
import asyncio
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

import tomllib
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.database import get_db as _get_db, db_conn
from app.config import load_settings as _load_settings, save_settings as _save_settings, SETTINGS_PATH
from app.pipeline import run as run_full_pipeline
from app.engine.valuation import (portfolio_series as _portfolio_series,
                                  period_returns as _period_returns,
                                  sharpe_ratio as _sharpe_ratio,
                                  max_drawdown as _max_drawdown,
                                  alpha_series as _alpha_series)
import app.repo as repo
from app import domain

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
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class _IndexQuoteCache:
    """实时指数行情：15 秒实时缓存，抓取失败降级为数据库最近收盘（60 秒缓存）。"""

    def __init__(self):
        self.data: dict | None = None
        self.expires: float = 0.0
        self.ttl_live = 15.0
        self.ttl_fallback = 60.0

    async def get(self) -> dict:
        now = time.time()
        if self.data is not None and now < self.expires:
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
        from app.data.fetchers import fetch
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
        """降级：沪深300 取数据库最近收盘（含最近两日涨跌幅），上证无历史数据标记不可用。"""
        items = []
        rows = sorted(repo.get_index_series("sh000300", ("date", "close")), key=lambda r: r[0])
        if rows:
            price = rows[-1][1]
            pct = None
            if len(rows) >= 2 and rows[-2][1]:
                pct = round((rows[-1][1] / rows[-2][1] - 1) * 100, 2)
            items.append({"code": "sh000300", "name": "沪深300", "price": price,
                          "change_percent": pct, "date": rows[-1][0], "source": "closed"})
        items.append({"code": "sh000001", "name": "上证指数", "price": None,
                      "change_percent": None, "source": "unavailable"})
        return items


_index_quote = _IndexQuoteCache()


@app.get("/api/indices")
async def get_indices():
    """实时指数行情（后端代理 + 15s 缓存；失败降级收盘价，前端据此标注）。"""
    return await _index_quote.get()


@app.get("/api/pipeline-schedule")
async def get_pipeline_schedule():
    """管线自动执行状态：下次执行时间 + 上次执行结果（页面状态卡用）。"""
    s = _load_settings()
    sched = s.get("scheduler", {}) or {}
    h, m = sched.get("hour", ""), sched.get("minute", "")
    enabled = h != "" and h is not None
    next_run = None
    if enabled:
        now = datetime.now()
        run = now.replace(hour=int(h), minute=int(m or 0), second=0, microsecond=0)
        if run <= now:
            run = run + timedelta(days=1)
        next_run = run.strftime("%Y-%m-%d %H:%M")
    return {
        "enabled": enabled,
        "next_run": next_run,
        "last_run_date": _pipeline.last_run_date,
        "state": _pipeline.status.get("state"),
        "message": _pipeline.status.get("message"),
        "now": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def _macro_summary(mn):
    """宏观摘要解析：新闻列表/领涨领跌/资金流向/赛道分析/大盘状态归一。"""
    news_items = ["暂无快讯"]
    sector_gainers = sector_losers = []
    flow_inflows = []
    flow_outflows = []
    sector_reasoning = ""
    regime_label = "NEUTRAL"
    if mn:
        text = mn.get("news_summary") or ""
        lines = text.split("\n")
        seen = set()
        items = []
        for seg in lines:
            seg = seg.strip()
            if not seg or len(seg) < 6 or seg.startswith(("http", "www")):
                continue
            title = seg.split("：", 1)[0].strip()
            if not title:
                continue
            dedup = title[:100] if len(title) > 100 else title
            if dedup in seen:
                continue
            seen.add(dedup)
            items.append(title)
        if items:
            news_items = items
        top_gainers = mn.get("top_gainers") or ""
        top_losers = mn.get("top_losers") or ""
        # 领涨/领跌行业（各取前3，带幅度强度）
        if top_gainers:
            raw_g = re.findall(r"([^(]+)\(([^)]+)\)", top_gainers)[:9]
            if raw_g:
                g = [(n.strip("、 "), float(p.replace("%", ""))) for n, p in raw_g]
                m = len(g)
                sector_gainers = [
                    {"name": n, "pct": f"{v:+.2f}%", "s": 1 - i / (m - 1) if m > 1 else 0.5}
                    for i, (n, v) in enumerate(g)
                ]
        if top_losers:
            raw_l = re.findall(r"([^(]+)\(([^)]+)\)", top_losers)[:3]
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
        # 大盘状态（LLM 可能输出 bullish/bearish/bull/bear 等变体，统一归一）
        regime_label = domain.normalize_regime_label(mn.get("regime_label"))
    macro_data = {
        "news": "；".join(news_items),
        "news_items": news_items,
        "top_gainers": [],
        "top_losers": [],
        "etf_net_flow": "",
    }
    max_inflow = max((s.get("flow", 0) or 0 for s in flow_inflows), default=0)
    max_outflow = max((abs(s.get("flow", 0) or 0) for s in flow_outflows), default=0)
    return {
        "macro": macro_data,
        "sector_gainers": sector_gainers,
        "sector_losers": sector_losers,
        "flow_inflows": flow_inflows,
        "flow_outflows": flow_outflows,
        "max_inflow": max_inflow,
        "max_outflow": max_outflow,
        "sector_reasoning": sector_reasoning,
        "regime_label": regime_label,
    }


def _candidate_summary(candidates):
    """追踪监控列表 → 展示项 + 累计收益/命中率统计。"""
    candidate_list = []
    for c in candidates:
        code, first_date = c["code"], c["first_date"]
        name = c["name"] or ""
        rec_count = c["rec_count"]
        # 展示状态与基金详情一致：取 monitor_events 最新监控信号（无信号时回退推荐状态）
        status = repo.get_latest_signal(code) or (c["status"] or "HOLD")
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
    return candidate_list, total_return, rec_count, hit_rate


def _fund_profile_block(code):
    """基金特征画像 + 十大持仓（最新推荐 / 次新推荐复用）。"""
    fund_features = None
    feat = repo.get_latest_features(code)
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
    top_holdings = [
        {"code": h["stock_code"], "name": h["stock_name"], "weight": h["weight"],
         "industry": h["industry"] or ""}
        for h in repo.get_holdings(code, 10)
    ]
    return fund_features, top_holdings


def _alpha_curve_svg(alpha_pcts):
    """逐基金 alpha 贡献曲线 SVG（自动缩放）。返回 (svg, baseline_y)。"""
    if not alpha_pcts:
        return "", 50
    a_min, a_max = min(alpha_pcts), max(alpha_pcts)
    a_range = a_max - a_min or 1
    a_pad = a_range * 0.1
    a_min -= a_pad
    a_max += a_pad
    a_range = a_max - a_min or 1

    def _ay(v):
        return 90 - (v - a_min) / a_range * 80

    baseline_y = _ay(0)
    if len(alpha_pcts) == 1:
        y = _ay(alpha_pcts[0])
        return f"M 0,{y:.1f} L 200,{y:.1f}", baseline_y
    n = len(alpha_pcts)
    pts = [(i / (n - 1) * 200, _ay(v)) for i, v in enumerate(alpha_pcts)]
    d = f"M {pts[0][0]:.1f},{pts[0][1]:.1f}"
    for i in range(len(pts) - 1):
        x0, y0 = pts[i]
        x1, y1 = pts[i + 1]
        mx = (x0 + x1) / 2
        d += f" C {mx:.1f},{y0:.1f} {mx:.1f},{y1:.1f} {x1:.1f},{y1:.1f}"
    return d, baseline_y


def _alpha_block(candidate_list, total_return):
    """超额阿尔法（跑赢沪深300）+ 逐基金 alpha 贡献曲线。"""
    alpha = None
    start_date = repo.get_first_reco_date()
    if start_date and total_return is not None:
        hs300_start = repo.get_index_close("sh000300", start_date)
        hs300_now = repo.get_index_close("sh000300")
        if hs300_start and hs300_now:
            hs300_pct = round((hs300_now / hs300_start - 1) * 100, 2)
            alpha = round(total_return - hs300_pct, 2)
    alpha_svg, alpha_baseline_y = _alpha_curve_svg(_alpha_series(candidate_list))
    return alpha, alpha_svg, alpha_baseline_y


def _nav_chart(code):
    """返回近3个月基金净值+沪深300数据，用于双线走势图。"""
    rows = repo.get_nav_history(code, 65)
    if not rows:
        return [], [], [], []
    # 基金净值归一化为收益率
    base_nav = rows[0][1] or 1
    nav_pcts = [round(((r[1] or 0) / base_nav - 1) * 100, 2) for r in rows]
    dates = [r[0] for r in rows]
    # 沪深300同日期
    hs_rows = repo.get_index_series("sh000300", ("date", "close"), dates[0])
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


def _quality_curve_svg(points):
    """累计超额曲线 SVG（单线），points=[{cum_alpha,...}] 按时间序。返回 (path, baseline_y)。"""
    vals = [float(p["cum_alpha"]) for p in points if p.get("cum_alpha") is not None]
    if len(vals) < 2:
        return "", 50
    y_min, y_max = min(vals), max(vals)
    y_range = y_max - y_min or 1
    pad = y_range * 0.15
    y_min -= pad
    y_max += pad
    y_range = y_max - y_min or 1

    def _y(v):
        return 90 - (v - y_min) / y_range * 80

    baseline_y = _y(0)
    n = len(vals)
    pts = [(i / (n - 1) * 200, _y(v)) for i, v in enumerate(vals)]
    d = f"M {pts[0][0]:.1f},{pts[0][1]:.1f}"
    for i in range(n - 1):
        x0, y0 = pts[i]
        x1, y1 = pts[i + 1]
        mx = (x0 + x1) / 2
        d += f" C {mx:.1f},{y0:.1f} {mx:.1f},{y1:.1f} {x1:.1f},{y1:.1f}"
    return d, baseline_y


def _index_context() -> dict:
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
            "code": rec["code"], "name": rec["name"],
            "pred_alpha": rec["score"],
            "regime": rec["regime"] or "NEUTRAL", "reason": rec["reason"],
            "status": rec["status"], "date": rec["date"] or today, "return": rec["return"],
            "type": rec["type"] or "",
        }
        latest_list.append(entry)
        if latest is None:
            latest = entry

    # 宏观摘要（从 macro_news 取管线已入库的快讯，与 LLM 分析数据源一致）
    mn = repo.get_latest_macro_news()
    macro = _macro_summary(mn)
    macro_data = macro["macro"]
    sector_gainers = macro["sector_gainers"]
    sector_losers = macro["sector_losers"]
    flow_inflows = macro["flow_inflows"]
    flow_outflows = macro["flow_outflows"]
    max_inflow = macro["max_inflow"]
    max_outflow = macro["max_outflow"]
    sector_reasoning = macro["sector_reasoning"]
    regime_label = macro["regime_label"]
    empty_today = None
    _empty_reco = repo.get_empty_recommendation(today)
    if _empty_reco:
        empty_today = _empty_reco
    quality_metrics = repo.get_quality_metrics(6)
    quality_curve_svg = ""
    quality_curve_baseline = 50
    if quality_metrics:
        _pts = quality_metrics[0].get("points") or []
        if len(_pts) >= 2:
            quality_curve_svg, quality_curve_baseline = _quality_curve_svg(_pts)

    # 行业热力图
    sectors = repo.get_sector_heatmap()
    sector_list = [
        {"name": s[0], "weight": round(s[1] or 0, 1), "momentum": round(s[2] or 0, 1)}
        for s in sectors
    ]

    # 基金池总数 + 按类型分组
    fund_pool, pool_by_type = repo.get_fund_pool_stats()
    pool_types = [{"type": t["type"], "count": t["count"]} for t in pool_by_type]

    # 追踪监控列表 + 累计收益/命中率
    candidate_list, total_return, rec_count, hit_rate = _candidate_summary(repo.get_tracking_list())

    # 净值图表（近3个月双线走势）
    nav_pcts, nav_dates, hs_pcts, hs_dates = _nav_chart(latest["code"]) if latest else ([], [], [], [])
    fund_svg, hs_svg, baseline_y = _make_dual_svg(nav_pcts, hs_pcts)
    period_ret = _period_returns(latest["code"]) if latest else {}
    period_ret2 = _period_returns(latest_list[1]["code"]) if len(latest_list) > 1 else {}

    # 基金特征画像 + 十大持仓（最新 / 次新推荐）
    fund_features, top_holdings = _fund_profile_block(latest["code"]) if latest else (None, [])
    top_holdings2 = _fund_profile_block(latest_list[1]["code"])[1] if len(latest_list) > 1 else []

    # 运行天数
    uptime_days = repo.get_uptime_days()

    # 超额阿尔法 + 逐基金 alpha 贡献曲线
    alpha, alpha_svg, alpha_baseline_y = _alpha_block(candidate_list, total_return)

    # 等权组合累计收益序列（用于 Alpha 双线图 + 夏普/回撤）
    _, port_pcts, port_hs_pcts = _portfolio_series()
    portfolio_svg = ""
    portfolio_hs_svg = ""
    portfolio_baseline_y = 50
    sharpe_ratio = None
    max_drawdown = None
    if port_pcts:
        portfolio_svg, portfolio_hs_svg, portfolio_baseline_y = _make_dual_svg(port_pcts, port_hs_pcts)
        sharpe_ratio = _sharpe_ratio(port_pcts)
        max_drawdown = _max_drawdown(port_pcts)
    # 最新一期质量度量（IC 与超额胜率，用于 Alpha 图浮动框）
    latest_ic = None
    latest_excess_win_rate = None
    if quality_metrics:
        latest_ic = quality_metrics[0].get("ic")
        latest_excess_win_rate = quality_metrics[0].get("excess_win_rate")
    return {
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
        "period_ret2": period_ret2,
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
        "empty_today": empty_today,
        "quality_metrics": quality_metrics,
        "quality_curve_svg": quality_curve_svg,
        "quality_curve_baseline": quality_curve_baseline,
        "portfolio_svg": portfolio_svg,
        "portfolio_hs_svg": portfolio_hs_svg,
        "portfolio_baseline_y": portfolio_baseline_y,
        "sharpe_ratio": sharpe_ratio,
        "max_drawdown": max_drawdown,
        "latest_ic": latest_ic,
        "latest_excess_win_rate": latest_excess_win_rate,
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", _index_context())


@app.get("/v1", response_class=HTMLResponse)
async def index_v1(request: Request):
    return templates.TemplateResponse(request, "index_v1.html", _index_context())


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
    body = body or {}
    dry_run = bool(body.get("dry_run"))
    if dry_run:
        return {"status": "ok", "dry_run": True, "deleted": repo.count_recommendation_domain()}
    try:
        counts = repo.clear_recommendations()
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
        "display_score": domain.display_score(rec["combo"], rec["score"]),
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

    signal = repo.get_latest_monitor_event(code)
    current_signal = None
    if signal:
        # monitor 写入的是 "; " 拼接的纯文本原因，无需（也无法）按 JSON 解析
        reason = signal[4] or ""
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

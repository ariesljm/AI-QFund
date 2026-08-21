"""FastAPI：将 docs/stitch_daily_fund_alpha (1)/code.html 对接真实数据。"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import json
import logging
import threading
from collections.abc import AsyncIterator
from datetime import datetime

from fastapi import FastAPI, Request, Header, HTTPException, Depends
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.database import get_db as _get_db
from app.config import load_settings as _load_settings, save_settings as _save_settings
from app.web import runner, quotes, dashboard
import app.repo as repo
from app import domain
from app.engine.valuation import period_returns

logger = logging.getLogger("web")

TEMPLATES_DIR = Path(__file__).parent / "templates"


async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    conn = _get_db()
    conn.close()
    t = threading.Thread(target=runner.scheduler_loop, daemon=True)
    t.start()
    yield


app = FastAPI(title="AI Quant Terminal", lifespan=lifespan)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# 进程启动时刻：用于界面显示系统运行累计时间
_PROCESS_START = datetime.now()


@app.get("/api/indices")
async def get_indices() -> dict:
    """实时指数行情（后端代理 + 15s 缓存；失败降级收盘价，前端据此标注）。"""
    return await quotes.index_quote.get()


@app.get("/api/pipeline-schedule")
async def get_pipeline_schedule() -> dict[str, object]:
    """管线自动执行状态：各槽位下次执行时间 + 上次执行结果（页面状态卡用）。

    下次执行推算收敛进 runner.next_run_for 窄读（架构深化 K）：触发侧与展示侧
    同一口径（hour+minute 都非空才启用），不再各自重算。
    """
    next_run = runner.next_run_for("全流程")
    return {
        "enabled": next_run is not None,
        "next_run": next_run,
        "last_run_date": runner.last_run_date(),
        "state": runner.pipeline.status.get("state"),
        "message": runner.pipeline.status.get("message"),
        "started_at": _PROCESS_START.astimezone().isoformat(),
        "now": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html", dashboard.index_context())


@app.get("/v1", response_class=HTMLResponse)
async def index_v1(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index_v1.html", dashboard.index_context())


@app.get("/api/logs")
async def get_logs(lines: int = 200, after: int = 0, before: int = 0) -> dict[str, object]:
    """从 SQLite 返回日志；after 为增量游标（拉新），before 为向前翻页游标（拉更早）。"""
    rows, total, last_id = repo.get_system_logs(lines, after, before)
    out = []
    for r in rows:
        out.append(json.dumps({
            "id": r[0],
            "timestamp": r[1],
            "level": r[2],
            "logger": r[3],
            "event": r[4],
            "message": r[5],
            "correlation_id": r[6],
        }, ensure_ascii=False))
    return {"lines": out, "total": total, "last_id": last_id}


@app.get("/api/settings")
async def get_settings() -> dict:
    s = _load_settings()
    s.get("web", {}).pop("settings_password", None)
    return s


def _settings_password() -> str:
    """设置密码读取（写接口鉴权/密码校验共用，未设置时返回空串）。"""
    s = _load_settings()
    return (s.get("web", {}) or {}).get("settings_password", "") or ""


def _require_settings_auth(x_settings_password: str | None = Header(default=None)) -> None:
    """写操作鉴权：校验 X-Settings-Password 头；密码为空（未设置）时放行。

    防止绕过前端密码弹窗直接调用写接口（改设置/清数据/触发管线）。
    """
    pwd = _settings_password()
    if pwd and x_settings_password != pwd:
        raise HTTPException(status_code=403, detail="密码错误")


@app.post("/api/settings")
async def save_settings(body: dict[str, object], _auth: None = Depends(_require_settings_auth)) -> dict[str, object]:
    try:
        _save_settings(body)
        return {"status": "ok"}
    except Exception as e:
        logger.error("保存设置失败: %s", e, exc_info=True)
        return {"status": "error", "message": str(e)}


@app.post("/api/check-password")
async def check_password(body: dict[str, object]) -> dict[str, bool]:
    pwd = _settings_password()
    if not pwd:
        return {"ok": True}
    return {"ok": body.get("password", "") == pwd}


@app.post("/api/run-pipeline")
async def run_pipeline(_auth: None = Depends(_require_settings_auth)) -> dict[str, object]:
    try:
        t = threading.Thread(target=runner.run_pipeline_wrapper, daemon=True)
        t.start()
        logger.info("管线手动触发成功")
        return {"status": "started"}
    except Exception as e:
        logger.error("管线手动触发失败: %s", e, exc_info=True)
        return {"status": "error", "detail": str(e)}


@app.get("/api/pipeline-status")
async def get_pipeline_status() -> dict[str, object]:
    return runner.pipeline.status


@app.post("/api/clear-recommendations")
async def clear_recommendations(body: dict[str, object] | None = None,
                               _auth: None = Depends(_require_settings_auth)) -> dict[str, object]:
    """清除推荐决策域数据（推荐记录、赛道选择、监控事件、进化洞察）。

    dry_run=true 时仅返回各表行数不删除（前端确认弹窗用）。
    管线运行中拒绝执行；保留底层数据与 meta 配置。
    """
    if runner.pipeline.status.get("state") == "running":
        return {"status": "error", "message": "管线运行中，请稍后再试"}
    body = body or {}
    dry_run = bool(body.get("dry_run"))
    if dry_run:
        return {"status": "ok", "dry_run": True, "deleted": repo.count_recommendation_domain()}
    try:
        counts = repo.clear_recommendations()
        return {"status": "ok", "deleted": counts}
    except Exception as e:
        logger.error("清除推荐数据失败: %s", e, exc_info=True)
        return {"status": "error", "message": str(e)}


@app.get("/api/recommendation-status")
async def recommendation_status() -> dict[str, object]:
    """返回最新推荐ID和时间，前端用于检测推荐是否更新。"""
    rid, created_at = repo.get_latest_reco_id()
    return {"id": rid, "updated_at": created_at}


@app.get("/api/pipeline-log")
async def get_pipeline_log(since: int = 0) -> dict[str, object]:
    """返回管线日志，since 为上次读取的行数。"""
    items = list(runner.pipeline.logs)
    return {"lines": items[since:], "total": len(items)}


@app.get("/api/fund-detail/{code}")
async def get_fund_detail(code: str) -> dict[str, object]:
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
        for r in repo.nav.series(code, limit=90)
    ]

    signal = repo.get_latest_monitor_event(code)
    current_signal = None
    if signal:
        # repo 返回结构化行：monitor 写入的是 "; " 拼接的纯文本原因，无需（也无法）按 JSON 解析
        current_signal = {
            "signal": signal["signal"],
            "logic_verdict": signal.get("logic_verdict") or "",
            "sector_risk": bool(signal.get("sector_risk")),
            "holding_risk": bool(signal.get("holding_risk")),
            "reason": signal.get("detail") or "",
            "date": signal.get("date") or "",
            "is_stale": bool(signal.get("is_stale")),
        }

    return {
        "fund": fund_info,
        "top_holdings": top_holdings,
        "nav_data": nav_data,
        "current_signal": current_signal,
        "period_returns": period_returns(code),
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
    uvicorn.run("app.web.app:app", host="0.0.0.0", port=port, reload=reload,
                log_level="warning")  # 屏蔽 uvicorn 访问日志（每请求一条 INFO）与启动 INFO

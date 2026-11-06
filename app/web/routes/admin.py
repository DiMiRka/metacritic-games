import asyncio
import math
import time
from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.service import load_session
from app.crawler.control import collect_active, request_manual_collect
from app.crawler.lock import crawl_lock_held
from app.crawler.runs import seconds_until_manual_run
from app.jobs.queue import retry_dead
from app.monitoring.metrics import refresh
from app.monitoring.snapshot import MonitorSnapshot, snapshot, worker_alive
from app.web.deps import Admin, Session, SessionFactory, require_admin, require_csrf, session_token
from app.web.templating import templates_of

MONITOR_INTERVAL = 2.0
# Как часто поток мониторинга перепроверяет, что сессия ещё действует
SESSION_RECHECK_SECONDS = 30.0

router = APIRouter(dependencies=[Depends(require_admin)])


def render_monitor(templates: Jinja2Templates, data: MonitorSnapshot) -> str:
    return templates.get_template("_monitor.html").render(m=data)


def sse_event(event: str, data: str) -> str:
    lines = "\n".join(f"data: {line}" for line in data.splitlines() or [""])
    return f"event: {event}\n{lines}\n\n"


async def monitor_events(
    session_factory: async_sessionmaker[AsyncSession],
    templates: Jinja2Templates,
    interval: float = MONITOR_INTERVAL,
    limit: int | None = None,
    still_allowed: Callable[[], Awaitable[bool]] | None = None,
    recheck_seconds: float = SESSION_RECHECK_SECONDS,
) -> AsyncIterator[str]:
    """Снимок мониторинга каждые interval секунд, пока сессия действует"""
    sent = 0
    checked_at = time.monotonic()
    while limit is None or sent < limit:
        if still_allowed and time.monotonic() - checked_at >= recheck_seconds:
            if not await still_allowed():
                yield sse_event("logout", "")
                return
            checked_at = time.monotonic()
        async with session_factory() as session:
            data = await snapshot(session)
        yield sse_event("update", render_monitor(templates, data))
        sent += 1
        if limit is None or sent < limit:
            await asyncio.sleep(interval)


@router.get("/monitor", response_class=HTMLResponse)
async def monitor_page(request: Request, session: Session) -> HTMLResponse:
    data = await snapshot(session)
    return templates_of(request).TemplateResponse(
        request, "monitor.html", {"m": data, "interval_ms": int(MONITOR_INTERVAL * 1000)}
    )


@router.get("/monitor/stream")
async def monitor_stream(request: Request, session_factory: SessionFactory) -> StreamingResponse:
    token = session_token(request)

    async def still_allowed() -> bool:
        async with session_factory() as db:
            auth = await load_session(db, token)
        return auth is not None and auth.is_admin

    return StreamingResponse(
        monitor_events(session_factory, templates_of(request), still_allowed=still_allowed),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/metrics", include_in_schema=False)
async def metrics(session: Session) -> Response:
    """Метрики Prometheus для администратора в браузере. Prometheus опрашивает тот же
    набор с внутреннего порта WEB_METRICS_PORT, без входа и без Caddy
    """
    await refresh(session)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.get("/api/v1/monitor")
async def api_monitor(session: Session) -> MonitorSnapshot:
    return await snapshot(session)


@router.post("/api/v1/runs", status_code=202, dependencies=[Depends(require_csrf)])
async def start_run(session: Session, admin: Admin) -> JSONResponse:
    """202 означает, что задача collect записана в очередь и не потеряется"""
    if await collect_active(session) or await crawl_lock_held(session):
        return JSONResponse({"detail": "Проход уже идёт"}, status_code=409)
    if wait := await seconds_until_manual_run(session):
        minutes = math.ceil(wait / 60)
        return JSONResponse(
            {"detail": f"Последний проход был недавно, повторить можно через {minutes} мин"},
            status_code=429,
            headers={"Retry-After": str(wait)},
        )
    if not await worker_alive(session):
        return JSONResponse({"detail": "Воркер не отвечает"}, status_code=503)
    job_id = await request_manual_collect(session, admin.user_id)
    if job_id is None:
        await session.rollback()
        return JSONResponse({"detail": "Проход уже идёт"}, status_code=409)
    await session.commit()
    return JSONResponse({"status": "accepted", "job_id": job_id}, status_code=202)


@router.post("/api/v1/jobs/{job_id}/retry", dependencies=[Depends(require_csrf)])
async def retry_job(session: Session, job_id: int) -> JSONResponse:
    """Повтор dead-задачи с чистым счётчиком попыток"""
    if not await retry_dead(session, job_id):
        await session.rollback()
        return JSONResponse({"detail": "Задача не найдена или не в статусе dead"}, status_code=404)
    await session.commit()
    return JSONResponse({"status": "queued", "job_id": job_id})

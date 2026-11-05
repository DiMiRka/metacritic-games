import asyncio
from collections.abc import AsyncIterator
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, get_args
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import settings
from app.crawl_requests import crawl_running, request_crawl
from app.db import get_engine, get_session, get_session_factory
from app.monitor import MonitorSnapshot, snapshot, worker_alive
from app.queries import GameFilters, Sort, get_game, list_games, platform_options
from app.schemas import GameDetailOut, GameListOut
from app.similar import similar_games

BASE_DIR = Path(__file__).parent
MONTHS = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]  # fmt: skip
# Без явной сортировки: новые сверху, а при поиске — по совпадению с запросом
SORT_LABELS = {
    "date": "Сначала новые",
    "metascore": "По Metascore",
    "userscore": "По оценке игроков",
    "title": "По названию",
}

app = FastAPI(title="Metacritic Games")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

Session = Annotated[AsyncSession, Depends(get_session)]
Engine = Annotated[AsyncEngine, Depends(get_engine)]
SessionFactory = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]

MONITOR_INTERVAL = 2.0


def ru_date(value: date | None) -> str:
    return f"{value.day} {MONTHS[value.month - 1]} {value.year}" if value else "дата неизвестна"


def score_level(value: float | None, scale: int = 100) -> str:
    if value is None:
        return "none"
    value = value * 100 / scale
    return "good" if value >= 75 else "mixed" if value >= 50 else "bad"


def local_time(value: datetime | None) -> str:
    return value.astimezone(settings.tz).strftime("%d.%m %H:%M:%S") if value else "—"


def duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes} мин {secs} с" if minutes else f"{secs} с"


templates.env.filters["ru_date"] = ru_date
templates.env.filters["local_time"] = local_time
templates.env.filters["duration"] = duration
templates.env.globals["score_level"] = score_level


def game_filters(
    q: Annotated[str, Query(max_length=200)] = "",
    platform: Annotated[str, Query(max_length=100)] = "",
    sort: str = "",
    page: Annotated[int, Query(ge=1)] = 1,
) -> GameFilters:
    valid = sort if sort in get_args(Sort) else None
    return GameFilters(q=q.strip(), platform=platform, sort=valid, page=page)


Filters = Annotated[GameFilters, Depends(game_filters)]


def page_url(filters: GameFilters, page: int) -> str:
    params = {
        "q": filters.q,
        "platform": filters.platform,
        "sort": filters.sort or "",
        "page": page,
    }
    query = urlencode({k: v for k, v in params.items() if v and not (k == "page" and v == 1)})
    return f"/?{query}" if query else "/"


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, session: Session, filters: Filters) -> HTMLResponse:
    result = await list_games(session, filters)
    context = {
        "filters": filters,
        "result": result,
        "sort_labels": SORT_LABELS,
        "prev_url": page_url(filters, result.page - 1) if result.page > 1 else None,
        "next_url": page_url(filters, result.page + 1) if result.page < result.pages else None,
    }
    partial = request.headers.get("HX-Request") and not request.headers.get(
        "HX-History-Restore-Request"
    )
    if partial:
        return templates.TemplateResponse(request, "_results.html", context)
    context["platforms"] = await platform_options(session)
    return templates.TemplateResponse(request, "index.html", context)


@app.get("/games/{slug}", response_class=HTMLResponse)
async def game_page(request: Request, session: Session, slug: str) -> HTMLResponse:
    game = await get_game(session, slug)
    if game is None:
        return templates.TemplateResponse(request, "404.html", {"slug": slug}, status_code=404)
    similar = await similar_games(session, game)
    summaries = {s.source: s for s in game.summaries}
    platforms = sorted(
        game.platforms, key=lambda p: (p.metascore is None, -(p.metascore or 0), p.platform_name)
    )
    return templates.TemplateResponse(
        request,
        "game.html",
        {"game": game, "platforms": platforms, "similar": similar, "summaries": summaries},
    )


@app.get("/api/games")
async def api_games(session: Session, filters: Filters) -> GameListOut:
    return GameListOut.from_page(await list_games(session, filters))


@app.get("/api/games/{slug}")
async def api_game(session: Session, slug: str) -> GameDetailOut:
    game = await get_game(session, slug)
    if game is None:
        raise HTTPException(status_code=404, detail="Игра не найдена")
    return GameDetailOut.from_game(game, await similar_games(session, game))


def render_monitor(data: MonitorSnapshot) -> str:
    return templates.get_template("_monitor.html").render(m=data)


def sse_event(event: str, data: str) -> str:
    lines = "\n".join(f"data: {line}" for line in data.splitlines() or [""])
    return f"event: {event}\n{lines}\n\n"


async def monitor_events(
    session_factory: async_sessionmaker[AsyncSession],
    interval: float = MONITOR_INTERVAL,
    limit: int | None = None,
) -> AsyncIterator[str]:
    sent = 0
    while limit is None or sent < limit:
        async with session_factory() as session:
            data = await snapshot(session)
        yield sse_event("update", render_monitor(data))
        sent += 1
        if limit is None or sent < limit:
            await asyncio.sleep(interval)


@app.get("/monitor", response_class=HTMLResponse)
async def monitor_page(request: Request, session: Session) -> HTMLResponse:
    data = await snapshot(session)
    return templates.TemplateResponse(
        request, "monitor.html", {"m": data, "interval_ms": int(MONITOR_INTERVAL * 1000)}
    )


@app.get("/monitor/stream")
async def monitor_stream(session_factory: SessionFactory) -> StreamingResponse:
    return StreamingResponse(
        monitor_events(session_factory),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/monitor")
async def api_monitor(session: Session) -> MonitorSnapshot:
    return await snapshot(session)


@app.post("/api/runs", status_code=202)
async def start_run(session: Session, engine: Engine) -> JSONResponse:
    if await crawl_running(engine):
        return JSONResponse({"detail": "Проход уже идёт"}, status_code=409)
    if not await worker_alive(session):
        return JSONResponse({"detail": "Воркер не отвечает"}, status_code=503)
    await request_crawl(session)
    return JSONResponse({"status": "accepted"}, status_code=202)

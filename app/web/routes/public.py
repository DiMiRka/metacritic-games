import asyncio
from typing import Annotated, get_args
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from loguru import logger
from sqlalchemy import text

from app.catalog.queries import (
    GameFilters,
    Sort,
    get_game,
    list_games,
    platform_options,
    score_history,
)
from app.catalog.schemas import GameDetailOut, GameListOut, ScorePointOut
from app.catalog.similar import similar_games
from app.web.deps import Session, load_auth
from app.web.middleware.ratelimit import api_limit, catalog_limit
from app.web.templating import templates_of

# Без явной сортировки: по Metascore, а при поиске — по совпадению с запросом
SORT_LABELS = {
    "metascore": "По Metascore",
    "date": "Сначала новые",
    "userscore": "По оценке игроков",
    "title": "По названию",
}

HEALTH_DB_TIMEOUT = 3.0

router = APIRouter(dependencies=[Depends(load_auth)])


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


def pager_pages(page: int, pages: int, around: int = 2) -> list[int | None]:
    """Номера для пагинации: первая, последняя и окно вокруг текущей, None — пропуск.
    Пропуск из одной страницы не ставится: вместо «…» сразу её номер"""
    shown = {1, pages, *range(max(page - around, 1), min(page + around, pages) + 1)}
    result: list[int | None] = []
    previous = 0
    for number in sorted(shown):
        if number - previous == 2:
            result.append(number - 1)
        elif number - previous > 2:
            result.append(None)
        result.append(number)
        previous = number
    return result


@router.get("/health")
async def health(session: Session) -> JSONResponse:
    """Готовность: процесс жив и база отвечает. По нему healthcheck в compose и Caddy"""
    try:
        await asyncio.wait_for(session.execute(text("SELECT 1")), HEALTH_DB_TIMEOUT)
    except Exception as exc:
        logger.warning("health: база недоступна: {!r}", exc)
        return JSONResponse({"status": "unavailable", "db": "error"}, status_code=503)
    return JSONResponse({"status": "ok", "db": "ok"})


@router.get("/", response_class=HTMLResponse, dependencies=[Depends(catalog_limit)])
async def index(request: Request, session: Session, filters: Filters) -> HTMLResponse:
    result = await list_games(session, filters)
    context = {
        "filters": filters,
        "result": result,
        "sort_labels": SORT_LABELS,
        "prev_url": page_url(filters, result.page - 1) if result.page > 1 else None,
        "next_url": page_url(filters, result.page + 1) if result.page < result.pages else None,
        # Широкий экран: по две страницы вокруг текущей, узкий: по одной
        "pager": {
            size: [
                (n, page_url(filters, n) if n else None)
                for n in pager_pages(result.page, result.pages, around)
            ]
            for size, around in (("wide", 2), ("narrow", 1))
        },
    }
    partial = request.headers.get("HX-Request") and not request.headers.get(
        "HX-History-Restore-Request"
    )
    if partial:
        return templates_of(request).TemplateResponse(request, "_results.html", context)
    context["platforms"] = await platform_options(session)
    return templates_of(request).TemplateResponse(request, "index.html", context)


@router.get("/games/{slug}", response_class=HTMLResponse)
async def game_page(request: Request, session: Session, slug: str) -> HTMLResponse:
    game = await get_game(session, slug)
    if game is None:
        return templates_of(request).TemplateResponse(
            request, "404.html", {"slug": slug}, status_code=404
        )
    similar = await similar_games(session, game)
    summaries = {s.source: s for s in game.summaries}
    platforms = sorted(
        game.platforms, key=lambda p: (p.metascore is None, -(p.metascore or 0), p.platform_name)
    )
    return templates_of(request).TemplateResponse(
        request,
        "game.html",
        {"game": game, "platforms": platforms, "similar": similar, "summaries": summaries},
    )


@router.get("/api/v1/games", dependencies=[Depends(api_limit)])
async def api_games(session: Session, filters: Filters) -> GameListOut:
    return GameListOut.from_page(await list_games(session, filters))


@router.get("/api/v1/games/{slug}", dependencies=[Depends(api_limit)])
async def api_game(session: Session, slug: str) -> GameDetailOut:
    game = await get_game(session, slug)
    if game is None:
        raise HTTPException(status_code=404, detail="Игра не найдена")
    return GameDetailOut.from_game(game, await similar_games(session, game))


@router.get("/api/v1/games/{slug}/scores", dependencies=[Depends(api_limit)])
async def api_game_scores(session: Session, slug: str) -> list[ScorePointOut]:
    """Изменения оценок по платформам для графиков: точка появляется, когда оценка поменялась"""
    game = await get_game(session, slug)
    if game is None:
        raise HTTPException(status_code=404, detail="Игра не найдена")
    return [ScorePointOut.model_validate(p) for p in await score_history(session, game.id)]

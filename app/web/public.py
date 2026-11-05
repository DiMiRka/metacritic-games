from typing import Annotated, get_args
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from app.queries import GameFilters, Sort, get_game, list_games, platform_options
from app.schemas import GameDetailOut, GameListOut
from app.similar import similar_games
from app.web.deps import Session, load_auth
from app.web.ratelimit import api_limit, catalog_limit
from app.web.templating import templates

# Без явной сортировки: новые сверху, а при поиске — по совпадению с запросом
SORT_LABELS = {
    "date": "Сначала новые",
    "metascore": "По Metascore",
    "userscore": "По оценке игроков",
    "title": "По названию",
}

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


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/", response_class=HTMLResponse, dependencies=[Depends(catalog_limit)])
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


@router.get("/games/{slug}", response_class=HTMLResponse)
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


@router.get("/api/games", dependencies=[Depends(api_limit)])
async def api_games(session: Session, filters: Filters) -> GameListOut:
    return GameListOut.from_page(await list_games(session, filters))


@router.get("/api/games/{slug}", dependencies=[Depends(api_limit)])
async def api_game(session: Session, slug: str) -> GameDetailOut:
    game = await get_game(session, slug)
    if game is None:
        raise HTTPException(status_code=404, detail="Игра не найдена")
    return GameDetailOut.from_game(game, await similar_games(session, game))

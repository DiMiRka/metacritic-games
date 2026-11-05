import math
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.catalog.models import Game, GamePlatform

PAGE_SIZE = 24
Sort = Literal["relevance", "date", "metascore", "userscore", "title"]


@dataclass
class GameFilters:
    q: str = ""
    platform: str = ""
    sort: Sort | None = None
    page: int = 1

    @property
    def effective_sort(self) -> Sort:
        if self.sort == "relevance" and not self.q:
            return "date"
        return self.sort or ("relevance" if self.q else "date")


@dataclass
class GameRow:
    game: Game
    metascore: int | None
    userscore: float | None


@dataclass
class GamePage:
    items: list[GameRow]
    total: int
    page: int
    pages: int


@dataclass
class PlatformOption:
    slug: str
    name: str
    games: int


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def list_games(session: AsyncSession, filters: GameFilters) -> GamePage:
    """Оценка игры в списке — лучшая по платформам, а при фильтре — по выбранной платформе"""
    scores = select(
        GamePlatform.game_id,
        func.max(GamePlatform.metascore).label("metascore"),
        func.max(GamePlatform.userscore).label("userscore"),
    ).group_by(GamePlatform.game_id)
    if filters.platform:
        scores = scores.where(GamePlatform.platform == filters.platform)
    scores = scores.subquery()

    stmt = select(Game, scores.c.metascore, scores.c.userscore)
    on = scores.c.game_id == Game.id
    stmt = stmt.join(scores, on) if filters.platform else stmt.outerjoin(scores, on)

    q = filters.q.strip()
    if q:
        stmt = stmt.where(
            or_(Game.title.ilike(f"%{_escape_like(q)}%", escape="\\"), Game.title.op("%")(q))
        )

    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = min(max(filters.page, 1), pages)

    order = {
        "relevance": [func.similarity(Game.title, q).desc()],
        "date": [Game.release_date.desc().nulls_last()],
        "metascore": [scores.c.metascore.desc().nulls_last()],
        "userscore": [scores.c.userscore.desc().nulls_last()],
        "title": [func.lower(Game.title)],
    }[filters.effective_sort]

    rows = (
        await session.execute(
            stmt.options(selectinload(Game.platforms))
            .order_by(*order, Game.id.desc())
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
        )
    ).all()
    return GamePage(
        items=[GameRow(r.Game, r.metascore, r.userscore) for r in rows],
        total=total,
        page=page,
        pages=pages,
    )


async def platform_options(session: AsyncSession) -> list[PlatformOption]:
    rows = await session.execute(
        select(
            GamePlatform.platform,
            func.min(GamePlatform.platform_name),
            func.count(GamePlatform.game_id),
        )
        .group_by(GamePlatform.platform)
        .order_by(func.count(GamePlatform.game_id).desc(), GamePlatform.platform)
    )
    return [PlatformOption(slug, name, count) for slug, name, count in rows]


async def get_game(session: AsyncSession, slug: str) -> Game | None:
    return await session.scalar(
        select(Game)
        .options(
            selectinload(Game.platforms),
            selectinload(Game.summaries),
            selectinload(Game.letsplay),
        )
        .where(Game.slug == slug)
    )
